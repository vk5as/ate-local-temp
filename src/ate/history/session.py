"""The History session finds past Test Runs (ticket 23a).

It is UI-free — no widgets, no event loop — and holds no SQL. Every query goes through
:class:`RunStore.query_runs`; this class only shapes the filters, sort order and paging.
Test Runs are append-only, so there is nothing to delete or edit here (the neighbouring
packets own the History screen's display of that fact)."""

from __future__ import annotations

from typing import Any, Mapping

from ..storage import HISTORY_SORT_KEYS
from ..storage import RunStore


class HistorySession:
    """Browse past runs newest-first, sorted and filtered, one page at a time.

    ``store`` is the only dependency; ``page_size`` defaults to 100 rows per fetch so a large
    database stays fast (rows still load page by page). Changing the filters or sort order resets
    to the first page."""

    def __init__(self, store: RunStore, *, page_size: int = 100) -> None:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.store = store
        self.page_size = page_size
        # Sort order defaults to newest first.
        self.sort_key = "started_at"
        self.descending = True
        # Filters live here as a plain mapping; the store builds the SQL from them.
        self.filters: dict[str, Any] = {}
        # Cursor into the current sort/filter state (0-based page index).
        self._page_index = 0

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
