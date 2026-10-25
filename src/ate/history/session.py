"""The History session finds past Test Runs (ticket 23a).

It is UI-free — no widgets, no event loop — and holds no SQL. Every query goes through
:class:`RunStore.query_runs`; this class only shapes the filters, sort order and paging.
Test Runs are append-only, so there is nothing to delete or edit here (the neighbouring
packets own the History screen's display of that fact)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..config import Config
from ..report.html import RESULT_LABELS
from ..report.html import report_stem_and_directory
from ..report.html import result_label
from ..report.html import write_report
from ..report.log_export import export_log
from ..storage import HISTORY_SORT_KEYS
from ..storage import RunStore


class HistorySession:
    """Browse past runs newest-first, sorted and filtered, one page at a time.

    ``store`` is the only hard dependency; ``page_size`` defaults to 100 rows per fetch so a large
    database stays fast (rows still load page by page). Changing the filters or sort order resets
    to the first page. An optional ``config`` supplies report settings (the reports directory and
    paper size) for :meth:`regenerate` and the project prefill of :meth:`new_run_prefill`; it is not
    required for browsing, sorting, filtering or summarising."""

    def __init__(self, store: RunStore, *, page_size: int = 100, config: Optional[Config] = None) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.store = store
        self.page_size = page_size
        self.config = config
        # Sort order defaults to newest first.
        self.sort_key = "started_at"
        self.descending = True
        # Filters live here as a plain mapping; the store builds the SQL from them.
        self.filters: dict[str, Any] = {}
        # Cursor into the current sort/filter state (0-based page index).
        self._page_index = 0
        # Whether a run is currently in progress; set by :meth:`set_run_in_progress`.
        self._run_in_progress = False

    def set_filters(self, filters: Mapping[str, Any]) -> None:
        """Replace the active filters and reset to the first page."""
        self.filters = dict(filters)
        self._reset_page()

    def set_sort(self, sort_key: str, descending: bool = True) -> None:
        """Set the sort column/direction (must be a valid History column) and reset to page 0."""
        if sort_key not in HISTORY_SORT_KEYS:
            raise ValueError(f"unknown sort key {sort_key!r}; expected one of {HISTORY_SORT_KEYS}")
        self.sort_key = sort_key
        self.descending = descending
        self._reset_page()

    def _reset_page(self) -> None:
        """Return the cursor to page 0 after a filter or sort change."""
        self._page_index = 0

    def page(self, n: int, *, filters=None, sort_key=None, descending=None) -> list[dict]:
        """Return the rows for absolute page ``n`` under the current filters and sort.

        Any of ``filters``/``sort_key``/``descending`` may override the active state for this one
        call; when omitted the session's current filters/sort are used. Changing the persistent
        filters or sort (via :meth:`set_filters`/:meth:`set_sort`) resets to page 0."""
        f = dict(filters) if filters is not None else self.filters or None
        key = sort_key if sort_key is not None else self.sort_key
        desc = descending if descending is not None else self.descending
        offset = n * self.page_size
        return self.store.query_runs(
            filters=f,
            sort_key=key,
            descending=desc,
            limit=self.page_size,
            offset=offset,
        )

    def next_page(self) -> list[dict]:
        """Advance one page from the current position; empty when there is no further page."""
        self._page_index += 1
        return self.page(self._page_index)

    def count(self) -> int:
        """Total number of runs matching the current filters (ignores paging)."""
        return len(
            self.store.query_runs(filters=self.filters or None, sort_key=self.sort_key, descending=self.descending)
        )

    # -- during a run -------------------------------------------------------

    def allow_during_run(self) -> bool:
        """Whether History may be used while a run is in progress (from Config, default True)."""
        return self.config.ui_allow_history_during_run if self.config is not None else True

    def set_run_in_progress(self, active: bool) -> None:
        """Mark a run as in progress or finished; the screen reads :meth:`available` to refresh.

        Rows still come from the store, so this only tracks the transient app-level state (a run that has
        started but not yet been closed through :meth:`RunStore.update_run_end`) — an open row is otherwise
        recognised as running by :meth:`is_running`."""
        self._run_in_progress = bool(active)

    def available(self) -> bool:
        """Whether the History screen can be used now.

        False only while a run is in progress and ``ui.allowHistoryDuringRun`` disables it."""
        return not (self._run_in_progress and not self.allow_during_run())

    def is_running(self, row: Mapping[str, Any]) -> bool:
        """True for a run with no result and no end_reason (still executing)."""
        return not row.get("result") and not row.get("end_reason")

    # -- a selected run -----------------------------------------------------

    def summary(self, run_uuid: str) -> dict[str, Any]:
        """A selected run's fields plus a count per ``Result`` and its checksum mismatches.

        The counts mirror :func:`ate.report.html.render_report` (header rows excluded; a step with no
        Attempt reads NOTRUN), so the panel matches the report it opens. ``checksum_mismatches`` is the
        list of file-used rows whose recorded checksum did not match."""
        row = self.store.get_test_run(run_uuid)
        if row is None:
            raise ValueError(f"no test run recorded for {run_uuid!r}")
        counts: dict[str, int] = {label: 0 for label in RESULT_LABELS}
        for node in self.store.nodes(run_uuid):
            if node.get("kind") == "header":
                continue
            result = node.get("result") or "NOTRUN"
            counts[result] = counts.get(result, 0) + 1
        mismatches = [f for f in self.store.run_files(run_uuid) if f.get("mismatch")]
        return {**row, "result_counts": counts, "checksum_mismatches": mismatches}

    def report_files(self, run_uuid: str) -> list[Path]:
        """This run's report files on disk (HTML and PDF), newest first.

        Empty when the reports directory is missing or no file has been written yet — that is what
        disables "Open report" for a freshly recorded run."""
        if self.config is None:
            raise ValueError("report_files needs a HistorySession constructed with a config")
        directory, stem = report_stem_and_directory(self.store, run_uuid, self.config)
        if not directory.exists():
            return []
        # PDFs are derived from the HTML; list them together, newest first overall (the PDF is written
        # after the HTML at run end, so it sorts ahead). The stable sort keeps HTML ahead of a same-
        # timestamp file so "Open report" prefers the viewable one when mtimes tie.
        htmls = sorted(directory.glob(f"{stem}*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        pdfs = sorted(directory.glob(f"{stem}*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        return sorted([*htmls, *pdfs], key=lambda p: p.stat().st_mtime, reverse=True)

    # -- run log export -------------------------------------------------------

    def export_owner_choices(self, run_uuid: str) -> list[tuple[str, str]]:
        """This run's export-dialog Owner checkboxes, in tree order: Run, each Pre-/Post-test
        Action, then the steps.

        Each entry is the exporter's owner key — ``"run"``, ``"pretest:<n>"``, ``"posttest:<n>"`` or
        ``"step:<n>"`` — with its display label; a step reads ``"<Step Number> <Step Name>"`` (e.g.
        ``"1. Step name"``) and brings all of its Attempts through its key. Raises
        :class:`ValueError` if no run is recorded for ``run_uuid``."""
        row = self.store.get_test_run(run_uuid)
        if row is None:
            raise ValueError(f"no test run recorded for {run_uuid!r}")
        choices = [("run", "Run")]
        for phase in ("pre", "post"):
            phase_actions = [a for a in self.store.actions(run_uuid) if a.get("phase") == phase]
            phase_actions.sort(key=lambda a: a["seq"])  # recorded execution order within the phase
            for i, action in enumerate(phase_actions, 1):
                label = "Pre-test" if phase == "pre" else "Post-test"
                if len(phase_actions) > 1:
                    label = f"{label} {i}"
                key = f"{'pretest' if phase == 'pre' else 'posttest'}:{action['instance_seq']}"
                choices.append((key, label))
        nodes = [n for n in self.store.nodes(run_uuid) if n.get("kind") == "step" and n.get("node_seq") is not None]
        nodes.sort(key=lambda n: n["node_seq"])  # tree order as the schedule expanded them
        for node in nodes:
            number = (node.get("step_number") or "").strip()
            name = (node.get("step_name") or "").strip()
            label = f"{number} {name}".strip()
            if not label:
                label = f"Step {node['node_seq']}"
            choices.append((f"step:{node['node_seq']}", label))
        return choices

    def export_log_default_path(self, run_uuid: str) -> Path:
        """The default Save As path for this run's log: the report folder, the run's report
        name (``<stem>_<RESULT>``) and ``_log.csv`` (spec 24b Saving) — the same name the
        run's report gets, with ``_log`` added. Raises :class:`ValueError` without a config
        or an unknown run."""
        if self.config is None:
            raise ValueError("export_log_default_path needs a HistorySession constructed with a config")
        row = self.store.get_test_run(run_uuid)
        if row is None:
            raise ValueError(f"no test run recorded for {run_uuid!r}")
        stem, result = report_stem_and_directory(self.store, run_uuid, self.config)[0].parent, result_label(row)  # noqa: E501
        directory = report_stem_and_directory(self.store, run_uuid, self.config)[0]
        return directory / f"{stem}_{result}_log.csv"

    def export_log(
        self,
        run_uuid: str,
        path: str,
        fmt: str,
        *,
        levels: Optional[Iterable[str]] = None,
        sources: Optional[Iterable[str]] = None,
        owners: Optional[Iterable[str]] = None,
    ) -> int:
        """Write this run's exported log to ``path`` in ``fmt`` (the dialog's chosen format); the
        dialog's ticks are the filters. Passes straight through to the exporter and returns the
        row count."""
        return export_log(self.store, run_uuid, path, fmt, levels=levels, sources=sources, owners=owners)

    def regenerate(
        self,
        run_uuid: str,
        *,
        html: bool = True,
        pdf: bool = False,
        include_run_log: bool = True,
        parent: Optional[object] = None,
        on_done: Optional[Callable[[Optional[str]], None]] = None,
    ) -> dict[str, Any]:
        """Write this run's report(s).

        The HTML is written now; a PDF (if requested) renders in the helper process without blocking.
        ``parent`` owns that QProcess and receives completion through ``on_done(error_or_None)`` when
        it finishes — both are optional so the synchronous HTML path stays testable without Qt. Raises
        if no config was supplied."""
        if self.config is None:
            raise ValueError("regenerate needs a HistorySession constructed with a config")
        written: dict[str, Any] = {"html": None}
        try:
            if html:
                written["html"] = write_report(self.store, run_uuid, self.config, include_run_log=include_run_log)
        except Exception as exc:  # noqa: BLE001 - surface the failure through on_done when a caller supplies one
            if on_done is not None:
                on_done(str(exc))
            else:
                raise
            return written

        pdf_started = False
        if pdf and html:
            # Render from the freshly written HTML (its exact path, including any -2/-3 suffix), so
            # the PDF always sits beside the report that was actually written.
            pdf_path = Path(written["html"]).with_suffix(".pdf")
            # Imported here so history.session stays Qt-free at load time; only needed for a PDF job.
            from ..report.pdf_job import start_pdf_job

            try:
                start_pdf_job(parent, html_path=written["html"], pdf_path=pdf_path, paper=self.config.reports_paper_size, on_done=on_done)
                pdf_started = True
            except Exception as exc:  # noqa: BLE001 - a PDF-helper error is reported through on_done, never raised into the caller
                if on_done is not None:
                    on_done(str(exc))

        return {"html": written["html"], "pdf_started": pdf_started}

    def uut_filter(self, run_uuid: str) -> dict[str, Any]:
        """Filters selecting every run of this run's UUT Type and serial ("Show all runs of this UUT")."""
        row = self.store.get_test_run(run_uuid)
        if row is None:
            raise ValueError(f"no test run recorded for {run_uuid!r}")
        return {"uut_type": row.get("uut_type") or "", "uut_serial": row.get("uut_serial") or ""}

    def new_run_prefill(self, run_uuid: str) -> dict[str, Any]:
        """Prefill for a new run of this UUT (UUT Type, serial, Project and Test Schedule).

        The session has no notion of an Operator's selected schedule, so ``schedule_name`` is left
        empty — the app shell fills it from its own selection when it connects ``new_run_requested``."""
        row = self.store.get_test_run(run_uuid)
        if row is None:
            raise ValueError(f"no test run recorded for {run_uuid!r}")
        project = self.config.station_name if self.config is not None else ""
        return {"uut_type": row.get("uut_type") or "", "uut_serial": row.get("uut_serial") or "", "project": project, "schedule_name": ""}
