# ATE

ATE (Automated Test Execution) runs scripted tests against units, mostly hardware, records the results and reports on them.

## Language

**Project**:
The product or programme a UUT belongs to; Test Schedules are grouped under it.
_Avoid_: Product, programme, job

**Operator**:
The person conducting a Test Run, identified by free-text name.
_Avoid_: User, tester, technician

**Station**:
One installation of ATE on one computer; its identity is recorded on every Test Run.
_Avoid_: Rig, bench, tester

**Device**:
A piece of equipment, or a connection to a UUT, that a Test Step talks to through ATE, reached using connection details the Test Schedule supplies.
_Avoid_: Instrument, resource, port

**Device Type**:
The kind of connection ATE uses to reach a Device (for example serial), chosen from the `DeviceType` enum; it decides which connection details a Test Schedule must supply.
_Avoid_: Device class

**Test Definition**:
A file that names a Media Release and lists every Lua file belonging to it (Test Schedules and auxiliary files), each with its expected checksum.
_Avoid_: Manifest, package

**Media Release**:
A numbered, named, released set of Lua files, as identified by a Test Definition.
_Avoid_: Version, bundle

**UUT**:
Unit Under Test: the single physical item (or occasionally software artefact) being tested, identified by its serial number.
_Avoid_: DUT, device, board

**Test Schedule**:
A Lua script that, when loaded, returns the ordered set of Test Steps to execute.
_Avoid_: Test script, sequence, procedure, test plan

**Test Step**:
One entry in the data structure a Test Schedule returns; the unit of execution and of recorded result.
_Avoid_: Test, test case, check

**Step Number**:
The free-form dotted identifier a Test Step is shown and reported under (e.g. "4.0.1"); not its execution position.
_Avoid_: Step ID, index

**Step Input**:
A value the Operator supplies during a Test Step, declared on that step.
_Avoid_: Parameter, field, prompt

**Sub-schedule**:
A Test Schedule whose Test Steps are pulled into another Test Schedule at a given step position.
_Avoid_: Include, child schedule, nested schedule

**Pre-test Action**:
Logic a Test Schedule runs once before its first Test Step.
_Avoid_: Setup, init

**Post-test Action**:
Logic a Test Schedule runs once after its Test Steps, told the Test Run's overall result.
_Avoid_: Teardown, cleanup

**Test Run**:
One execution of one Test Schedule against one UUT at one point in time.
_Avoid_: Execution, session, test

**Test Report**:
A document describing the outcome of exactly one Test Run, generated from stored results.
_Avoid_: Certificate, log
