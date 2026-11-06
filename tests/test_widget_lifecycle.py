"""Test windows must be destroyed on the GUI thread before the next test starts."""

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QComboBox, QLabel, QWidget
from shiboken6 import isValid


@pytest.fixture(scope="module")
def windows(qapp):
    widgets = []
    destroyed_on = []
    yield widgets, destroyed_on
    # Module teardown follows the per-test resource cleanup. Retain the wrappers so this
    # checks explicit destruction, independent of refcounts or the GC's scheduling.
    assert widgets and all(not isValid(widget) for widget in widgets)
    assert destroyed_on == [qapp.thread()]


def test_test_owned_window_and_children_are_released(windows):
    widgets, destroyed_on = windows
    window = QWidget()
    child = QLabel("child", window)
    combo = QComboBox(window)
    combo.addItem("choice")
    combo.view()  # create Qt's parented top-level popup; its owner must destroy it
    window.cycle = lambda: window
    window.destroyed.connect(lambda: destroyed_on.append(QThread.currentThread()))
    widgets.extend((window, child, combo))
