"""Export a finished Test Run's Run log to CSV, JSON Lines, or Text (packet 24a).

One recorded run is one file. Rows are one per exported log line, in the order the lines
were recorded (``seq``), not grouped by owner, and carry the spec's fields: station local
time with milliseconds and the run's offset, level, source, owner, the owning schedule's
name, Step Number, Step Name, Attempt and the unchanged text (which may contain newlines).

The dialog's three filter groups select which lines are exported:

- ``levels``: a set of allowed levels (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``);
- ``sources``: a set of allowed sources (``schedule``, ``ate``, ``device``);
- ``owners``: a set of owner keys — ``"run"``, ``"pretest:<instance_seq>"``,
  ``"posttest:<instance_seq>"`` or ``"step:<node_seq>"`` — one per dialog tree node, a step
  bringing all of its Attempts.

A line is exported only when its level, source and owner are all allowed; ``None`` means
the group is fully ticked (the dialog's default).

Public API:
    build_export_rows(store, run_uuid, *, levels=None, sources=None, owners=None) -> list[ExportRow]
    export_log(store, run_uuid, path, fmt, *, levels=None, sources=None, owners=None) -> int
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from ..storage.store import RunStore


FIELDS = ("time", "level", "source", "owner", "schedule", "step_number", "step_name", "attempt", "text")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
SOURCES = ("schedule", "ate", "device")
FORMATS = ("csv", "jsonl", "text")


@dataclass
class ExportRow:
    """One exported log line, ready for the three writers.

    ``schedule`` / ``step_number`` / ``step_name`` / ``attempt`` are ``None`` when the line's
    owner is not a step (blank in every format); ``attempt`` is the Attempt's number."""

    time: str
    level: str
    source: str
    owner: str  # "Run" | "Pre-test" | "Post-test" | "Step"
    schedule: Optional[str]
    step_number: Optional[str]
    step_name: Optional[str]
    attempt: Optional[int]
    text: str


def _offset_delta(offset: str) -> _dt.timedelta:
    """Parse a stored UTC offset (``"+10:30"``, ``"-05:00"``, ``"+00:00"``) into a timedelta."""
    body = (offset or "+00:00").strip()
    sign = -1 if body.startswith("-") else 1
    parts = body[1:].split(":")
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 else 0
    return _dt.timedelta(hours=sign * hours, minutes=sign * minutes)


def _local(iso_utc: Optional[str], offset: str) -> Optional[str]:
    """The stored UTC instant in Station local wall-clock, with milliseconds and the offset.

    Format: ``YYYY-MM-DD HH:MM:SS.mmm +HH:MM`` — a space before the signed, colon-separated
    offset as stored on the run (e.g. ``+10:30``), so the run's zone wins over the host's."""
    if not iso_utc:
        return None
    dt = _dt.datetime.fromisoformat(iso_utc).astimezone(_dt.timezone.utc)
    local = dt + _offset_delta(offset)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')}.{local.microsecond // 1000:03d} {offset or '+00:00'}"


def _build_rows(store: RunStore, run_uuid: str, *, levels: Optional[Iterable[str]] = None,
                sources: Optional[Iterable[str]] = None, owners: Optional[Iterable[str]] = None) -> tuple[dict, list[ExportRow], dict[str, str]]:
    """Return ``(run_row, rows, owner_labels)`` for one run.

    ``rows`` are in recorded order and filtered as asked; ``owner_labels`` maps every owner key
    the dialog's tree can tick (``"run"``, each Pre-/Post-test Action, each step — in tree
    order) to its display label, so the Text header can name the applied owners."""
    row = store.get_test_run(run_uuid)
    if row is None:
        raise ValueError(f"no test run recorded for {run_uuid!r}")
    lines = store.log_lines(run_uuid)
    nodes = store.nodes(run_uuid)
    actions = store.actions(run_uuid)

    offset = row.get("utc_offset") or "+00:00"
    schedule_name = row.get("schedule_name")

    # Step info per node_seq: (step_number, step_name); ``step_number`` is text ("" when unset).
    step_info: dict[int, tuple[Optional[str], Optional[str]]] = {
        n["node_seq"]: (n.get("step_number") or None, n.get("step_name") or None)
        for n in nodes if n.get("kind") == "step" and n.get("node_seq") is not None
    }

    owner_labels: dict[str, str] = {"run": "Run"}
    for a in actions:
        key = f"{'pretest' if a.get('phase') == 'pre' else 'posttest'}:{a['instance_seq']}"
        owner_labels.setdefault(key, "Pre-test" if a.get("phase") == "pre" else "Post-test")
    for node_seq, (number, _name) in step_info.items():
        owner_labels[f"step:{node_seq}"] = f"Step {number or ''}"

    pre_instances = {a["instance_seq"] for a in actions if a.get("phase") == "pre"}
    post_instances = {a["instance_seq"] for a in actions if a.get("phase") == "post"}

    # A log line ties to a Pre-/Post-test Action through ``action_seq`` — the action's
    # ``instance_seq`` — and each phase enumerates its own actions from 0, so the Pre-test's
    # and Post-test's first actions share ``action_seq`` 0. Disambiguate by position in the
    # recorded log: every Pre-test line precedes the first step line, every Post-test line
    # follows the last one (the phases run before/after all steps, in order).
    step_indices = [i for i, ln in enumerate(lines) if ln.get("node_seq") is not None]
    first_step = step_indices[0] if step_indices else None
    last_step = step_indices[-1] if step_indices else None

    def _owner_key(i: int, ln: dict[str, Any]) -> str:
        """The owner key for this line: run / pretest:<n> / posttest:<n> / step:<node_seq>."""
        node_seq = ln.get("node_seq")
        if node_seq is not None:
            return f"step:{node_seq}"
        action_seq = ln.get("action_seq")
        if action_seq is None:
            return "run"
        if first_step is not None and i < first_step:
            return f"pretest:{action_seq}"
        if last_step is not None and i > last_step:
            return f"posttest:{action_seq}"
        # No step lines: a pre/post-only run. Both phases ran, so the Pre-test's lines precede
        # the Post-test's; the action rows' sequential start/end windows separate them.
        in_pre, in_post = action_seq in pre_instances, action_seq in post_instances
        if in_pre and not in_post:
            return f"pretest:{action_seq}"
        if in_post and not in_pre:
            return f"posttest:{action_seq}"
        for a in actions:  # only one of the two windows can contain the line's recorded time
            if a.get("phase") == "pre" and a.get("instance_seq") == action_seq and \
               a.get("started_at") and a.get("ended_at") and a["started_at"] <= ln["at"] <= a["ended_at"]:
                return f"pretest:{action_seq}"
        return f"posttest:{action_seq}" if in_post else "run"

    level_set = None if levels is None else set(levels)
    source_set = None if sources is None else set(sources)
    owner_set = None if owners is None else set(owners)

    rows: list[ExportRow] = []
    for i, ln in enumerate(lines):
        key = _owner_key(i, ln)
        if key == "run":
            owner, schedule, number, name, attempt = "Run", None, None, None, None
        elif key.startswith("pretest:"):
            owner, schedule, number, name, attempt = "Pre-test", schedule_name, None, None, None
        elif key.startswith("posttest:"):
            owner, schedule, number, name, attempt = "Post-test", schedule_name, None, None, None
        else:  # key == "step:<node_seq>"
            number, name = step_info.get(int(key.split(":", 1)[1]), (None, None))
            attempt = ln.get("attempt_no")
            owner, schedule = "Step", schedule_name

        row_out = ExportRow(
            time=_local(ln.get("at"), offset) or "",
            level=ln.get("level") or "",
            source=ln.get("source") or "",
            owner=owner,
            schedule=schedule,
            step_number=number,
            step_name=name,
            attempt=attempt,
            text=ln.get("text") or "",
        )
        if (level_set is None or row_out.level in level_set) and \
           (source_set is None or row_out.source in source_set) and \
           (owner_set is None or key in owner_set):
            rows.append(row_out)
    return row, rows, owner_labels


def build_export_rows(store: RunStore, run_uuid: str, *, levels: Optional[Iterable[str]] = None,
                     sources: Optional[Iterable[str]] = None, owners: Optional[Iterable[str]] = None) -> list[ExportRow]:
    """Return one row per exported log line, in recorded order (``seq``), filtered as asked.

    Raises :class:`ValueError` if no run is recorded for ``run_uuid``."""
    return _build_rows(store, run_uuid, levels=levels, sources=sources, owners=owners)[1]


def export_log(store: RunStore, run_uuid: str, path: Any, fmt: str, *, levels: Optional[Iterable[str]] = None,
               sources: Optional[Iterable[str]] = None, owners: Optional[Iterable[str]] = None) -> int:
    """Write one run's exported log to ``path`` in ``fmt`` (``"csv"``, ``"jsonl"`` or ``"text"``).

    Returns the number of rows written. When the filters leave no line, the file carries
    only the header (CSV/Text) or nothing (JSON Lines) and the count is 0."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; expected one of {', '.join(FORMATS)}")
    run_row, rows, owner_labels = _build_rows(store, run_uuid, levels=levels, sources=sources, owners=owners)
    if fmt == "csv":
        _write_csv(path, rows)
    elif fmt == "jsonl":
        _write_jsonl(path, rows)
    else:
        _write_text(path, rows, run_row, owner_labels, levels=levels, sources=sources, owners=owners)
    return len(rows)


def _write_csv(path: Any, rows: list[ExportRow]) -> None:
    """UTF-8 with BOM (so Excel opens it), a header row of the field names, minimal quoting.

    Text with commas, quotes or newlines is quoted, so a multi-line traceback stays in one
    cell; no record of the filters."""
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        for r in rows:
            writer.writerow([
                r.time, r.level, r.source, r.owner,
                r.schedule or "", r.step_number or "", r.step_name or "",
                "" if r.attempt is None else r.attempt, r.text,
            ])


def _write_jsonl(path: Any, rows: list[ExportRow]) -> None:
    """UTF-8, one JSON object per line with the field names above; blanks are ``null``,
    ``step_number`` a string and ``attempt`` a number. No record of the filters."""
    with Path(path).open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({
                "time": r.time,
                "level": r.level,
                "source": r.source,
                "owner": r.owner,
                "schedule": r.schedule,
                "step_number": r.step_number,
                "step_name": r.step_name,
                "attempt": r.attempt,
                "text": r.text,
            }, ensure_ascii=False) + "\n")


def _filters_text(levels: Optional[Iterable[str]], sources: Optional[Iterable[str]],
                  owners: Optional[Iterable[str]], owner_labels: dict[str, str]) -> str:
    """The header's one-line description of the filters actually applied (``"none"`` when
    every group is fully ticked — the dialog's default)."""
    parts: list[str] = []
    if levels is not None and set(levels) != set(LEVELS):
        parts.append(f"levels={','.join(level for level in LEVELS if level in set(levels))}")
    if sources is not None and set(sources) != set(SOURCES):
        parts.append(f"sources={','.join(source for source in SOURCES if source in set(sources))}")
    if owners is not None and set(owners) != set(owner_labels):
        parts.append(f"owners={','.join(owner_labels[k] for k in owner_labels if k in set(owners))}")
    return "none" if not parts else "; ".join(parts)


def _write_text(path: Any, rows: list[ExportRow], run_row: dict, owner_labels: dict[str, str], *,
                levels: Optional[Iterable[str]], sources: Optional[Iterable[str]],
                owners: Optional[Iterable[str]]) -> None:
    """UTF-8 for reading and sharing: a header naming the run UUID, UUT Type and serial, the
    Test Schedule, the start time and the filters applied, then one line per log line.

    Each line is ``time``, two spaces, and the level / source / owner fields padded to
    9 / 10 / 12 columns, then the text (the spec's layout); the owner is written as ``Run``,
    ``Pre-test``, ``Post-test`` or ``Step <Step Number>#<attempt>``. The continuation lines of a
    multi-line message are indented under its text."""
    offset = run_row.get("utc_offset") or "+00:00"
    start = _local(run_row.get("started_at"), offset)
    lines = [
        f"Run UUID: {run_row.get('run_uuid') or ''}",
        f"UUT Type: {run_row.get('uut_type') or ''}, Serial: {run_row.get('uut_serial') or ''}",
        f"Test Schedule: {run_row.get('schedule_name') or ''}",
        f"Start: {start or ''}",
        f"Filters: {_filters_text(levels, sources, owners, owner_labels)}",
        "",
    ]
    for r in rows:
        owner = f"Step {r.step_number or ''}#{r.attempt or 0}" if r.owner == "Step" else r.owner
        prefix = f"{r.time}  {r.level:<9}{r.source:<10}{owner:<12}"
        lines.append(prefix + r.text)
        for continuation in r.text.split("\n")[1:]:
            lines.append(" " * len(prefix) + continuation)
    with Path(path).open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
