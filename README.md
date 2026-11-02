# ATE — Automated Test Execution

ATE runs Lua 5.5 *Test Schedules* against hardware UUTs, records each run in a
local SQLite database, and renders an HTML/PDF report. It is a PySide6 desktop app.

## Scope

The application terms are defined in [GLOSSARY.md](GLOSSARY.md), and lasting
architecture choices are recorded in [docs/adr](docs/adr/).

## Architecture

Three layers, enforced by an import-lint test:

- **engine** (`src/ate/engine`) — run flow control and the Lua host. Imports no widgets.
- **run session** (`src/ate/run_session`) — the UI-free object a layout binds to (state + commands). Imports no widgets.
- **ui** (`src/ate/ui`) — layouts and screens (QtWidgets). This is where GUI code lives; nothing here runs logic.

The engine and run session reach the GUI only through queued Qt signals, so the
engine is testable without a display.

## Install & test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

`ate --version` prints the version baked from the git tag at build time (a safe
dev version when untagged).

Full source-build and wheel instructions, including `pipx install` and
`ate setup`, are in [docs/building-from-source.md](docs/building-from-source.md).

## Known limitation

Ticket 01 builds the skeleton only. The pinned lupa wheel cannot host Lua
schedules yet, so there is **no Lua runtime** in this checkout — later tickets
add it.
