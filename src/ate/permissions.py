r"""Station filesystem/permissions (ticket 26b).

``ate setup`` prepares a Station for every install format, and on some platforms that means more
than creating directories: ordinary users must be able to write the database, its WAL files, the
``reports`` and ``logs`` trees, while ``config.json`` — and the config directory itself on Linux —
stays admin-only. This module is the injectable seam those privileged operations go through so that
tests never need root (see :func:`prepare_station` in :mod:`ate.setup`).

On Linux a system ``ate`` group carries the group ownership; on Windows an ``icacls`` grant does.
Each concrete implementation (:class:`LinuxPermissions`, :class:`WindowsPermissions`) provides only
its platform's methods and raises :class:`NotImplementedError` for the other platform's —
:func:`prepare_station` calls just the ones it needs, so a Linux object never runs ``icacls`` and
vice versa. The recording fake (:class:`RecordingPermissions`) implements every method and records
each call, so seam-2 tests can assert exactly which path received which treatment.

Every privileged command is issued with :func:`subprocess.run` over an argument list (never a shell
string) with ``check=True``; the Linux implementation never touches the real system's groups during
tests because those commands are captured by the test's patched subprocess.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# The group that carries ordinary users' modify rights on Linux. Adding Operators to it is a manual
# admin step (see ``assets/packaging.md``); this packet only creates and uses the group itself.
ATE_GROUP = "ate"

# Passed by :func:`prepare_station` when the caller explicitly opts out of privileged setup —
# ``ate setup --no-permissions`` or an unprivileged run. No directories are skipped, only the
# group/ownership/ACL treatment; tests never use this sentinel, they inject a fake instead.
SKIP_PERMISSIONS = object()


@runtime_checkable
class Permissions(Protocol):
    r"""The seam :func:`prepare_station` drives through.

    Linux implementations grant ordinary users modify rights on the data tree (database, WAL files,
    reports and logs) while keeping ``config.json`` — and the config directory — admin-only; a system
    ``ate`` group carries the ownership. Windows implementations use ``icacls`` to give Users Modify
    on the data directories and read-only access to ``config.json``.

    A concrete implementation provides only its platform's methods (the unused ones raise
    :class:`NotImplementedError`); :func:`prepare_station` calls just those it needs for the running
    platform, so a Linux object need not implement the Windows ones. The recording fake implements
    every method so tests can assert which path received which treatment regardless of platform.
    """

    def ensure_group(self, name: str) -> None: ...
    def set_group_writable(self, path: Path, group: str, *, recursive: bool = True) -> None: ...
    def set_admin_only(self, path: Path) -> None: ...
    def grant_users_modify(self, path: Path) -> None: ...
    def admin_only(self, path: Path) -> None: ...


class LinuxPermissions(Permissions):
    """Real Linux permissions via ``getent``/``groupadd``/``chgrp``/``chmod``."""

    def ensure_group(self, name: str) -> None:
        """Create the group only if it is missing (``groupadd --system <name>``)."""
        result = subprocess.run(["getent", "group", name], capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            subprocess.run(["groupadd", "--system", name], check=True)

    def set_group_writable(self, path: Path, group: str, *, recursive: bool = True) -> None:
        """Make ``path`` and its contents group-owned and group-writable.

        Directories also get the setgid bit so new files (``ate.db-wal``/``-shm``, fresh reports)
        inherit the group rather than dropping it on creation.
        """
        path = Path(path)
        if not path.exists():
            return
        if recursive:
            stack = [Path(path)]
            while stack:
                current = stack.pop()
                self._apply_entry(current, group, is_dir=current.is_dir())
                if current.is_dir():
                    for child in sorted(current.iterdir()):
                        stack.append(child)
        else:
            self._apply_entry(path, group, is_dir=path.is_dir())

    def _apply_entry(self, path: Path, group: str, *, is_dir: bool) -> None:
        subprocess.run(["chgrp", group, str(path)], check=True)  # ownership to the group
        if is_dir:
            # group write + setgid so descendants inherit both the group and writability
            subprocess.run(["chmod", "g+ws", str(path)], check=True)
        else:
            subprocess.run(["chmod", "g+w", str(path)], check=True)

    def set_admin_only(self, path: Path) -> None:
        """Owner root; ``config.json`` 0644 (readable by all, writable only by root), the dir 0755."""
        path = Path(path)
        if not path.exists():
            return
        subprocess.run(["chown", "root:root", str(path)], check=True)  # owner root
        mode = "0755" if path.is_dir() else "0644"
        subprocess.run(["chmod", mode, str(path)], check=True)

    def grant_users_modify(self, path: Path) -> None:  # Windows-only; unused on Linux
        raise NotImplementedError("grant_users_modify is a Windows operation")

    def admin_only(self, path: Path) -> None:  # Windows-only; unused on Linux
        raise NotImplementedError("admin_only is a Windows operation")


class WindowsPermissions(Permissions):
    """Real Windows permissions via ``icacls``."""

    # Well-known SIDs, so the commands work on any Windows display language.
    USERS, ADMINISTRATORS, SYSTEM = "*S-1-5-32-545", "*S-1-5-32-544", "*S-1-5-18"

    def grant_users_modify(self, path: Path) -> None:
        """Users get Modify on a data directory; ``(OI)(CI)`` makes it inherit to every file and
        subdirectory, existing (icacls propagates it) and future (the database's -wal/-shm, reports)."""
        subprocess.run(["icacls", str(path), "/grant", f"{self.USERS}:(OI)(CI)M"], check=True)

    def admin_only(self, path: Path) -> None:
        """``config.json``: stop inheriting the data directory's Users Modify grant (on Windows the
        config lives in the same directory) and set an explicit ACL: Administrators and SYSTEM
        full control, Users read."""
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r",
             f"{self.ADMINISTRATORS}:F", f"{self.SYSTEM}:F", f"{self.USERS}:R"],
            check=True,
        )

    def ensure_group(self, name: str) -> None:  # Linux-only; unused on Windows
        raise NotImplementedError("ensure_group is a Linux operation")

    def set_group_writable(self, path: Path, group: str, *, recursive: bool = True) -> None:
        raise NotImplementedError("set_group_writable is a Linux operation")

    def set_admin_only(self, path: Path) -> None:  # Linux-only; unused on Windows
        raise NotImplementedError("set_admin_only is a Linux operation")


class RecordingPermissions(Permissions):
    """A :class:`Permissions` double that records every call instead of touching the filesystem.

    Used by seam-2 tests to assert exactly which path received which treatment (for example, that
    ``config.json`` is never made group-writable) and that a second run repeats the same operations —
    without needing root or a real platform.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _record(self, op: str, **fields: Any) -> dict[str, Any]:
        entry = {"op": op}
        entry.update(fields)
        self.calls.append(entry)
        return entry

    def ensure_group(self, name: str) -> None:
        self._record("ensure_group", group=name)

    def set_group_writable(self, path: Path, group: str, *, recursive: bool = True) -> None:
        self._record("set_group_writable", path=str(path), group=group, recursive=recursive)

    def set_admin_only(self, path: Path) -> None:
        self._record("set_admin_only", path=str(path))

    def grant_users_modify(self, path: Path) -> None:
        self._record("grant_users_modify", path=str(path))

    def admin_only(self, path: Path) -> None:
        self._record("admin_only", path=str(path))

    @property
    def group_writable_paths(self) -> list[str]:
        """Paths handed group-writable rights (``set_group_writable``)."""
        return [c["path"] for c in self.calls if c["op"] == "set_group_writable"]

    @property
    def admin_only_paths(self) -> list[str]:
        """Paths made admin-only (``set_admin_only`` / ``admin_only``)."""
        return [c["path"] for c in self.calls if c["op"] in ("set_admin_only", "admin_only")]


def _current_platform() -> str:
    """This process's platform label ("linux" or "windows"), used to pick the permissions impl."""
    return "windows" if os.name == "nt" else "linux"


def real_permissions_for(platform: str) -> Permissions:
    """The real :class:`Permissions` for ``platform`` (Linux or Windows)."""
    if platform == "windows":
        return WindowsPermissions()
    return LinuxPermissions()
