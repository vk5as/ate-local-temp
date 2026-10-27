"""Render and write ATE's self-contained HTML Test Report from a recorded run.

The report is built only from the :class:`~ate.storage.store.RunStore` record for one run —
the doc fields, times and result counts all come back through ``get_test_run``/``nodes``.
``config`` supplies the three v1 customisations: organisation, logo (embedded as a data URI)
and paper size; nothing else reaches the template.

Public API:
    render_report(store, run_uuid, config, include_run_log=True) -> str
    write_report(store, run_uuid, config) -> Path
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from ..config import Config
from ..storage.store import RunStore
from .log_export import owner_keys


# The CSS ``@page`` margin boxes (mm) must match exactly the ``QPageLayout`` margins passed to
# ``printToPdf`` by the helper process — Chromium takes its print margins from Qt. Defined once so
# the template's ``{{ page_margin_css }}`` and the helper can never drift apart (spec rule 21).
REPORT_PAGE_MARGINS_MM = {"top": 24.0, "right": 14.0, "bottom": 18.0, "left": 14.0}


def _page_margin_css(margins: dict[str, float]) -> str:
    """The ``@page margin`` value as a single CSS string (top right bottom left), e.g. ``24mm 14mm ...``."""
    return " ".join(f"{m:g}mm" for m in (margins["top"], margins["right"], margins["bottom"], margins["left"]))


# The seven Result values a step can end with (NOTRUN for a step that never ran). Order is the
# order they are shown in the Summary counts table.
RESULT_LABELS = ["PASS", "FAIL", "ERROR", "NOREPORT", "SKIPPED", "FORCED_NOTRUN", "NOTRUN"]

# Characters illegal in Windows file names; replaced with "_" on both platforms (spec rule 17).
_ILLEGAL_WIN = "<>:\"/\\|?*" + "\x00"


_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _logo_data_uri(logo_path: Optional[Path]) -> str:
    """Embed a logo as a ``data:`` URI (base64 of the file bytes, mime type from its extension)."""
    if not logo_path:
        return ""
    try:
        raw = Path(logo_path).read_bytes()
    except OSError:
        # A configured-but-unreadable logo is ignored rather than failing the report.
        return ""
    ext = Path(logo_path).suffix.lower().lstrip(".")
    mime = _MIME_BY_EXT.get(ext, "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _offset_delta(offset: str) -> _dt.timedelta:
    """Parse a stored UTC offset (``"+10:30"``, ``"-05:00"`` or ``"+00:00"``) into a timedelta."""
    body = (offset or "+00:00").strip().replace("Z", "")
    sign = -1 if body.startswith("-") else 1
    parts = body[1:].replace("-", ":").split(":")
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 else 0
    return _dt.timedelta(hours=sign * hours, minutes=sign * minutes)


def _local_time(iso_utc: Optional[str], offset: str) -> Optional[str]:
    """Convert a stored UTC ISO time to the Station's local wall-clock with its offset shown."""
    if not iso_utc:
        return None
    dt = _dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))  # already tz-aware UTC
    delta = _offset_delta(offset)
    local = dt + delta
    sign = "-" if delta < _dt.timedelta(0) else "+"
    total = abs(int(delta.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{local:%Y-%m-%d %H:%M:%S} {sign}{hours:02d}:{minutes:02d}"


def _local_stamp(iso_utc: Optional[str], offset: str) -> Optional[str]:
    """Local start as ``yyyymmdd-HHMMSS`` for the report file name (offset-adjusted)."""
    if not iso_utc:
        return None
    dt = _dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))  # already tz-aware UTC
    local = (dt + _offset_delta(offset)).replace(microsecond=0)
    return local.strftime("%Y%m%d-%H%M%S")


def _sanitize_filename(name: str) -> str:
    """Replace Windows-invalid characters and control chars with ``_`` (spec rule 17)."""
    for ch in _ILLEGAL_WIN:
        name = name.replace(ch, "_")
    return re.sub(r"[\x00-\x1f\x7f]", "_", name)


def _tree_depths(nodes: list[dict]) -> dict[int, int]:
    """Nesting depth of every expanded-tree node, keyed by ``node_seq`` (0 = top level).

    ``nodes`` comes back in tree order, so a node's parent is always already keyed when it is read."""
    depth: dict[int, int] = {}
    for row in nodes:
        seq = int(row.get("node_seq") or 0)
        parent = row.get("parent_node_seq")
        depth[seq] = 0 if parent is None else depth.get(int(parent), 0) + 1
    return depth


def _subtree_steps(nodes: list[dict]) -> dict[int, list[dict]]:
    """Every Test Step beneath each header, keyed by that header's ``node_seq``.

    Nested headers contribute their own steps, so a header's roll-up counts the whole subtree under
    it (spec rule 68); the header row itself is never in the list."""
    children: dict[int, list[int]] = {}
    for row in nodes:
        parent = row.get("parent_node_seq")
        if parent is not None:
            children.setdefault(int(parent), []).append(int(row.get("node_seq") or 0))
    by_seq = {int(row.get("node_seq") or 0): row for row in nodes}
    beneath: dict[int, list[dict]] = {}
    for row in nodes:
        if row.get("kind") != "header":
            continue
        seq = int(row.get("node_seq") or 0)
        steps: list[dict] = []
        stack = list(children.get(seq, []))
        while stack:
            child = by_seq[stack.pop()]
            if child.get("kind") == "header":
                stack.extend(children.get(int(child.get("node_seq") or 0), []))
            else:
                steps.append(child)
        beneath[seq] = steps
    return beneath


def _rollup_text(steps: list[dict]) -> str:
    """A header's Result column: "4 steps: 3 PASS, 1 FAIL", zero counts left out (spec rule 68)."""
    if not steps:
        return "0 steps"
    counts: dict[str, int] = {}
    for step in steps:
        result = step.get("result") or "NOTRUN"
        counts[result] = counts.get(result, 0) + 1
    parts = [f"{counts[r]} {r}" for r in RESULT_LABELS if counts.get(r)]
    return f"{len(steps)} step{'s' if len(steps) != 1 else ''}: " + ", ".join(parts)


def _input_lines(declared: Optional[str], values: list[dict]) -> list[dict]:
    """One "Input n (desc): value" line per Operator input value, labelled from the step's declaration.

    A CHECKBOX selection is stored as a JSON array of labels (packet 10), so it prints as those
    labels joined by ", " rather than as raw JSON; every other value prints exactly as recorded."""
    if not values:
        return []
    labels: dict[int, str] = {}
    checkbox: set[int] = set()
    if declared:
        try:
            decls = json.loads(declared)
        except ValueError:
            decls = []
        # `inputs` stores the schedule-format declarations in order, so position i is input index i.
        for i, decl in enumerate(decls, start=1):
            labels[i] = (decl.get("desc") or "").strip()
            if decl.get("type") == "ENUM" and decl.get("input") == "CHECKBOX":
                checkbox.add(i)
    lines: list[dict] = []
    for v in values:
        index = int(v.get("input_index") or 0)
        value = v.get("value") if v.get("value") is not None else ""
        if index in checkbox and isinstance(value, str):
            try:
                chosen = json.loads(value)
            except ValueError:
                chosen = None
            if isinstance(chosen, list):
                value = ", ".join(str(label) for label in chosen)
        lines.append({"index": index, "desc": labels.get(index, ""), "value": value})
    return lines


def _row(number: str, name: str, message: str, expected: str, actual: str, result: str, depth: int,
         *, override: str = "", retry: str = "", choice: str = "", inputs: Optional[list[dict]] = None,
         rollup: str = "", is_header: bool = False, sub: bool = False) -> dict:
    """One Steps-table row: a Test Step (main or Attempt sub-row) or an include header."""
    return {
        "sub": sub,
        "number": number,
        "name": name,
        "message": message,
        "choice": choice,
        "expected": expected,
        "actual": actual,
        "result": result,
        "override": override,
        "retry": retry,
        "inputs": inputs or [],
        "rollup": rollup,
        "depth": depth,
        "is_header": is_header,
    }


def _step_rows(nodes: list[dict], attempts: list[dict], input_values: list[dict]) -> list[dict]:
    """One row per expanded-tree node, in tree order, with earlier Attempts as sub-rows (spec 21b).

    Header rows carry the roll-up of the steps beneath them and, like Test Steps, an Operator
    override label whenever their recorded ``enabled`` differs from ``default_enabled`` (spec rule
    66): a header's ``enabled`` is derived from its children (packet 15d), so disabling every step
    under a Sub-schedule labels the header "disabled by Operator". A retried step shows "↻ n" on the
    main row, which always holds its last Attempt; Attempts 1..n-1 follow it as deeper sub-rows with
    the prompt choice after the message. A step that never ran shows NOTRUN with no Attempt to print."""
    atts: dict[int, list[dict]] = {}
    for a in attempts:
        atts.setdefault(int(a.get("node_seq") or 0), []).append(a)
    for rows in atts.values():
        rows.sort(key=lambda a: int(a.get("attempt_no") or 0))

    asked: dict[tuple[int, int], list[dict]] = {}
    for v in input_values:
        asked.setdefault((int(v.get("node_seq") or 0), int(v.get("attempt_no") or 0)), []).append(v)

    depths = _tree_depths(nodes)
    beneath = _subtree_steps(nodes)

    rows: list[dict] = []
    for node in nodes:
        seq = int(node.get("node_seq") or 0)
        depth = depths.get(seq, 0)
        name = node.get("step_name") or ""
        number = node.get("step_number") or ""
        default_enabled, enabled = node.get("default_enabled"), node.get("enabled")
        override = ""
        if default_enabled is not None and enabled is not None and bool(default_enabled) != bool(enabled):
            override = "enabled by Operator" if enabled else "disabled by Operator"
        if node.get("kind") == "header":
            rows.append(_row(number, name, "", "", "", "", depth, override=override,
                             rollup=_rollup_text(beneath.get(seq, [])), is_header=True))
            continue

        node_atts = atts.get(seq, [])
        last = node_atts[-1] if node_atts else {}
        attempt_count = int(node.get("attempt_count") or 0)
        rows.append(
            _row(
                number,
                name,
                last.get("msg") or "",
                last.get("expected") or "",
                last.get("actual") or "",
                node.get("result") or "NOTRUN",
                depth,
                override=override,
                retry=f"↻ {attempt_count}" if attempt_count > 1 else "",
                inputs=_input_lines(node.get("inputs"), asked.get((seq, int(last.get("attempt_no") or 0)), [])),
            )
        )
        for attempt in node_atts[:-1]:  # earlier Attempts, indented one level deeper
            rows.append(
                _row(
                    "",
                    name,
                    attempt.get("msg") or "",
                    attempt.get("expected") or "",
                    attempt.get("actual") or "",
                    attempt.get("pass_fail") or "NOTRUN",
                    depth + 1,
                    sub=True,
                    choice=f"→ {attempt['prompt_choice']}" if attempt.get("prompt_choice") else "",
                    inputs=_input_lines(node.get("inputs"), asked.get((seq, int(attempt.get("attempt_no") or 0)), [])),
                )
            )
    return rows


def _equipment_rows(store: RunStore, run_uuid: str) -> list[dict]:
    """Equipment in declaration order, every nil printed as a blank (spec Equipment).

    Nothing is reformatted: `cal_due` is free text and stays exactly what the schedule gave."""
    return [
        {
            "name": row.get("name") or "",
            "serial": row.get("serial") or "",
            "connection": row.get("connection") or "",
            "cal_due": row.get("cal_due") or "",
            "notes": row.get("notes") or "",
        }
        for row in store.equipment(run_uuid)
    ]


def _media_release_rows(store: RunStore, run_uuid: str) -> list[dict]:
    """Only the Media Releases this run used: the top-level one plus every release that supplied
    a file the run loaded (spec Configuration). Chain order (`seq`) is kept; an unused dependency
    is left out, and bundled `ate.` modules belong to no release, so they add none."""
    used = {row.get("media_number") for row in store.run_files(run_uuid) if row.get("media_number")}
    return [
        {
            "media_number": rel.get("media_number") or "",
            "name": rel.get("name") or "",
            "version": rel.get("version") or "",
            "atedef_sha256": rel.get("atedef_sha256") or "",
        }
        for rel in store.media_releases(run_uuid)
        if rel.get("seq") == 0 or rel.get("media_number") in used
    ]


def _file_rows(store: RunStore, run_uuid: str) -> list[dict]:
    """Every `run_file` row: path, owning Media Number, expected and computed SHA-256, whether the
    bytes were stored, and the mismatch flag the template highlights. `ate.` modules have no
    expected checksum, so their expected cell is blank."""
    return [
        {
            "path": row.get("path") or "",
            "media_number": row.get("media_number") or "",
            "expected": row.get("expected_sha256") or "",
            "computed": row.get("computed_sha256") or "",
            "mismatch": bool(row.get("mismatch")),
            "stored": row.get("content") is not None,  # an empty file stored is still stored
        }
        for row in store.run_files(run_uuid)
    ]


def _instance_rows(store: RunStore, run_uuid: str) -> list[dict]:
    """Schedule instances in tree order with name, file and prefix; `keyvals` are not shown.

    `instance_seq` is already tree order (0 is the top-level schedule), so the depth is just the
    parent chain, and the template indents by it like the Steps table."""
    instances = store.schedule_instances(run_uuid)
    depth: dict[int, int] = {}
    for row in instances:
        seq = int(row.get("instance_seq") or 0)
        parent = row.get("parent_instance_seq")
        depth[seq] = 0 if parent is None else depth.get(int(parent), 0) + 1
    return [
        {
            "name": row.get("schedule_name") or "",
            "file": row.get("file") or "",
            "prefix": row.get("prefix") or "",
            "depth": depth.get(int(row.get("instance_seq") or 0), 0),
        }
        for row in instances
    ]


def _run_log_groups(store: RunStore, run_uuid: str, offset: str) -> list[dict]:
    """Every INFO/WARNING/ERROR log line grouped by owner: the run, then each Pre-test and
    Post-test Action, then each step Attempt; lines within a group stay in recorded order, which
    is time order (spec Run log).

    `DEBUG` lines are dropped here, and Device trace is `DEBUG`, so it never reaches the report.
    Ownership comes from :func:`ate.report.log_export.owner_keys`, so the report and the export
    dialog can never disagree about who a line belongs to."""
    lines = []
    for key, line in zip(owner_keys(store, run_uuid), store.log_lines(run_uuid)):
        if line.get("level") not in ("INFO", "WARNING", "ERROR"):
            continue
        # A step's owner key is its node; split it further by Attempt so each Attempt is its own group.
        if key.startswith("step:") and line.get("attempt_no") is not None:
            key = f"{key}:{line['attempt_no']}"
        lines.append((key, line))
    if not lines:
        return []

    actions = store.actions(run_uuid)
    instances = {int(row["instance_seq"]): row.get("schedule_name") or "" for row in store.schedule_instances(run_uuid)}
    steps = {
        int(row["node_seq"]): (row.get("step_number") or "", row.get("step_name") or "")
        for row in store.nodes(run_uuid)
        if row.get("kind") == "step" and row.get("node_seq") is not None
    }
    pre_count = sum(1 for a in actions if a.get("phase") == "pre")
    post_count = sum(1 for a in actions if a.get("phase") == "post")

    groups: dict[str, list[dict]] = {}
    for key, line in lines:
        groups.setdefault(key, []).append(
            {
                "time": _local_time(line.get("at"), offset) or "",
                "level": line.get("level") or "",
                "source": line.get("source") or "",
                "text": line.get("text") or "",
            }
        )

    def phase_label(phase: str, instance_seq: int) -> str:
        """Pre-test, or Pre-test (Channel check, instance 1) when the phase has more than one Action."""
        name = instances.get(int(instance_seq), "")
        count = pre_count if phase == "pre" else post_count
        if count < 2:
            return "Pre-test" if phase == "pre" else "Post-test"
        return f"{'Pre-test' if phase == 'pre' else 'Post-test'} ({name}, instance {instance_seq})"

    def ordered(phase: str) -> list[str]:
        """This phase's owner keys in the order its Actions ran (run_order, then instance)."""
        return [
            f"{phase}test:{a['instance_seq']}"
            for a in sorted((a for a in actions if a.get("phase") == phase),
                            key=lambda a: (int(a.get("run_order") or 0), int(a.get("instance_seq") or 0)))
        ]

    out: list[dict] = []
    if "run" in groups:
        out.append({"label": "Run", "lines": groups["run"]})
    for phase in ("pre", "post"):
        for key in ordered(phase):
            if key in groups:
                out.append({"label": phase_label(phase, key.split(":", 1)[1]), "lines": groups[key]})
    for node_seq, (number, name) in sorted(steps.items()):  # nodes are in tree order
        attempt_keys = sorted(
            (k for k in groups if k.startswith(f"step:{node_seq}:")),
            key=lambda k: int(k.rsplit(":", 1)[1] or 0),
        )
        for key in attempt_keys:
            label = " ".join(p for p in ("Step", number, name) if p) + f" (attempt {key.rsplit(':', 1)[1]})"
            out.append({"label": label, "lines": groups[key]})
    return out


def _report_context(store: RunStore, run_uuid: str, config: Config, *, include_run_log: bool = True) -> dict:
    """Everything the report template needs for one run, as its render context.

    Split out from :func:`render_report` so a caller (or a test) can render the same context with a
    different Jinja environment — for example with autoescape off — and compare the byte output."""
    row = store.get_test_run(run_uuid)
    if row is None:
        raise ValueError(f"no test run recorded for {run_uuid!r}")

    nodes = store.nodes(run_uuid)
    offset = row.get("utc_offset") or "+00:00"
    start_iso = row.get("started_at")
    end_iso = row.get("ended_at")

    # Summary counts from the expanded tree. Header rows are excluded; a step with no Attempt is
    # counted as NOTRUN (its node.result). Retried steps and Operator enable overrides come from
    # the same rows. Failure rows need each failing step's last message, so read attempts once.
    counts: dict[str, int] = {label: 0 for label in RESULT_LABELS}
    retried_steps = 0
    operator_overrides = 0
    node_message: dict[int, str] = {}
    for node in nodes:
        if node.get("kind") == "header":
            continue
        result = node.get("result") or "NOTRUN"
        counts[result] = counts.get(result, 0) + 1
        if int(node.get("attempt_count") or 0) > 1:
            retried_steps += 1
        default_enabled = node.get("default_enabled")
        enabled = node.get("enabled")
        if default_enabled is not None and enabled is not None and bool(default_enabled) != bool(enabled):
            operator_overrides += 1

    by_node_msg: dict[int, str] = {}
    for attempt in store.attempts(run_uuid):
        msg = attempt.get("msg") or ""
        if msg:
            node_message.setdefault(attempt.get("node_seq"), msg)

    failures = [
        {
            "number": node.get("step_number"),
            "name": node.get("step_name") or "",
            "message": node_message.get(node["node_seq"], ""),
            "result": node.get("result") or "NOTRUN",
        }
        for node in nodes
        if node.get("kind") != "header" and node.get("result") in ("FAIL", "ERROR", "NOREPORT")
    ]

    # Banners: an INTERRUPTED run, a checksum mismatch among the files used, or a Pre-test/Post-
    # test Action that errored (named on the cover per spec rule 44). The pre/post error comes
    # from the owning action row's outcome.
    interrupted = row.get("end_reason") == "INTERRUPTED"
    checksum_mismatch = any(f.get("mismatch") for f in store.run_files(run_uuid))
    pretest_error, posttest_error = _phase_errors(store, run_uuid)

    ctx = {
        # Shared with the PDF helper's QPageLayout so CSS and print margins can't drift. Both of
        # these are CSS/URI markup the template interpolates raw, so they are marked safe.
        "page_margin_css": Markup(_page_margin_css(REPORT_PAGE_MARGINS_MM)),
        "org": config.reports_organisation,
        # The logo is a base64 data URI (ASCII A-Za-z0-9+/= plus ",;=/") emitted into a CSS url()
        # and an <img src>; it is markup the template must not escape. No external reference is
        # ever emitted.
        "logo": Markup(_logo_data_uri(config.reports_logo)),
        "paper": config.reports_paper_size,
        # Cover — document fields (recorded on test_run by packet 21a).
        "doc_title": row.get("doc_title") or None,
        "doc_id": row.get("doc_id") or None,
        "doc_rev": row.get("doc_rev") or None,
        # Run details.
        "uut_type": row.get("uut_type") or "",
        "uut_serial": row.get("uut_serial") or "",
        "project": row.get("project") or "",
        "station": row.get("station_name") or "",
        "start_local": _local_time(start_iso, offset),
        "end_local": _local_time(end_iso, offset),
        "duration": None,
        "ate_version": row.get("ate_version") or "",
        "stop_on_fail": bool(row.get("stop_on_fail")),
        # Schedule.
        "schedule_name": row.get("schedule_name") or "",
        "schedule_description": row.get("schedule_description") or "",
        "schedule_file": None,
        "media_number": row.get("media_number") or "",
        "media_name": None,
        "media_version": None,
        # Result banner + warnings.
        "result": (row.get("result") or "").upper(),
        "end_reason": row.get("end_reason") or "",
        "pretest_failed_schedule": pretest_error,
        "posttest_failed_schedule": posttest_error,
        "checksum_mismatch": checksum_mismatch,
        "interrupted": interrupted,
        # Summary counts.
        "result_labels": RESULT_LABELS,
        "counts": counts,
        "retried_steps": retried_steps,
        "operator_overrides": operator_overrides,
        # Failures list (Cover).
        "failures": failures,
        # Steps section (packet 21b): one row per expanded-tree node, headers indented by depth with
        # their roll-up, retried steps followed by their earlier Attempts.
        "steps": _step_rows(nodes, store.attempts(run_uuid), store.input_values(run_uuid)),
        # Equipment, Configuration and Run log (packet 21c).
        "equipment": _equipment_rows(store, run_uuid),
        "media_releases": _media_release_rows(store, run_uuid),
        "files": _file_rows(store, run_uuid),
        "instances": _instance_rows(store, run_uuid),
        "include_run_log": include_run_log,
        "run_log": _run_log_groups(store, run_uuid, offset) if include_run_log else [],
        # Operator + signature box.
        "operator": row.get("operator") or "",
    }

    return ctx


def _render(context: dict) -> str:
    """Render report.html.j2 with `context`, escaping every value that is not marked safe."""
    env = Environment(
        loader=PackageLoader("ate.report", "templates"),
        # report.html.j2 does not end in ".html", so "j2" has to be an enabled extension or
        # autoescape stays off and every value lands in the page verbatim.
        autoescape=select_autoescape(["html", "j2"]),
    )
    # Spread the context as top-level variables (the template reads them bare, e.g. {{ uut_type }}).
    return env.get_template("report.html.j2").render(**context)


def render_report(store: RunStore, run_uuid: str, config: Config, *, include_run_log: bool = True) -> str:
    """Render the self-contained HTML Test Report for a finished run.

    Raises :class:`ValueError` if no run is recorded for ``run_uuid`` — callers that regenerate
    from History only ever render finished runs. Every context value is autoescaped unless it is a
    :class:`markupsafe.Markup`, so the template's deliberate markup (logo data URI, print margins)
    stays raw while Operator-entered text cannot become markup."""
    return _render(_report_context(store, run_uuid, config, include_run_log=include_run_log))


def _phase_errors(store: RunStore, run_uuid: str) -> tuple[Optional[dict], Optional[dict]]:
    """Return the errored Pre-test/Post-test Action as ``{"name", "error"}`` or ``None``."""
    pretest_error = None
    posttest_error = None
    for action in store.actions(run_uuid):
        if action.get("outcome") != "error":
            continue
        phase = action.get("phase")
        name = "Post-test Action" if phase == "post" else "Pre-test Action"
        entry = {"name": name, "error": action.get("error") or ""}
        if phase == "pre" and pretest_error is None:
            pretest_error = entry
        elif phase == "post" and posttest_error is None:
            posttest_error = entry
    return pretest_error, posttest_error


def result_label(row: Mapping[str, Any]) -> str:
    """The run's result as it goes into the report file name (``PASS`` when the row has none).

    The HTML report and the export dialog's ``_log`` default both name files from this
    (spec 21a / 24b Saving), so it lives here rather than being repeated."""
    return (row.get("result") or "PASS").upper()


def report_stem_and_directory(store: RunStore, run_uuid: str, config: Config) -> tuple[Path, str]:
    """Return ``(directory, stem)`` for this run's report files (spec 21a naming).

    The file name is ``<UUT Type>/<UUT Type>_<serial>_<local start yyyymmdd-HHMMSS>_<PASS|FAIL>.html``;
    stripping the result suffix leaves a stable ``stem`` that both the HTML report and its derived PDF
    share, so a caller can glob ``stem*.html`` / ``stem*.pdf`` in ``directory`` to find every file for
    the run. Raises :class:`ValueError` if no run is recorded for ``run_uuid``."""
    row = store.get_test_run(run_uuid)
    if row is None:
        raise ValueError(f"no test run recorded for {run_uuid!r}")
    uut_type = (row.get("uut_type") or "").strip() or "Report"
    serial = (row.get("uut_serial") or "").strip()
    stamp = _local_stamp(row.get("started_at"), row.get("utc_offset") or "+00:00") or "0000-00-00-000000"
    directory = Path(config.reports_directory) / uut_type
    stem = f"{uut_type}_{serial}_{stamp}"
    return directory, stem


def write_report(store: RunStore, run_uuid: str, config: Config, *, include_run_log: bool = True) -> Path:
    """Render and write this run's HTML report under ``reports.directory``.

    File name is ``<UUT Type>/<UUT Type>_<serial>_<local start yyyymmdd-HHMMSS>_<PASS|FAIL>.html``;
    Windows-invalid characters are replaced, and an existing file is never overwritten (a free
    ``-2``, ``-3`` … suffix is used instead). The optional ``include_run_log`` flag passes through to
    :func:`render_report`, so a regenerated report can omit the run log."""
    html = render_report(store, run_uuid, config, include_run_log=include_run_log)
    row = store.get_test_run(run_uuid)
    assert row is not None, "no test run recorded for the report"

    uut_type = (row.get("uut_type") or "").strip() or "Report"
    serial = (row.get("uut_serial") or "").strip()
    stamp = _local_stamp(row.get("started_at"), row.get("utc_offset") or "+00:00") or "0000-00-00-000000"
    result = result_label(row)

    directory = Path(config.reports_directory)
    target_dir = directory / uut_type
    target_dir.mkdir(parents=True, exist_ok=True)

    name = _sanitize_filename(f"{uut_type}_{serial}_{stamp}_{result}.html")
    suffix = 2
    while (target_dir / name).exists():
        # Insert the next free "-N" before ".html" so an existing report is never overwritten.
        stem = name[: -len(".html")]
        name = f"{stem}-{suffix}.html"
        suffix += 1

    path = target_dir / name
    path.write_text(html, encoding="utf-8")
    return path
