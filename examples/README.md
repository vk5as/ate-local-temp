# Example Media Release

`media-release/` is a Test Definition (`example.atedef`) whose Test Schedules demonstrate Test
Schedule features: schedule metadata, Step Numbers and `setEnable` defaults, every `Result` value,
`NOREPORT`, a Lua `ERROR`, `ATE.log` at every level, `print`, `ATE.sleep`, `ATE.addEquipment` and
reading `ATE.run`, every Step Input type and widget, Retry under Stop on Fail, Pre-test and
Post-test Actions across a Sub-schedule tree, hidden Sub-schedules with `keyvals` and `setPrefix`,
nested includes, and modules loaded with `require`. Devices and the Test Report come in later
packets.

| Schedule | Test Schedule name | What it demonstrates |
| --- | --- | --- |
| `schedule_basics.tslua` | Example basics | metadata, Step Numbers, `setEnable`, logging, `ATE.sleep`, `ATE.addEquipment`, `ATE.run`, one blocking `FLOAT` input |
| `report_results.tslua` | Report every Result | every `Result` value, `NOREPORT`, a Lua `ERROR` |
| `stop_on_fail.tslua` | Stop on fail | the Stop-on-Fail pause and the Operator's Continue/Abort choice |
| `step_inputs.tslua` | Step inputs | `ENUM` as `RADIOBUT`, `CHECKBOX` and `PUSHBUT`, `INTEGER` and `FLOAT` with a range, `STRING` with a pattern, a blocking `PROMPT`, a `POLL` read in a loop, and `tooltip`/`orient` |
| `flow.tslua` | Flow and retry | Retry after a failed Attempt, Pre/Post-test Actions on the schedule and its includes, `setInclude` with `keyvals` and `setPrefix`, a nested include, `setEnable` on a header, and the `ate.devices` builders |

The Sub-schedules it includes are `hidden`, so the Operator never picks them as Test Schedules:

| File | Included from | What it demonstrates |
| --- | --- | --- |
| `sub_channel.tslua` | `flow.tslua`, twice and one more time under a disabled header | `keyvals` per include instance, `setPrefix`, Pre/Post-test Actions on an included schedule |
| `nest_a.tslua` | `flow.tslua` | a nested include, and sharing one required module across the tree |
| `nest_b.tslua` | `nest_a.tslua` | prefixes stacking through nesting |
| `nest_c.tslua` | `nest_b.tslua` | the deepest level, whose Actions run first in the Post-test phase |

Helper modules, loaded with the standard `require`:

| File | Module name | How the name is decided |
| --- | --- | --- |
| `lib/attempts.lua` | `lib.attempts` | derived from the path |
| `modules/helper.lua` | `example_helper` | the `module` field in `example.atedef` |

Both are required by more than one schedule in the tree, and `require` is cached for the Test Run,
so all of them see one shared counter - which is what the harness asserts.

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
