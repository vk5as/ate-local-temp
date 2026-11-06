"""Packet 42 — the bottom band loses its Summary tab, and the Windows title bar matches the header.

GitHub issue #3, "rc4 issues": the Summary tab next to the Log tab repeats what the run page
already shows, and the coloured run header sits under a plain Windows title bar that looks bad
against it. Both fixes are here:

* **no Summary tab.** The band is the Log, and a band with one pane has no tab bar. The Log
  content itself is untouched — live during the run, read-only, its own floor — and the
  Summary tab, its builder and its updater are gone (grep proved nothing else used them).
* **the native title bar.** On Windows, `DwmSetWindowAttribute` paints the caption with the
  theme's `head` token and its caption text with `head_ink`, so the frame blends with the run
  header. Off Windows nothing is attempted; an unsupported attribute (Windows 10 answers with
  an error HRESULT) or a ctypes failure is swallowed, and a launch never fails because of it.

The DWM call is injectable, so these tests run on the Linux CI: they assert the attribute IDs
and the `0x00bbggrr` values, not that a Windows frame actually changed.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import time
import types

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QTabWidget

from ate import app
from ate.app import apply_windows_title_bar, launch
from ate.run_session.headless import HeadlessRunSession, SessionState
from ate.storage.store import RunStore
from ate.ui import run_widgets, theme
from ate.ui.run_widgets import LogPane
from ate.ui.staged_run_screen import StagedRunScreen
from ate.ui.theme import TOKENS
from ate.ui.three_pane_run_screen import ThreePaneRunScreen

from ._fixtures import fill_operator_fields, write_ate_env

#: The dwmapi.h attribute IDs the code has to use.
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

#: The attributes in the order they are set, each with the theme token it comes from.
ATTRIBUTES = [(DWMWA_CAPTION_COLOR, "head"), (DWMWA_TEXT_COLOR, "head_ink"), (DWMWA_BORDER_COLOR, "head")]

#: Qt's own ceiling for a widget nobody capped (PySide6 does not expose the name).
QWIDGETSIZE_MAX = (1 << 24) - 1


def _colorref(token: str) -> int:
    """`#rrggbb` as the DWM wants it: `0x00bbggrr`."""
    red, green, blue = (int(token[i:i + 2], 16) for i in (1, 3, 5))
    return (blue << 16) | (green << 8) | red


def _screen(qapp, tmp_path, layout: str):
    """One Run layout on a real session, with the picker populated."""
    env = write_ate_env(tmp_path)
    session = HeadlessRunSession(env["config"])
    screen = StagedRunScreen(session) if layout == "staged" else ThreePaneRunScreen(session)
    qapp.processEvents()
    return screen


def _window(tmp_path, layout: str = "staged"):
    """A real MainWindow, launched but not yet shown."""
    env = write_ate_env(tmp_path)
    RunStore(env["db"])  # the launch refuses an uncreated database, so make it first
    config = tmp_path / "title-config.json"
    data = json.loads(env["config"].read_text(encoding="utf-8"))
    data["ui"] = {"layout": layout}
    config.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    return launch(["--config", str(config)], exec_=False, prompt=lambda path: True, alert=lambda message: None).window


def _pick(screen, name: str = "Passing schedule") -> None:
    rows = screen.picker.schedule_list
    item = next(rows.item(i) for i in range(rows.count()) if rows.item(i).data(Qt.ItemDataRole.UserRole) == name)
    rows.setCurrentItem(item)


def _wait(qapp, predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


# -- A. the band has no Summary tab --------------------------------------------


def test_neither_band_has_a_summary_pane_or_a_tab_bar(qapp, tmp_path):
    """The bottom band is the Log alone: no Summary pane, and no tab bar to hold one tab (issue #3)."""
    for layout in ("staged", "threePane"):
        screen = _screen(qapp, tmp_path, layout)
        view = screen.run_view if layout == "staged" else screen
        for name in ("summary_tab", "summary_pane", "summary_text"):
            assert not hasattr(view, name), f"the {layout} band still has a {name}"
        assert not view.findChildren(QTabWidget), f"the {layout} band still has a tab bar"


def test_the_band_keeps_its_log_pane_with_a_floor_and_no_ceiling(qapp, tmp_path):
    """The Log pane takes the band's place: still read-only, still floored, still uncapped."""
    for layout in ("staged", "threePane"):
        screen = _screen(qapp, tmp_path, layout)
        view = screen.run_view if layout == "staged" else screen
        panes = view.findChildren(LogPane)
        assert panes, f"the {layout} band has no log pane in it"
        pane = view.log_pane
        assert pane in panes, f"the {layout} log pane is not inside the band"
        text = pane.text_widget()
        assert text.isReadOnly(), f"the {layout} log pane is editable"
        text.setPlainText("the run writes it")  # the run may still write it
        assert text.toPlainText() == "the run writes it", "a read-only pane cannot be written by the run"
    assert _screen(qapp, tmp_path, "staged").run_view.log_pane.minimumHeight() == 140, "the band lost its floor"
    assert _screen(qapp, tmp_path, "staged").run_view.log_pane.maximumHeight() == QWIDGETSIZE_MAX, "the band carries a ceiling"


def test_build_summary_and_update_summary_are_gone(qapp, tmp_path):
    """Nothing builds or writes a Summary any more: the function and its hook are removed, not hidden."""
    assert not hasattr(run_widgets, "build_summary"), "build_summary is still in run_widgets"
    assert not hasattr(run_widgets.RunScreenMixin, "_update_summary"), "_update_summary is still on the mixin"
    for layout in ("staged", "threePane"):
        screen = _screen(qapp, tmp_path, layout)
        assert not hasattr(screen, "_update_summary"), f"the {layout} layout still updates a Summary"
        assert not hasattr(run_widgets, "build_summary"), "the Summary builder came back"


def test_the_band_names_its_log_in_both_layouts(qapp, tmp_path):
    """With the tab bar gone the band still says what it is: "Log", with the level muted beside it.

    The mockup's variant B right pane heads the log `Log … INFO and above`; packet 42 removed the
    tab that used to carry the name, so the pane has to carry it (issue #3 review, item 3)."""
    for layout in ("staged", "threePane"):
        screen = _screen(qapp, tmp_path, layout)
        pane = (screen.run_view if layout == "staged" else screen).log_pane
        title = pane.findChild(QLabel, "log_title")
        note = pane.findChild(QLabel, "log_note")
        assert title is not None and note is not None, f"the {layout} band has no Log heading"
        assert title.text() == "Log", f"the {layout} band is headed {title.text()!r}"
        assert note.text() == "INFO and above", f"the {layout} band notes {note.text()!r}"
        assert not title.isHidden() and not note.isHidden(), f"the {layout} heading is hidden"
        assert pane.text_widget().isReadOnly(), f"the heading replaced the {layout} log itself"


def test_the_log_heading_is_painted_by_the_theme_not_an_inline_style(qapp, tmp_path):
    """The heading's ink and weight come from the palette, so a token change moves it (issue #3 review)."""
    screen = _screen(qapp, tmp_path, "threePane")
    pane = screen.log_pane
    title = pane.findChild(QLabel, "log_title")
    note = pane.findChild(QLabel, "log_note")
    assert title.styleSheet() == "" and note.styleSheet() == "", "the heading styles itself inline"
    assert f"#log_title {{ color: {TOKENS['ink']}" in theme.STYLESHEET, "the Log heading is not themed"
    assert f"#log_note {{ color: {TOKENS['ink_2']}" in theme.STYLESHEET, "the level note is not themed"


def test_a_finished_run_still_draws_its_log_in_the_band(qapp, tmp_path):
    """Dropping the Summary tab leaves the Log live and complete: the band shows the run's own lines."""
    screen = _screen(qapp, tmp_path, "threePane")
    _pick(screen, "Logging schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.start_button.click()
    qapp.processEvents()
    assert _wait(qapp, lambda: screen.session.state == SessionState.FINISHED), "the run never finished"
    # The last refresh can land a turn after FINISHED under a loaded `-n 3` run; wait (bounded) for
    # the run's own line rather than reading the pane at the instant the state flips.
    _wait(qapp, lambda: any("something failed" in line for line in screen.log_pane.rendered_lines()))
    lines = screen.log_pane.rendered_lines()
    assert lines, "the band's log pane is empty after a finished run"
    assert any(line.startswith("── Step") for line in lines), f"the log band shows no step headings: {lines[:3]}"
    assert any("something failed" in line for line in lines), f"the run's own lines are missing: {lines}"


# -- B. the Windows title bar ----------------------------------------------------


def test_the_finished_run_keeps_its_boxes_locked_after_new_run_with_nothing_picked(qapp, tmp_path):
    """Packet 42 review, item 4: New run with no schedule made a finished run's rows tickable.

    The packet's reading is the packet 41 one — with nothing picked the run on screen "stays as it
    is" — so the rows are kept rather than cleared, and what changes is that their Enable boxes stay
    locked: they belong to a run that is over, and there is no schedule for a tick to apply to. A
    picked schedule is different: then the same rows are next run's setup, and the boxes are live."""
    screen = _screen(qapp, tmp_path, "threePane")
    _pick(screen, "Logging schedule")
    fill_operator_fields(screen)
    qapp.processEvents()
    screen.start_button.click()
    assert _wait(qapp, lambda: screen.session.state == SessionState.FINISHED), "the run never finished"
    for _ in range(50):
        qapp.processEvents()
    rows = screen.table.table.rowCount()

    screen.start_new_run()  # the schedule is still picked: those rows are the next run's setup
    for _ in range(50):
        qapp.processEvents()
    boxes = list(screen.table.checks.values())
    assert boxes and all(box.isEnabled() for box in boxes), "a picked schedule left the boxes locked"

    screen.session.clear_selection()
    screen.start_new_run()
    for _ in range(50):
        qapp.processEvents()

    assert screen.table.table.rowCount() == rows, "New run with nothing picked emptied the finished run"
    assert not any(box.isEnabled() for box in boxes), f"the finished run's Enable boxes are tickable: {boxes}"


def test_the_title_bar_colour_is_the_header_token_as_a_colorref(qapp, tmp_path, monkeypatch):
    """Caption, caption text and border are set to `head` and `head_ink`, as `0x00bbggrr`, on the window's winId."""
    window = _window(tmp_path)
    calls: list[tuple[int, int, int]] = []

    def record(hwnd, attribute, value) -> int:
        calls.append((hwnd, attribute, value))
        return 0

    monkeypatch.setattr(sys, "platform", "win32")
    apply_windows_title_bar(window, set_attribute=record)
    assert [attribute for _hwnd, attribute, _value in calls] == [attribute for attribute, _token in ATTRIBUTES], (
        f"the DWM attributes set were {calls}"
    )
    for (attribute, token), (hwnd, _attribute, value) in zip(ATTRIBUTES, calls):
        assert value == _colorref(TOKENS[token]), f"attribute {attribute} got {value:#08x}, not {_colorref(TOKENS[token]):#08x} from {token}"
        assert hwnd == window.winId(), "the call did not use the window's own winId"


def test_the_colour_follows_the_tokens_and_not_a_literal(qapp, tmp_path, monkeypatch):
    """A token changed is a title bar changed: the value comes from the palette, never from a literal."""
    window = _window(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(TOKENS, "head", "#0a0b0c")
    monkeypatch.setitem(TOKENS, "head_ink", "#112233")
    calls: list[tuple[int, int]] = []

    def record(_hwnd, attribute, value) -> int:
        calls.append((attribute, value))
        return 0

    apply_windows_title_bar(window, set_attribute=record)
    assert calls[0] == (DWMWA_CAPTION_COLOR, 0x0C0B0A), f"the caption colour is not the token: {calls[0]}"
    assert calls[1] == (DWMWA_TEXT_COLOR, 0x332211), f"the caption text colour is not the token: {calls[1]}"
    assert calls[2] == (DWMWA_BORDER_COLOR, 0x0C0B0A), f"the border colour is not the token: {calls[2]}"


def test_nothing_is_attempted_off_windows(qapp, tmp_path, monkeypatch):
    """On Linux and macOS the function touches no window and calls no DWM."""
    window = _window(tmp_path)
    calls: list[tuple[int, int, int]] = []

    def record(_hwnd, attribute, value) -> int:
        calls.append((attribute, value))
        return 0

    for platform in ("linux", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        apply_windows_title_bar(window, set_attribute=record)
    assert calls == [], f"off Windows the title bar was still touched: {calls}"


def test_a_rejected_hresult_is_swallowed_and_logged_as_debug(qapp, tmp_path, monkeypatch):
    """Windows 10 answers these attributes with an error HRESULT: ATE carries on, logged at DEBUG only."""
    window = _window(tmp_path)
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ate.app._dwm_set_window_attribute", lambda _hwnd, _attribute, _value: 0x80000000)
    apply_windows_title_bar(window, log=lambda level, _component, message: logged.append((level, message)))
    assert logged, "the rejected attribute was not logged at all"
    assert all(level == "DEBUG" for level, _message in logged), f"the failure was logged louder than DEBUG: {logged}"


def test_the_handle_is_declared_so_a_64_bit_one_is_not_cut_in_half(qapp, tmp_path, monkeypatch):
    """A window handle above 2**32 reaches the DWM whole (issue #3 review, item 2).

    The stand-in DLL is not a mock: its `DwmSetWindowAttribute` is libc's real `llabs`, which
    answers with the number ctypes packed for it, so the declared argtypes are what decide the
    result. Left a bare Python int, ctypes reads the handle as a 32-bit C int and `0x1_0000_0005`
    arrives at the DWM as 5."""
    handle = 0x1_0000_0005
    try:
        echo = ctypes.CDLL("libc.so.6").llabs
    except (OSError, AttributeError):
        pytest.skip("no libc to stand in for dwmapi on this platform")
    monkeypatch.setattr(app, "_dwm", types.SimpleNamespace(DwmSetWindowAttribute=echo))
    result = app._dwm_set_window_attribute(handle, DWMWA_CAPTION_COLOR, 0x000C0B0A)
    assert echo.argtypes == (wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD), (
        f"DwmSetWindowAttribute was called with argtypes {echo.argtypes}"
    )
    assert echo.restype is ctypes.c_long, f"the HRESULT was read as {echo.restype}, not a LONG"
    assert result == handle, f"the handle reached the DWM as {result:#x}, not {handle:#x}"


def test_a_refused_hresult_is_logged_as_the_hex_windows_wrote(qapp, tmp_path, monkeypatch):
    """`0x80070057` is printed as `0x80070057`, not as the negative number ctypes hands back."""
    window = _window(tmp_path)
    logged: list[str] = []
    # HRESULT is a signed 32-bit LONG, so an unsuccessful one arrives as a negative int.
    refused = 0x80070057 - 2**32
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ate.app._dwm_set_window_attribute", lambda _hwnd, _attribute, _value: refused)
    apply_windows_title_bar(window, log=lambda _level, _component, message: logged.append(message))
    assert logged, "the refused attribute was not logged at all"
    assert all("HRESULT 0x80070057" in message for message in logged), f"the HRESULT printed as {logged}"


def test_a_raising_ctypes_call_does_not_stop_the_window(qapp, tmp_path, monkeypatch):
    """An exception out of ctypes is caught: the window still shows and the launch is not its problem."""
    window = _window(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ate.app._dwm_set_window_attribute", lambda hwnd, attribute, value: (_ for _ in ()).throw(OSError("dwmapi.dll is not there")))
    window.show()
    qapp.processEvents()
    assert window.isVisible(), "the window did not show because the title bar call raised"


def test_debug_windows_are_left_alone(qapp, tmp_path, monkeypatch):
    """Only the main window gets a coloured title bar: the Debug window keeps Qt's own frame."""
    window = _window(tmp_path)
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ate.app._dwm_set_window_attribute", lambda hwnd, attribute, value: calls.append((hwnd, attribute)) or 0)
    window.show()
    qapp.processEvents()
    window.debug_window.show()
    qapp.processEvents()
    assert all(hwnd == window.winId() for hwnd, _attribute in calls), f"a window other than the main window was coloured: {calls}"


def test_the_colour_is_set_on_show_and_again_when_the_window_is_shown_again(qapp, tmp_path, monkeypatch):
    """First show sets it; showing the window again sets it again, so a recreated native window is recoloured."""
    window = _window(tmp_path)
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("ate.app._dwm_set_window_attribute", lambda hwnd, attribute, value: calls.append((attribute, value)) or 0)
    window.show()
    qapp.processEvents()
    first = len(calls)
    assert first == len(ATTRIBUTES), f"showing the window set {first} attributes, not {len(ATTRIBUTES)}"
    window.hide()
    qapp.processEvents()
    window.show()
    qapp.processEvents()
    assert len(calls) > first, "the title bar was not set again when the window was shown a second time"


def test_showing_a_window_on_linux_touches_nothing(qapp, tmp_path, monkeypatch):
    """The show hook runs on every launch, so on the Linux CI it must be inert."""
    monkeypatch.setattr(sys, "platform", "linux")
    window = _window(tmp_path)
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr("ate.app._dwm_set_window_attribute", lambda hwnd, attribute, value: calls.append((attribute, value)) or 0)
    window.show()
    qapp.processEvents()
    assert calls == [], f"showing a window on Linux set DWM attributes: {calls}"
