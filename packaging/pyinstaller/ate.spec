# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-directory spec for ATE (packet 27a).

Run through ``packaging/build_pyinstaller.py``, which makes a venv with the build's
CPython (3.13 here, 3.14 in CI) and calls PyInstaller on this file. Nothing here is
pinned to a CPython version, so the same recipe builds every frozen artifact: the Inno
installer (27b), the `.tar.gz` and the `.deb` (28b).

Output: ``dist/ate/`` with the ``ate`` executable next to its ``_internal`` directory.

What the bundle carries:
  * ATE's modules and the files it reads at runtime — the report template and the
    bundled ``ate.`` Lua modules (the same rule as ``[tool.setuptools.package-data]``).
  * QtWebEngine: the bindings plus the ``QtWebEngineProcess`` helper binary and its
    ``.pak``/ICU/V8 data, which no import graph reaches. NI-VISA is never bundled;
    pyvisa-py is, so TCPIP and USBTMC work with nothing installed (see
    ``.scratch/ate-v1-wayfinding/assets/packaging.md``).
  * lupa's native Lua runtime and every pyvisa-py protocol, which pyvisa loads by name.
"""

import os
from pathlib import Path

REPO = Path(SPECPATH).parents[1]  # packaging/pyinstaller/ate.spec -> repository root
SRC = REPO / "src"
ENTRY = Path(SPECPATH) / "ate_entry.py"

# ---------------------------------------------------------------- runtime data ----
# Every non-.py file under src/ate is read at runtime: the Jinja report template, the
# bundled Lua modules, and anything added later. Derived from the tree on purpose, so
# a new resource needs no change here (tests/test_pyinstaller_build_27a.py checks the
# spec still sweeps the tree).
resources = sorted(
    p
    for p in (SRC / "ate").rglob("*")
    if p.is_file() and p.suffix not in {".py", ".pyc"} and "__pycache__" not in p.parts
)
datas = [(str(p), str(p.relative_to(SRC))) for p in resources]

# The build script writes the tagged version out to build/ and passes its path here, so
# `ate --version` in a frozen app reports the release it was built from, not the fallback.
version_file = os.environ.get("ATE_BUILD_VERSION_FILE")
if version_file and Path(version_file).is_file():
    datas.append((version_file, "ate/version.txt"))

# ------------------------------------------------------- QtWebEngine assets ----
# PyInstaller's own hooks (hook-PyQt6.QtWebEngineCore and friends) collect the Qt libraries
# plus QtWebEngine's `QtWebEngineProcess` helper, its `.pak` resources, ICU data and
# translations, so the spec only has to make sure those modules are actually reached: the
# PDF helper imports QtWebEngineCore, and the GUI imports QtWebEngineWidgets. NI-VISA is
# never bundled; pyvisa-py is, so TCPIP and USBTMC work with nothing installed.
hiddenimports = [
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtNetwork",  # WebEngine's request handling
    "lupa.lua55",
    # pyvisa picks a backend by name at runtime: ResourceManager("@py") imports pyvisa_py,
    # which then reaches its protocol modules the same way. ATE never ships NI-VISA, so
    # pyvisa-py is the only backend in the bundle.
    "pyvisa_py",
    "pyvisa_py.attributes",
    "pyvisa_py.common",
    "pyvisa_py.highlevel",
    "pyvisa_py.sessions",
    "pyvisa_py.tcpip",
    "pyvisa_py.serial",
    "pyvisa_py.usb",
    "pyvisa_py.gpib",
    "pyvisa_py.prologix",
    "pyvisa_py.protocols",
    "pyvisa_py.protocols.hislip",
    "pyvisa_py.protocols.rpc",
    "pyvisa_py.protocols.vxi11",
    "pyvisa_py.protocols.xdrlib",
]

excludes = []  # nothing excluded: pyvisa imports its NI-VISA-only resources statically,
# and ATE simply never installs NI-VISA (see packaging.md), so pyvisa falls back to pyvisa-py.

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = Executable(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ate",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # ATE is a CLI as well as a GUI; the console is where --version prints
)
