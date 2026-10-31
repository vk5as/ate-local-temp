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
  * The licence notices Qt and PySide6 require: the LGPL-3.0 and GPL-3.0 texts, a notice naming
    the Qt and PySide6 versions and where their source is, and THIRD_PARTY_NOTICES.txt generated
    from the wheels the build venv installed (packet 32a).
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
# spec still sweeps the tree). PyInstaller places a data file *inside* its destination
# directory, so the destination is the directory, not the file path.
resources = sorted(
    p
    for p in (SRC / "ate").rglob("*")
    if p.is_file() and p.suffix not in {".py", ".pyc"} and "__pycache__" not in p.parts
)
# Destinations are written with forward slashes: PyInstaller normalises them on every platform,
# and a Windows backslash in a spec is easy to misread as an escape.
datas = [(str(p), p.parent.relative_to(SRC).as_posix()) for p in resources]

# The build script writes the version it took from the git tag into the work directory and
# passes its path here, so `ate --version` in a frozen app reports the version the bundle was
# built from: a frozen app has no git to ask.
version_file = os.environ.get("ATE_BUILD_VERSION_FILE")
if version_file and Path(version_file).is_file():
    datas.append((version_file, "ate"))  # -> ate/version.txt

# Qt and PySide6 are LGPL: the bundle carries the licence texts, the notice that names them and
# where their source is, and the inventory of runtime dependencies the build venv installed
# (packet 32a). The build script generates them and points this spec at the directory.
notices_dir = os.environ.get("ATE_BUILD_NOTICES_DIR")
if notices_dir and Path(notices_dir).is_dir():
    datas.extend((str(path), "licences") for path in sorted(Path(notices_dir).iterdir()) if path.is_file())

# ------------------------------------------------------- QtWebEngine assets ----
# PyInstaller's own hooks (hook-PySide6.QtWebEngineCore and friends) collect the Qt libraries
# plus QtWebEngine's `QtWebEngineProcess` helper, its `.pak` resources, ICU data and
# translations, so the spec only has to make sure those modules are actually reached: the PDF
# helper imports QtWebEngineCore, and QtWebEngineWidgets is named so the view binding is in the
# bundle for anything that wants a page on screen. Qt requires AA_ShareOpenGLContexts to be set
# before the first QCoreApplication, so `ate _selftest` only checks the Core binding. NI-VISA is
# never bundled; pyvisa-py is, so TCPIP and USBTMC work with nothing installed.
hiddenimports = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtNetwork",  # WebEngine's request handling
    "shiboken6",  # PySide6's object-lifetime runtime: every PySide6 module links against it
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

excludes = [
    # Qt bindings ATE never imports: the GUI is QtCore/QtGui/QtWidgets, the report renderer
    # QtWebEngineCore/Widgets, and pyvisa-py QtNetwork. Every Qt shared library that QtWebEngine
    # is actually linked against (QtQml, QtQuick, QtPositioning, QtPrintSupport, ...) is still
    # collected by PyInstaller's own dependency walk, so dropping these bindings costs only their
    # own ``*.abi3.so``. NI-VISA is never bundled either: ATE installs no NI-VISA, so pyvisa falls
    # back to pyvisa-py (see packaging.md).
    #
    # What must NOT be here: a module the WebEngine binding imports on its own. PyQt6 never did
    # this, PySide6 does — `PySide6.QtWebEngineWidgets` reaches `PySide6.QtWebChannel`, so
    # excluding it produced a bundle where `ate _pdf` died on "libshiboken: could not import
    # module 'PySide6.QtWebChannel'" with the helper binary and every .pak still in place
    # (2026-11-01). tests/test_pyinstaller_build_27a.py checks the list against what the
    # WebEngine import actually pulls in.
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCanvasPainter",
    "PySide6.QtCharts",
    "PySide6.QtConcurrent",
    "PySide6.QtDataVisualization",
    "PySide6.QtDBus",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.QtXml",
]

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

# One-directory (onedir): the executable is separate from the collected libraries and data,
# which is what the installer, the tarball and the .deb all wrap.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # ATE is a CLI as well as a GUI; the console is where --version prints
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ate",
)
