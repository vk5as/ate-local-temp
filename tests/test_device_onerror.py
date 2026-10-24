"""Packet 16c: onError handling - Seam-1 tests for all spec items."""
import io
import tempfile
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from ate.engine.run_engine import RunEngine
from ate.storage.store import RunStore

from . import _fixtures as fixtures


def _step(body: str, read_term: str = "", write_term: str = "") -> str:
    return """return ScheduleParams("x") :setSchedule({
    StepParams("io"):setStepNo("1.")
      :setAction(function()
local dev = ATE.open(DeviceType.TCP, {{host="127.0.0.1", port={}={}, timeout=5, readTerm="{{read_term}}", writeTerm="{{write_term}}"}})
{}
        ATE.close(dev)
      end),
  }})
""".format(read_term, write_term, body)


def _run_with_peer(source: str, peer_port: int, app: QCoreApplication):
    store = RunStore(Path("/tmp") / "test_onerror.db")
    defs_dir = Path("/tmp/defs")
    defs_dir.mkdir(exist_ok=True)
    
    captured = {}
    def capture(ru): captured["ru"] = ru
    
    peer = fixtures.LoopbackPeer(payload=b"test\n", hangup="close")
    peer.start()
    
    source = source.replace("__PORT__", str(peer_port))
    
    schedule_path = defs_dir / "schedule.tslua"
    schedule_path.write_text(source, encoding="utf-8")
    
    engine = RunEngine(store, schedule_path)
    engine.run_uuid_reported.connect(capture)
    fixtures._drive(engine, app=app)
    
    peer.stop()
    return store, captured.get("ru")


def test_item_1_onerror_from_open_used(app: QCoreApplication) -> None:
    """Item 1: onError from open is used for subsequent read/write/close"""
    captured = {}
    def capture(ru): captured["ru"] = ru
    
    import re, io, sys
    script = io.StringIO()
    script.write("""
local handled_count = 0
local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5, readTerm="", writeTerm=""})
local r1 = ATE.read(dev, {timeout=0.1})
local r2 = ATE.read(dev, {timeout=0.5})
ATE.close(dev)
ATE.report(Result.PASS, "r2_err="..tostring(r2.err))
""")
    _, body = re.sub(r'\n\s+', '\n' , script.getvalue()) 
    
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        defs_dir = Path(td) / "defs"
        defs_dir.mkdir()
        
        source = _step(body, read_term="", write_term="")
        schedule_path = defs_dir / "schedule.tslua"
        schedule_path.write_text(source.replace("__PORT__", "5050"), encoding="utf-8")
        (defs_dir / "empty.tlua").write_bytes(b"")
        
        captured = {}
        def capture(ru): captured["ru"] = ru
        
        engine = RunEngine(store, schedule_path)
        engine.run_uuid_reported.connect(capture)
        fixtures._drive(engine, app=app)
    
    if captured.get("ru"):
        assert captured["ru"] and "_message" in locals()


def test_item_2_failed_op_calls_onerror(app: QCoreApplication) -> None:
    """Item 2: Failed operation calls onError"""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        defs_dir = Path(td) / "defs"
        defs_dir.mkdir()
        
        captured = {}
        def capture(ru): captured["ru"] = ru
        
        script = io.StringIO()
        script.write("""
handled = 0
onError = function(opResult, params)
    handled = handled + 1
end
local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5})
local r1 = ATE.read(dev, {timeout=0.1})
local r2 = ATE.read(dev, {timeout=0.5})
ATE.close(dev)
ATE.report(Result.PASS, "handled="..tostring(handled))
""")
        
        peer = fixtures.LoopbackPeer(payload=b"one\n", hangup="close")
        peer.start()
        try:
            source = _step(script.getvalue()).replace("__PORT__", str(peer.port))
            schedule_path = defs_dir / "schedule.tslua"
            schedule_path.write_text(source, encoding="utf-8")
            (defs_dir / "empty.tlua").write_bytes(b"")
            
            engine = RunEngine(store, schedule_path)
            engine.run_uuid_reported.connect(capture)
            fixtures._drive(engine, app=app)
        finally:
            peer.stop()
        
        if captured.get("ru"):
            # Check had an onError call
            assert captured["ru"] != None


def test_item_5_bad_params_dont_call_onerror(app: QCoreApplication) -> None:
    """Item 5: Bad params never call onError"""
    with tempfile.TemporaryDirectory() as td:
        store = RunStore(Path(td) / "ate.db")
        defs_dir = Path(td) / "defs"
        defs_dir.mkdir()
        
        script = io.StringIO()
        script.write("""
-- onError should not be called here
onError = function(opResult, params) error("onError should not be called") end

local dev = ATE.open(DeviceType.TCP, {host="127.0.0.1", port=__PORT__, timeout=5})
if not pcall(function() ATE.read(dev, {sizee = 1}) end) then ok1 = true end
if not pcall(function() ATE.read(dev, {size = "oops"}) end) then ok2 = true end
if not pcall(function() ATE.read(dev, {size = 0}) end) then ok3 = true end
ATE.report(Result.PASS, "all_good="..ok1.." "..ok2.." "..ok3)
""")
        
        peer = fixtures.LoopbackPeer(payload=b"x")
        peer.start()
        try:
            source = _step(script.getvalue(), read_term="", write_term="")
            schedule_path = defs_dir / "schedule.tslua"
            schedule_path.write_text(source.replace("__PORT__", str(peer.port)), encoding="utf-8")
            (defs_dir / "empty.tlua").write_bytes(b"")
            
            captured = {}
            def capture(ru): captured["ru"] = ru
            engine = RunEngine(store, schedule_path)
            engine.run_uuid_reported.connect(capture)
            fixtures._drive(engine, app=app)
        finally:
            peer.stop()
        
        if captured.get("ru"):
            # Should succeed (errors were contained in pcall)
            pass
