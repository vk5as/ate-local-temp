# Example Media Release

`media-release/` is a Test Definition (`example.atedef`) whose Test Schedules demonstrate Test
Schedule features: schedule metadata, Step Numbers and `setEnable` defaults, every `Result` value,
`NOREPORT`, a Lua `ERROR`, `ATE.log` at every level, `print`, `ATE.sleep`, `ATE.addEquipment` and
reading `ATE.run`, and every Step Input type and widget. Later packets add Sub-schedules, Devices
and flow control.

| Schedule | Test Schedule name | What it demonstrates |
| --- | --- | --- |
| `schedule_basics.tslua` | Example basics | metadata, Step Numbers, `setEnable`, logging, `ATE.sleep`, `ATE.addEquipment`, `ATE.run`, one blocking `FLOAT` input |
| `report_results.tslua` | Report every Result | every `Result` value, `NOREPORT`, a Lua `ERROR` |
| `stop_on_fail.tslua` | Stop on fail | the Stop-on-Fail pause and the Operator's Continue/Abort choice |
| `step_inputs.tslua` | Step inputs | `ENUM` as `RADIOBUT`, `CHECKBOX` and `PUSHBUT`, `INTEGER` and `FLOAT` with a range, `STRING` with a pattern, a blocking `PROMPT`, a `POLL` read in a loop, and `tooltip`/`orient` |

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
