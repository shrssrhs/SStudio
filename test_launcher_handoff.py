"""Regression tests for the Stage 3.9 standalone launcher -> editor
handoff (client_studio._run_launcher_and_resolve_place_path()).

Bug history: the first version of that function ended its nested
QEventLoop via `qt_app.lastWindowClosed.connect(loop.quit)`. Confirmed
empirically (isolated PySide6 6.11.1 repro, not a guess) that
QApplication.lastWindowClosed simply never fires for a window closed
while a NESTED QEventLoop -- as opposed to the top-level
QCoreApplication::exec() -- is the one currently running, regardless of
quitOnLastWindowClosed's value. That meant loop.exec() never returned
once the user picked a project: the launcher closed, but main()'s rest
(building the real editor window) never ran, so the whole process
appeared to hang/vanish. These tests exercise the ACTUAL fixed function
(closeEvent override + explicit loop.quit(), never relying on
lastWindowClosed) end-to-end, headless, with no interactive GUI
automation -- a QTimer.singleShot() stands in for "the user clicked
something" the same way Qt's own QDialog.exec() tests typically do.

Follows this project's existing test convention: plain top-level-
assertion script, run directly, offscreen Qt platform.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import client_studio as cs
import sstudio_templates

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def _find_launcher_window() -> sstudio_templates.SStudioTemplatesWindow | None:
    # Filters to VISIBLE instances -- WA_DeleteOnClose means a closed
    # launcher gets destroyed via deleteLater() (deferred to the next
    # event-loop iteration, not synchronous), so a stale, not-yet-
    # destroyed instance from a PREVIOUS call in the same test process
    # could otherwise still turn up here for a brief window and get
    # mistaken for the current one.
    for widget in app.topLevelWidgets():
        if isinstance(widget, sstudio_templates.SStudioTemplatesWindow) and widget.isVisible():
            return widget
    return None


def _run_launcher_with_watchdog(label: str, timeout_ms: int = 4000):
    """Calls the real _run_launcher_and_resolve_place_path(), guarded by
    a watchdog timer that only acts if the call hasn't already returned
    -- a genuine regression (the nested loop never quitting) FAILS this
    test with a clear message instead of hanging the whole suite
    forever."""
    done = {"finished": False}

    def _watchdog() -> None:
        if done["finished"]:
            return
        check(False, f"{label}: did not resolve within {timeout_ms}ms -- the nested loop likely never quit (regression)")
        for w in list(app.topLevelWidgets()):
            w.close()

    QTimer.singleShot(timeout_ms, _watchdog)
    try:
        return cs._run_launcher_and_resolve_place_path(app)
    finally:
        done["finished"] = True


def test_closing_launcher_without_choosing_returns_none_and_does_not_hang() -> None:
    """The "user clicked the launcher's own close button, picked
    nothing" path -- must return None (main() then exits with code 0,
    never building an engine/editor for a project the user never
    chose), and must actually RETURN at all rather than hang forever
    (the exact symptom of the lastWindowClosed-based bug)."""
    def _close_launcher_soon() -> None:
        window = _find_launcher_window()
        check(window is not None, "the launcher window is a real, findable top-level widget while showing")
        if window is not None:
            window.close()

    QTimer.singleShot(50, _close_launcher_soon)
    result = _run_launcher_with_watchdog("close-without-choosing")
    check(result is None, f"closing the launcher without picking anything resolves to None, got {result!r}")
    check(_find_launcher_window() is None, "the launcher window is gone (WA_DeleteOnClose) after the function returns")


def test_picking_a_template_resolves_a_real_path_and_creates_the_file() -> None:
    """The "user picked New from Template" path -- must resolve to a
    real, existing Place file path (place_manager.create_from_template()
    always writes it to disk before returning success), and the nested
    loop must exit via the DIRECT loop.quit() in _finish(), never via
    lastWindowClosed."""
    import tempfile
    from pathlib import Path
    from PySide6.QtWidgets import QDialog

    tmp_dir = Path(tempfile.mkdtemp(prefix="sstudio_launcher_test_"))
    created_name = "LauncherHandoffTestProject"

    def _pick_template_soon() -> None:
        window = _find_launcher_window()
        check(window is not None, "the launcher window exists before simulating a template pick")
        if window is None:
            return
        # PlaceCreateDialog.exec() would normally block on a real user --
        # patch it to auto-accept with our test project name/dir instead
        # of driving actual dialog widgets (same "stand in for a click"
        # principle as the QTimer.singleShot above, just for a nested
        # QDialog rather than the top-level launcher).
        original_exec = cs.place_manager.PlaceCreateDialog.exec
        original_name = cs.place_manager.PlaceCreateDialog.result_name
        original_directory = cs.place_manager.PlaceCreateDialog.result_directory
        cs.place_manager.PlaceCreateDialog.exec = lambda self: QDialog.DialogCode.Accepted
        cs.place_manager.PlaceCreateDialog.result_name = lambda self: created_name
        cs.place_manager.PlaceCreateDialog.result_directory = lambda self: tmp_dir
        try:
            from sstudio_templates import DEFAULT_TEMPLATES
            window.page.template_activated_spec.emit(DEFAULT_TEMPLATES[0])
        finally:
            cs.place_manager.PlaceCreateDialog.exec = original_exec
            cs.place_manager.PlaceCreateDialog.result_name = original_name
            cs.place_manager.PlaceCreateDialog.result_directory = original_directory

    QTimer.singleShot(50, _pick_template_soon)
    result = _run_launcher_with_watchdog("pick-template")
    check(result is not None, "picking a template resolves to a real path, not None")
    if result is not None:
        check(result.is_file(), f"the resolved path is a real, already-written Place file: {result}")
    check(_find_launcher_window() is None, "the launcher window is gone after resolving a template pick")


def test_a_real_top_level_exec_after_the_launcher_is_not_short_circuited() -> None:
    """The actual symptom the user reported: after the launcher resolves
    and closes, the REAL top-level qt_app.exec() (what main() calls to
    run the editor) must not return immediately/without ever really
    running -- proving there is no stale quit-condition left over from
    the launcher's own nested loop."""
    def _close_launcher_soon() -> None:
        window = _find_launcher_window()
        if window is not None:
            window.close()

    QTimer.singleShot(50, _close_launcher_soon)
    _run_launcher_with_watchdog("real-exec-after-close")

    # Mirrors main(): build a new top-level window AFTER the launcher is
    # gone, then run the REAL app.exec() -- if the old lastWindowClosed-
    # based bug (or any equivalent stale-quit-state regression) were
    # still present, this would return instantly without the 300ms timer
    # ever having a chance to fire.
    from PySide6.QtWidgets import QMainWindow
    editor_stand_in = QMainWindow()
    editor_stand_in.setWindowTitle("Editor stand-in")
    editor_stand_in.show()

    fired = {"ran": False}

    def _mark_and_quit() -> None:
        fired["ran"] = True
        app.quit()

    QTimer.singleShot(300, _mark_and_quit)
    app.exec()
    check(fired["ran"], "the real top-level qt_app.exec() actually ran (the 300ms timer fired) instead of returning instantly")
    editor_stand_in.close()


test_closing_launcher_without_choosing_returns_none_and_does_not_hang()
test_picking_a_template_resolves_a_real_path_and_creates_the_file()
test_a_real_top_level_exec_after_the_launcher_is_not_short_circuited()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All launcher handoff tests passed.")
