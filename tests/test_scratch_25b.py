"""Scratch: three-pane results after a finished run, before and after a switch."""

from ate.app import launch
from ate.run_session.headless import SessionState
from ate.storage.store import RunStore

from ._fixtures import write_ate_env
from .test_layout_switching_25b import (
    _add_pause_and_block_schedule,
    _finish,
    _pick_and_start,
    _three_step_results,
    _wait,
)


def test_three_results(qapp, tmp_path):
    env = write_ate_env(tmp_path)
    RunStore(env["db"])
    schedule = _add_pause_and_block_schedule(tmp_path)
    outcome = launch(["--config", str(env["config"])], exec_=False, prompt=lambda p: True, alert=print)
    window = outcome.window
    window.show()
    qapp.processEvents()
    asked, prompts = _pick_and_start(qapp, window, schedule)
    print("prompts", prompts, "asked", asked)
    window.staged_screen.run_view.failure_prompt_bar.continue_button.click()
    qapp.processEvents()
    _wait(qapp, lambda: any(a[2] == 1 for a in asked))
    ans = window.staged_screen.input_widget(1, 1)
    ans.line_edit.setText("7")
    ans.ok_button.click()
    print("wait finished", _wait(qapp, lambda: window.session.state == SessionState.FINISHED))
    qapp.processEvents()
    uuid = window.session.run_uuid
    print("nodes", [(n["node_seq"], n.get("result"), n.get("attempt_count")) for n in window.session.store.nodes(uuid)])
    print("attempts", [(a["node_seq"], a["attempt_no"], a["pass_fail"]) for a in window.session.store.attempts(uuid)])
    three = window.three_pane_screen
    print("three table rows", three.table.table.rowCount(), "step rows", three.table._step_rows)
    print("three results before switch", _three_step_results(three))
    print("three dirty", three._store_dirty)
    window.three_pane_layout_action.trigger()
    qapp.processEvents()
    print("three results after switch", _three_step_results(three))
    print("three log", three.log_pane.rendered_lines())
    _finish(qapp, window)
