# Example Media Release

`media-release/` is a Test Definition (`example.atedef`) whose Test Schedules demonstrate Test
Schedule features: schedule metadata, Step Numbers and `setEnable` defaults, every `Result` value,
`NOREPORT`, a Lua `ERROR`, `ATE.log` at every level, `print`, `ATE.sleep`, `ATE.addEquipment` and
reading `ATE.run`, every Step Input type and widget, Retry under Stop on Fail, Pre-test and
Post-test Actions across a Sub-schedule tree, hidden Sub-schedules with `keyvals` and `setPrefix`,
nested includes, modules loaded with `require`, and every Device Type - `TCP`, `UDP`, `SERIAL` and
`VISA` - against simulator peers. The Test Report comes in the next packet.

| Schedule | Test Schedule name | What it demonstrates |
| --- | --- | --- |
| `schedule_basics.tslua` | Example basics | metadata, Step Numbers, `setEnable`, logging, `ATE.sleep`, `ATE.addEquipment`, `ATE.run`, one blocking `FLOAT` input |
| `report_results.tslua` | Report every Result | every `Result` value, `NOREPORT`, a Lua `ERROR` |
| `stop_on_fail.tslua` | Stop on fail | the Stop-on-Fail pause and the Operator's Continue/Abort choice |
| `step_inputs.tslua` | Step inputs | `ENUM` as `RADIOBUT`, `CHECKBOX` and `PUSHBUT`, `INTEGER` and `FLOAT` with a range, `STRING` with a pattern, a blocking `PROMPT`, a `POLL` read in a loop, and `tooltip`/`orient` |
| `flow.tslua` | Flow and retry | Retry after a failed Attempt, Pre/Post-test Actions on the schedule and its includes, `setInclude` with `keyvals` and `setPrefix`, a nested include, `setEnable` on a header, and the `ate.devices` builders |
| `devices.tslua` | Device types | `TCP`, `UDP`, `SERIAL` and `VISA` each opened, written, read, flushed and closed: a partial read, a read that times out, `onError` handlers that continue and that return `nil`, per-call overrides, and a Device opened in the Pre-test Action and used by later steps |

The Sub-schedules it includes are `hidden`, so the Operator never picks them as Test Schedules:

| File | Included from | What it demonstrates |
| --- | --- | --- |
| `sub_channel.tslua` | `flow.tslua`, four times: three enabled, one under a disabled header | `keyvals` per include instance, `setPrefix`, Pre/Post-test Actions on an included schedule |
| `sub_device.tslua` | `devices.tslua`, once per Device Type | one Device opened, written, read, flushed and closed: `open`'s `onError` as the fallback for its later calls, a partial read, a read that times out, a flush |
| `nest_a.tslua` | `flow.tslua` | a nested include, and sharing one required module across the tree |
| `nest_b.tslua` | `nest_a.tslua` | prefixes stacking through nesting |
| `nest_c.tslua` | `nest_b.tslua` | the deepest level, whose Actions run before the rest of the chain's in the Post-test phase |

Helper modules, loaded with the standard `require`:

| File | Module name | How the name is decided | Required by |
| --- | --- | --- | --- |
| `lib/attempts.lua` | `lib.attempts` | derived from the path | `flow.tslua`, `sub_channel.tslua`, `nest_a.tslua` |
| `modules/helper.lua` | `example_helper` | the `module` field in `example.atedef` | `flow.tslua`, `sub_channel.tslua` |

Both are required by more than one schedule in the tree, and `require` is cached for the Test Run,
so every schedule that loads one sees the same table - the run-wide use counts the harness asserts.

The checksums in `example.atedef` are written by the real CLI, never by hand:

```sh
PYTHONPATH=src python -m ate.cli definition update examples/media-release/example.atedef
```

Point any config's `testDefinitions.directories` at `examples/media-release/` to use it. It lives
outside `src/`, so it is not installed with the package.

## The Device peers

`devices.tslua` needs no hardware. `harness/peers.py` ships two small simulators, which
`harness/run_examples.py` starts on ephemeral ports before the runs and stops afterwards:

| Peer | What it is | What it answers |
| --- | --- | --- |
| `ScpiPeer` | a line-oriented SCPI simulator over TCP | `*IDN?` and `MEAS:VOLT?` reply, `PARTIAL?` replies without a terminator, `MULTI?` replies with three lines, `NEVER?` gets no reply at all |
| `UdpEchoPeer` | a UDP listener | echoes every datagram as `ECHO:<datagram>`, except one starting `DROP:`, which it records and ignores |

The same SCPI peer is reached three ways: directly as a `TCP` Device, as a `SERIAL` Device through
pyserial's `socket://host:port` URL, and as a `VISA` instrument through pyvisa-py's
`TCPIP::host::port::SOCKET` resource.

The schedule's addresses are its own configuration: a `bench` table at the top of `devices.tslua`
and the `keyvals` of its four includes. `bench` reads the environment first and falls back to the
usual instrument ports, so the harness can start its peers on ephemeral ports without rewriting the
file:

| Variable | Default | Used for |
| --- | --- | --- |
| `ATE_SCPI_HOST` | `127.0.0.1` | the SCPI instrument's address |
| `ATE_SCPI_PORT` | `5025` | the usual SCPI-over-TCP port |
| `ATE_UDP_HOST` | `127.0.0.1` | the datagram peer's address |
| `ATE_UDP_PORT` | `5026` | the peer in this example's echo protocol |

The `SERIAL` port (`socket://host:port`) and the `VISA` resource
(`TCPIP0::host::port::SOCKET`) are both derived from `ATE_SCPI_HOST` and `ATE_SCPI_PORT`, and the
harness exports these four variables before it runs, so the shipped bytes run unchanged.

To run the schedule against real instruments, set those variables or edit `bench` and the keyvals:

- `scpiHost`/`scpiPort` (or `ATE_SCPI_HOST`/`ATE_SCPI_PORT`) - the LAN instrument that answers SCPI
  on, say, port 5025. It must answer the same five questions, so the steps' expectations still hold:
  reply to `*IDN?` and `MEAS:VOLT?`, answer `PARTIAL?` with bytes and no newline, `MULTI?` with three
  newline-terminated lines, and stay silent for `NEVER?`. The read that uses a per-call `readTerm`
  expects `MEAS:VOLT?` to answer `1.234\n`, and the `*IDN?` reply is checked against the keyval
  `reply`, so change those to what your instrument actually says.
- `udpHost`/`udpPort` - a peer that answers each datagram it receives. Change
  `silentCommand = "DROP:no reply"` to whatever the peer ignores, and `reply` to what it echoes.
- `serialPort` - a real port name (`"COM3"`, `"/dev/ttyUSB0"`) instead of pyserial's `socket://` URL;
  the baud in the keyvals is the port's own.
- `visaResource` - a real VISA resource, e.g. `"GPIB0::5::INSTR"` (which needs a VISA backend:
  NI-VISA, or pyvisa-py for TCPIP and USBTMC).

## Running the schedules headless

`harness/run_examples.py` runs every visible schedule through the seam-1 headless run session with
scripted Operator answers, and `harness/test_examples.py` asserts each Test Run record:

```sh
QT_QPA_PLATFORM=offscreen python -m examples.harness.run_examples   # print a summary of every run
pytest examples/                                                   # the harness, with its assertions
```

`testpaths` in `pyproject.toml` includes `examples`, so the ordinary suite runs the harness too. The
harness config turns on `deviceTrace`, so every Device operation the Device schedule performs shows
up as a recorded `DEBUG` line with source `device`.
