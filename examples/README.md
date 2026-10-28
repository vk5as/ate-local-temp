# Example Media Release

`media-release/` is a Test Definition (`example.atedef`) whose Test Schedules demonstrate Test
Schedule features: schedule metadata, Step Numbers and `setEnable` defaults, every `Result` value,
`NOREPORT`, a Lua `ERROR`, `ATE.log` at every level, `print`, `ATE.sleep`, `ATE.addEquipment` and
reading `ATE.run`. Later packets add Step Inputs in depth, Sub-schedules, Devices and flow control.

The checksums in `example.atedef` are written by the real CLI, never by hand:

```sh
PYTHONPATH=src python -m ate.cli definition update examples/media-release/example.atedef
```

Point any config's `testDefinitions.directories` at `examples/media-release/` to use it. It lives
outside `src/`, so it is not installed with the package.

## Running the schedules headless

`harness/run_examples.py` runs every visible schedule through the seam-1 headless run session with
scripted Operator answers, and `harness/test_examples.py` asserts each Test Run record:

```sh
QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples   # print a summary of every run
pytest examples/                                                   # the harness, with its assertions
```

`testpaths` in `pyproject.toml` includes `examples`, so the ordinary suite runs the harness too.
