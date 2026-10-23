"""Storage: the only place ATE talks to SQL.

Public interface is :class:`RunStore` plus :data:`SCHEMA_VERSION`, and — for launch —
:class:`check_database_at_launch` over :class:`DbLaunchResult`. The tables live in
:mod:`ate.storage.schema`; nothing outside this package imports them directly.
"""

from __future__ import annotations

from .schema import SCHEMA_VERSION
from .store import (
    DbLaunchResult,
    DbLaunchStatus,
    HISTORY_SORT_KEYS,
    RunStore,
    check_database_at_launch,
)
from ._uuid import new_uuid7

__all__ = [
    "RunStore",
    "HISTORY_SORT_KEYS",
    "SCHEMA_VERSION",
    "new_uuid7",
    "DbLaunchResult",
    "DbLaunchStatus",
    "check_database_at_launch",
]
