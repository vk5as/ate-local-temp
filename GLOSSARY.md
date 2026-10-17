# ATE

ATE (Automated Test Execution) runs scripted tests against units, mostly hardware, records the results and reports on them.

## Language

**Project**:
The product or programme a Test Run is for, named by the Operator when the run starts; it is not a property of a Test Schedule or Media Release.
_Avoid_: Product, programme, job

**Operator**:
The person conducting a Test Run, identified by free-text name.
_Avoid_: User, tester, technician

**Station**:
One installation of ATE on one computer, identified by a generated unique ID and carrying a name; its identity is recorded on every Test Run.
_Avoid_: Rig, bench, tester

**Device**:
A piece of equipment, or a connection to a UUT, that a Test Step talks to through ATE, reached using connection details the Test Schedule supplies.
_Avoid_: Instrument, resource, port

**Equipment**:
A piece of test equipment a Test Run used, declared by the Test Schedule so its identity and calibration appear on the Test Report. Not the same as a Device: a Device is a connection ATE talks through, Equipment is a record of what was on the bench.
_Avoid_: Instrument, asset

**Device Type**:
The kind of connection ATE uses to reach a Device (for example serial), chosen from the `DeviceType` enum; it decides which connection details a Test Schedule must supply.
_Avoid_: Device class

**Test Definition**:
A file that names a Media Release and lists every Lua file belonging to it (Test Schedules and auxiliary files), each with its expected checksum.
_Avoid_: Manifest, package

**Media Release**:
An installed, released set of Lua files described by one Test Definition, identified by its media number and carrying a name and version; it may contain only helper code, and may depend on other Media Releases.
_Avoid_: Bundle, package

**Media Number**:
The configured artifact-storage number that uniquely identifies a Media Release; not a version.
_Avoid_: Release number, version

**UUT**:
Unit Under Test: the single physical item (or occasionally software artefact) being tested, identified by its UUT Type and serial number.
_Avoid_: DUT, device, board

**UUT Type**:
What kind of unit a UUT is, entered by the Operator for each Test Run alongside the serial number; distinct from the Project.
_Avoid_: Part number, model, product, UUT name

**Test Schedule**:
A Lua script that, when loaded, returns the ordered set of Test Steps to execute.
_Avoid_: Test script, sequence, procedure, test plan

**Test Step**:
One entry in the data structure a Test Schedule returns; the unit of execution and of recorded result.
_Avoid_: Test, test case, check

**Step Name**:
The optional descriptive name a Test Step, or a Sub-schedule's header row, is shown and reported under alongside its Step Number.
_Avoid_: Title, label

**Step Number**:
The free-form dotted identifier a Test Step is shown and reported under (e.g. "4.0.1"); not its execution position.
_Avoid_: Step ID, index

**Step Input**:
A value the Operator supplies during a Test Step, declared on that step.
_Avoid_: Parameter, field, prompt

**Sub-schedule**:
A Test Schedule whose Test Steps are pulled into another Test Schedule at a given step position, under a header row of its own. Each inclusion is a separate instance with its own parameters. Sub-schedules may nest.
_Avoid_: Include, child schedule, nested schedule

**Pre-test Action**:
Logic a Test Schedule runs once before the first Test Step of the Test Run. With Sub-schedules, every Pre-test Action runs up front, parent first.
_Avoid_: Setup, init

**Post-test Action**:
Logic a Test Schedule runs once after the last Test Step of the Test Run, in reverse Pre-test order, told the Test Run's overall result. It runs even after an abort.
_Avoid_: Teardown, cleanup

**Test Run**:
One execution of one Test Schedule against one UUT at one point in time.
_Avoid_: Execution, session, test

**Stop on Fail**:
The Test Run setting under which a failed Test Step pauses the run for the Operator to Continue, Retry or Abort. With it off, failures never pause the run.
_Avoid_: Halt on error, break on fail

**Attempt**:
One execution of a Test Step's action within a Test Run. A Retry adds an Attempt; the step's result is its last Attempt's.
_Avoid_: Try, iteration

**End Reason**:
How a Test Run ended (`COMPLETED`, `PRETEST_FAILED`, `STOPPED_ON_FAIL`, `ABORTED`, or `INTERRUPTED` when ATE stopped before the run could end), recorded alongside its pass/fail result.
_Avoid_: Exit status, termination

**Test Report**:
A document describing the outcome of exactly one Test Run, generated from stored results.
_Avoid_: Certificate, log
