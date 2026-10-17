"""PROTOTYPE, throwaway. Checks the Lua runtime design from "Choose the Lua runtime" on this machine.

python lua_worker_check.py           # interactive window: run scenarios, click Continue / Abort yourself
python lua_worker_check.py --auto    # runs every check unattended, prints PASS/FAIL, exits 0 if all pass

Design under test: lupa.lua55 on a QThread worker (worker object moved onto it), a fresh LuaRuntime
per run created on that thread, register_eval/register_builtins off, dofile/loadfile/load removed,
blocking ATE calls that also wait on an abort flag, a count hook every 1000 instructions, and
xpcall tracebacks. The GUI thread runs a 10 ms heartbeat; its longest gap is the GUI stall.
"""
import os
import platform
import sys
import threading
import time

import lupa
from lupa.lua55 import LuaRuntime
from PyQt6.QtCore import QT_VERSION_STR, QObject, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                             QVBoxLayout, QWidget)

# Lua side of ATE. Python functions return (ok, value) and the Lua shim raises with error(msg, 2):
# a Python exception reaching Lua directly arrives as userdata, not a string (see check 7).
LUA_PRELUDE = r"""
local py = ...
dofile, loadfile, load = nil, nil, nil
python = nil  -- register_builtins=False still leaves a python table (as_function, iter, ...)
local aborted = py.aborted
debug.sethook(function() if aborted() then error("aborted", 0) end end, "", 1000)

-- the hook's error is catchable, so a schedule's own pcall/xpcall would swallow an abort (check
-- abort_pcall hung forever without this): after an abort, both re-raise it
local raw_pcall, raw_xpcall = pcall, xpcall
local function reraise(...)
  if aborted() then error("aborted", 0) end
  return ...
end
pcall = function(...) return reraise(raw_pcall(...)) end
xpcall = function(...) return reraise(raw_xpcall(...)) end

local function wrap(name, fn)
  return function(...)
    local ok, v = fn(...)
    if not ok then
      if v == "aborted" then error("aborted", 0) end
      error(name .. ": " .. tostring(v), 2)   -- level 2: blame the schedule line that called ATE
    end
    return v
  end
end
ATE = {
  input = wrap("ATE.input", py.input),
  sleep = wrap("ATE.sleep", py.sleep),
  read  = wrap("ATE.read", py.read),
  log   = function(...) py.log(table.concat({...}, " ")) end,
}
ATE_raw_read = py.read_raises    -- deliberately unwrapped, for check 7

-- every step runs through this
return function(fn)
  local ok, err = raw_xpcall(fn, debug.traceback)
  if ok then return true, err end
  return false, tostring(err)
end
"""

SCENARIOS = {
    "busy": ("Busy loop, 3 s of pure Lua",
             "local t0 = os.clock(); local n = 0; while os.clock() - t0 < 3 do n = n + 1 end; return n"),
    "input": ("ATE.input blocks until Continue is clicked",
              "ATE.log('waiting for input 1'); local v = ATE.input(1); ATE.log('got ' .. v); return v"),
    "abort_busy": ("Endless Lua loop; click Abort",
                   "while true do end"),
    "abort_pcall": ("Endless loop that pcalls an endless loop; click Abort",
                    "local n = 0; while true do n = n + 1; pcall(function() while true do end end) end"),
    "abort_xpcall": ("Endless loop that xpcalls an endless loop; click Abort",
                     "while true do xpcall(function() while true do end end, debug.traceback) end"),
    "abort_input": ("ATE.input with no Continue; click Abort",
                    "return ATE.input(1)"),
    "lua_error": ("Lua runtime error two calls deep",
                  "local function measure(ch)\n  local cfg = nil\n  local r = cfg.range[ch]\n  return r\nend\n"
                  "local function step()\n  local v = measure(3)\n  return v\nend\nlocal v = step()\nreturn v"),
    "py_error": ("Python exception inside ATE.read",
                 "local function step()\n  local v = ATE.read('dmm')\n  return v\nend\nlocal v = step()\nreturn v"),
    "py_error_raw": ("Python exception reaching Lua unwrapped",
                     "return ATE_raw_read('dmm')"),
    "locked": ("Loading is locked down",
               "return tostring(dofile) .. ' ' .. tostring(loadfile) .. ' ' .. tostring(load) .. ' ' .. tostring(python)"),
}


class Worker(QObject):
    log = pyqtSignal(str)
    waiting = pyqtSignal(bool)
    finished = pyqtSignal(str, bool, str, float)  # scenario, ok, value or error, seconds

    def __init__(self):
        super().__init__()
        self.abort = threading.Event()
        self.answer = threading.Event()
        self.answer_value = None

    # --- Python halves of the ATE functions; they run on the worker thread
    def _input(self, n):
        self.waiting.emit(True)
        try:
            while not self.answer.wait(0.05):
                if self.abort.is_set():
                    return False, "aborted"
            self.answer.clear()
            return True, self.answer_value
        finally:
            self.waiting.emit(False)

    def _sleep(self, s):
        return (False, "aborted") if self.abort.wait(s) else (True, None)

    def _read(self, dev):
        try:
            raise ConnectionError(f"device '{dev}' not open")
        except Exception as e:  # real ATE also writes the Python traceback to the application log
            return False, str(e)

    def _read_raises(self, dev):
        raise ConnectionError(f"device '{dev}' not open")

    @pyqtSlot(str)
    def run(self, name):
        self.abort.clear()
        self.answer.clear()
        t0 = time.perf_counter()
        # fresh runtime per run, created on this thread
        # unpack_returned_tuples: without it a Python (ok, value) tuple reaches Lua as one userdata
        lua = LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=True)
        py = lua.table_from({
            "aborted": self.abort.is_set, "input": self._input, "sleep": self._sleep,
            "read": self._read, "read_raises": self._read_raises,
            "log": lambda s: self.log.emit(f"  [lua] {s}"),
        })
        runner = lua.execute(LUA_PRELUDE, py, name="=ate-prelude", mode="t")
        # chunk named like a real schedule file, so tracebacks show file:line; mode="t" refuses bytecode
        fn = lua.execute(f"return function()\n{SCENARIOS[name][1]}\nend", name="@demo/step.tslua", mode="t")
        ok, val = runner(fn)
        self.finished.emit(name, bool(ok), str(val), time.perf_counter() - t0)


class Window(QWidget):
    start = pyqtSignal(str)

    def __init__(self, auto):
        super().__init__()
        self.setWindowTitle("PROTOTYPE: Lua worker check")
        self.auto = auto
        self.thread = QThread()
        self.worker = Worker()
        self.worker.moveToThread(self.thread)
        self.start.connect(self.worker.run)  # queued: crosses threads
        self.worker.log.connect(self.say)
        self.worker.waiting.connect(self.on_waiting)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()

        self.beats = 0
        self.last = time.perf_counter()
        self.max_gap = 0.0
        self.hb = QTimer(interval=10, timeout=self.beat)
        self.hb.start()

        lay = QVBoxLayout(self)
        self.alive = QLabel()
        lay.addWidget(self.alive)
        row = QHBoxLayout()
        for key, (label, _) in SCENARIOS.items():
            b = QPushButton(key)
            b.setToolTip(label)
            b.clicked.connect(lambda _, k=key: self.run(k))
            row.addWidget(b)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.cont = QPushButton("Continue (answer input)")
        self.cont.setEnabled(False)
        self.cont.clicked.connect(self.click_continue)
        self.abort_b = QPushButton("Abort")
        self.abort_b.clicked.connect(self.click_abort)
        row2.addWidget(self.cont)
        row2.addWidget(self.abort_b)
        lay.addLayout(row2)
        self.out = QPlainTextEdit(readOnly=True)
        self.out.setMinimumSize(900, 500)
        lay.addWidget(self.out)

        self.say(f"Python {sys.version.split()[0]} {platform.architecture()[0]} on {platform.platform()}")
        rt = LuaRuntime()
        self.say(f"lupa {lupa.__version__}, lupa.lua55 lua_version={rt.lua_version}, Qt {QT_VERSION_STR}")
        self.version_ok = rt.lua_version == (5, 5)
        self.results = []
        if auto:
            self.plan = list(AUTO_PLAN)
            QTimer.singleShot(500, self.next_auto)

    def beat(self):
        now = time.perf_counter()
        self.max_gap = max(self.max_gap, now - self.last)
        self.last = now
        self.beats += 1
        self.alive.setText(f"GUI heartbeat {self.beats}   (this counter freezing means the GUI is blocked)")

    def say(self, s):
        self.out.appendPlainText(s)
        if self.auto:
            print(s, flush=True)

    def run(self, key):
        self.cur = key
        self.max_gap = 0.0
        self.last = time.perf_counter()
        self.beats_at_start = self.beats
        self.say(f"\n> {key}: {SCENARIOS[key][0]}")
        self.start.emit(key)
        if self.auto:
            self.run_id = getattr(self, "run_id", 0) + 1
            QTimer.singleShot(10000, lambda rid=self.run_id: self.watchdog(rid))

    def watchdog(self, rid):
        if rid == self.run_id and self.cur not in [n for n, _, _ in self.results]:
            self.results.append((self.cur, False, "never finished within 10 s; the worker thread is stuck"))
            self.plan = []
            self.summary(stuck=True)

    def on_waiting(self, w):
        self.cont.setEnabled(w)
        self.say("  waiting for Operator input" if w else "  input wait over")

    def click_continue(self):
        self.say(f"  Continue clicked at {time.perf_counter():.3f}")
        self.worker.answer_value = "Green"
        self.worker.answer.set()

    def click_abort(self):
        self.abort_at = time.perf_counter()
        self.say("  Abort clicked")
        self.worker.abort.set()

    def on_finished(self, name, ok, val, secs):
        stall = self.max_gap * 1000
        self.say(f"  ok={ok} after {secs:.2f}s, longest GUI stall {stall:.0f} ms, heartbeats {self.beats - self.beats_at_start}")
        self.say("  " + val.replace("\n", "\n  "))
        if self.auto:
            check = CHECKS[name]
            passed, why = check(self, ok, val, secs, stall)
            self.results.append((name, passed, why))
            self.say(f"  {'PASS' if passed else 'FAIL'}: {why}")
            QTimer.singleShot(300, self.next_auto)

    # --- unattended driver
    def next_auto(self):
        if not self.plan:
            return self.summary()
        key, action, delay = self.plan.pop(0)
        self.run(key)
        if action:
            QTimer.singleShot(delay, getattr(self, action))

    def summary(self, stuck=False):
        self.say("\n===== SUMMARY =====")
        self.say(f"{'PASS' if self.version_ok else 'FAIL'}  lua_version is (5, 5)")
        for name, passed, why in self.results:
            self.say(f"{'PASS' if passed else 'FAIL'}  {name}: {why}")
        allok = self.version_ok and all(p for _, p, _ in self.results)
        self.say("ALL PASSED" if allok else "SOME CHECKS FAILED")
        self.worker.abort.set()
        self.thread.quit()
        self.thread.wait(2000)
        if stuck:
            os._exit(1)  # the worker can't be stopped; don't hang the batch file
        QApplication.exit(0 if allok else 1)


def _abort_latency(w):
    return (time.perf_counter() - w.abort_at) * 1000


# (scenario, GUI action to take, after ms). The GUI clicking a button mid-run shows it is responsive.
AUTO_PLAN = [
    ("busy", None, 0),
    ("input", "click_continue", 1500),
    ("abort_busy", "click_abort", 1000),
    ("abort_pcall", "click_abort", 1000),
    ("abort_xpcall", "click_abort", 1000),
    ("abort_input", "click_abort", 1000),
    ("lua_error", None, 0),
    ("py_error", None, 0),
    ("py_error_raw", None, 0),
    ("locked", None, 0),
]
CHECKS = {
    "busy": lambda w, ok, v, s, st: (ok and st < 100, f"{int(v):,} Lua loop iterations with the hook on; GUI stall {st:.0f} ms (< 100)"),
    "input": lambda w, ok, v, s, st: (ok and v == "Green" and s >= 1.4 and st < 100,
                                      f"step blocked {s:.2f}s, then returned {v!r}; GUI stall {st:.0f} ms"),
    "abort_busy": lambda w, ok, v, s, st: (not ok and v.startswith("aborted") and _abort_latency(w) < 200,
                                           f"stopped by count hook {_abort_latency(w):.0f} ms after Abort, error {v.splitlines()[0]!r}"),
    "abort_pcall": lambda w, ok, v, s, st: (not ok and v.startswith("aborted") and _abort_latency(w) < 200,
                                            f"pcall could not swallow the abort; stopped {_abort_latency(w):.0f} ms after Abort"),
    "abort_xpcall": lambda w, ok, v, s, st: (not ok and v.startswith("aborted") and _abort_latency(w) < 200,
                                             f"xpcall could not swallow the abort; stopped {_abort_latency(w):.0f} ms after Abort"),
    "abort_input": lambda w, ok, v, s, st: (not ok and v.startswith("aborted") and _abort_latency(w) < 200,
                                            f"blocked ATE.input released {_abort_latency(w):.0f} ms after Abort, error {v.splitlines()[0]!r}"),
    "lua_error": lambda w, ok, v, s, st: (not ok and "attempt to index" in v and "demo/step.tslua:4:" in v.splitlines()[0] and "'measure'" in v and "'step'" in v,
                                          f"first line {v.splitlines()[0]!r}; traceback names measure and step"),
    "py_error": lambda w, ok, v, s, st: (not ok and v.splitlines()[0] == "demo/step.tslua:3: ATE.read: device 'dmm' not open" and "'step'" in v,
                                         f"first line {v.splitlines()[0]!r}"),
    "py_error_raw": lambda w, ok, v, s, st: (not ok and "not open" in v,
                                             f"unwrapped Python error still reaches xpcall: first line {v.splitlines()[0]!r}"),
    "locked": lambda w, ok, v, s, st: (ok and v == "nil nil nil nil", f"dofile loadfile load python = {v}"),
}

if __name__ == "__main__":
    auto = "--auto" in sys.argv
    app = QApplication(sys.argv)
    w = Window(auto)
    w.show()
    sys.exit(app.exec())
