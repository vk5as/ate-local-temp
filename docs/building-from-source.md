# Building ATE from source

Packet 28a: the wheel and the source build. The `.deb`, the `.tar.gz` with
`install.sh` and the Windows installer are separate packets; this page covers
running ATE from a checkout and building the wheel.

ATE targets CPython **3.13** (Debian stable's system Python) and **3.14** (the
version the frozen builds bundle). Anything older is refused by
`requires-python = ">=3.13"`.

## From a checkout

```bash
git clone <this repository> && cd ate-ornith
python3.13 -m venv .venv            # or python3.14
source .venv/bin/activate
pip install -e ".[test]"            # pytest, pytest-qt, and the build tooling (setuptools, build)
pytest                              # needs no display: QT_QPA_PLATFORM=offscreen helps on CI
```

Run it:

```bash
ate setup --root /tmp/ate-station   # creates the config, database and data directories
ate --version                       # the version baked from the git tag: the install check
```

`ate --version` is how you verify the install. Bare `ate` opens the GUI: it reads the config,
refuses a missing or mismatched database, and opens the main window with the Run screen.
`ate --config <path>` points the launch at a specific config file; `ate --help` and
`ate <subcommand> --help` print usage without running anything.

`ate setup` is best run as admin or root: elevated, it creates the `ate` group and sets
the ownership and permissions of the station directories. Run unprivileged it warns and
skips those steps (or force the skip with `--no-permissions`); the directories themselves
are still created. See the spec's Packaging section.

On Windows the venv paths are `.venv\Scripts\activate` instead.

## Building the wheel

```bash
python -m build --wheel             # or: pip wheel --no-deps -w dist .
```

The wheel is `dist/ate-<version>-py3-none-any.whl`, where `<version>` comes from
`git describe --tags` (`0.0.0-dev` when there is no tag). It carries the report
template, the bundled Lua modules and every other non-`.py` file under `src/ate`;
`tests/test_wheel_build_28a.py` fails if a resource is ever left out.

Install it the way a Station operator would:

```bash
pipx install dist/ate-0.0.0.dev0-py3-none-any.whl
ate setup
ate --version
```

`pipx` puts `ate` on the PATH in its own virtual environment; `pip install` into
an existing venv works the same way. Dependencies come from PyPI, so no wheel
step is needed for them.

## Tests

```bash
PYTHONPATH=src QT_QPA_PLATFORM=offscreen pytest -q
```

The wheel test builds a wheel from a clean copy into a temporary directory and takes a few
seconds. The only test marked `slow` is the one that installs the wheel into a fresh venv
from PyPI: `pytest -m "not slow"` deselects just that one.
