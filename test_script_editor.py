"""Behavior-level tests for Stage 3.1's integrated Lua code editor
(script_editor.py) and its integration points in studio_editor_live.py.

Follows this project's existing test convention (test_physics_module.py,
test_scale_gizmo_math.py): a plain script of top-level assertions run
directly with `python test_script_editor.py`, no pytest/unittest. Requires
the offscreen Qt platform (set QT_QPA_PLATFORM=offscreen before running, or
this sets it itself if unset) since it builds real QWidgets headlessly.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication([])

import script_editor as se
import studio_editor_live as m


# ============================================================
# Fakes / helpers
# ============================================================

class _FakeButton:
    def __init__(self, text, role):
        self.text = text
        self.role = role


class _FakeMessageBox:
    """Drop-in stand-in for QMessageBox supporting exactly the surface
    ScriptEditorWorkspace/StudioMainWindow use (addButton/setWindowTitle/
    setText/setDefaultButton/exec/clickedButton). `click_text` selects
    which added button "the user" clicks; exec() is a no-op (never blocks)."""

    ButtonRole = QMessageBox.ButtonRole

    def __init__(self, click_text):
        self._click_text = click_text
        self._buttons = []
        self._clicked = None

    def __call__(self, *_args, **_kwargs):
        return self

    def setWindowTitle(self, _title):
        pass

    def setText(self, _text):
        pass

    def addButton(self, text, role):
        button = _FakeButton(text, role)
        self._buttons.append(button)
        return button

    def setDefaultButton(self, _button):
        pass

    def exec(self):
        for button in self._buttons:
            if button.text == self._click_text:
                self._clicked = button
                return
        self._clicked = self._buttons[-1] if self._buttons else None

    def clickedButton(self):
        return self._clicked


def make_bridge(objects=None):
    return m.EngineBridge(objects=list(objects) if objects else [], live_mode=False)


def make_script(name="Script1", object_type="Script", source="print(1)", parent="ServerScriptService"):
    return m.SceneObject(name=name, object_type=object_type, parent=parent, properties={"Source": source})


def make_workspace(bridge):
    scene_widget = m.QWidget()
    workspace = se.ScriptEditorWorkspace(bridge, scene_widget)
    return workspace


PASS_COUNT = 0


def check(condition, message):
    global PASS_COUNT
    assert condition, f"FAILED: {message}"
    PASS_COUNT += 1


def edit_text(editor, text):
    """Replaces an editor's content through the text-EDITING API (select
    all + insertText) instead of setPlainText(), which Qt treats as a
    programmatic load and explicitly resets document().isModified() to
    False -- exactly what ScriptEditorDocument._set_text_clean() relies
    on for remote refreshes. Any test simulating a REAL user edit (and
    expecting the document to end up dirty) must go through this."""
    cursor = editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    cursor.insertText(text)


# ============================================================
# 1. Open by ID / dedup / same-name separate tabs
# ============================================================

def test_open_by_id_and_dedup():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)

    check(ws.count() == 1, "workspace starts with only the Scene tab")
    ok = ws.open_script(obj.id)
    check(ok, "open_script succeeds for an existing Script")
    check(ws.count() == 2, "opening a Script adds exactly one tab")

    ok_again = ws.open_script(obj.id)
    check(ok_again, "re-opening the same instance_id succeeds")
    check(ws.count() == 2, "re-opening the same instance_id does not add a second tab")
    check(ws.currentWidget() is ws._documents[obj.id], "re-opening focuses the existing tab")

    print("test_open_by_id_and_dedup PASSED")


def test_same_name_separate_tabs():
    obj_a = make_script(name="Foo")
    obj_b = make_script(name="Foo")
    check(obj_a.id != obj_b.id, "two Scripts named 'Foo' still get distinct instance ids")
    bridge = make_bridge([obj_a, obj_b])
    ws = make_workspace(bridge)

    ws.open_script(obj_a.id)
    ws.open_script(obj_b.id)
    check(ws.count() == 3, "two same-named Scripts open as two separate tabs (+ Scene)")
    check(obj_a.id in ws._documents and obj_b.id in ws._documents, "both are keyed by instance id")
    check(ws._documents[obj_a.id] is not ws._documents[obj_b.id], "documents are distinct objects")

    print("test_same_name_separate_tabs PASSED")


# ============================================================
# 2. Rename / reparent preserve the open tab
# ============================================================

def test_rename_updates_tab_title():
    obj = make_script(name="Original")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)

    obj.name = "Renamed"
    bridge.scene_changed.emit()

    doc = ws._documents[obj.id]
    index = ws.indexOf(doc)
    check(ws.tabText(index) == "Renamed", "tab title follows the renamed instance")

    print("test_rename_updates_tab_title PASSED")


def test_reparent_preserves_open_tab():
    obj = make_script(name="Nested")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc_before = ws._documents[obj.id]

    obj.parent = "ReplicatedStorage"
    bridge.scene_changed.emit()

    check(obj.id in ws._documents, "instance id still has an open document after reparenting")
    check(ws._documents[obj.id] is doc_before, "reparenting keeps the SAME document instance, not a new one")

    print("test_reparent_preserves_open_tab PASSED")


# ============================================================
# 3. Dirty tracking / save / one authoritative edit
# ============================================================

def test_typing_marks_dirty_without_touching_bridge():
    obj = make_script(source="local x = 1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]
    check(not doc.is_dirty(), "freshly opened document starts clean")

    calls = []
    original_set_property = bridge.set_property
    bridge.set_property = lambda *a, **kw: (calls.append((a, kw)), original_set_property(*a, **kw))[-1]

    cursor = doc.editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    doc.editor.setTextCursor(cursor)
    doc.editor.insertPlainText("local y = 2\n")

    check(doc.is_dirty(), "typing marks the document dirty")
    check(len(calls) == 0, "typing must never call bridge.set_property (no per-keystroke scene history)")

    print("test_typing_marks_dirty_without_touching_bridge PASSED")


def test_save_calls_set_property_once_and_cleans():
    obj = make_script(source="local x = 1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]

    calls = []
    original_set_property = bridge.set_property
    def counting_set_property(*a, **kw):
        calls.append((a, kw))
        return original_set_property(*a, **kw)
    bridge.set_property = counting_set_property

    edit_text(doc.editor, "local x = 1\nlocal y = 2\n")
    check(doc.is_dirty(), "editing before save leaves the document dirty")

    ok = doc.save()
    check(ok, "save() succeeds for Source under the size limit")
    check(len(calls) == 1, "save() calls bridge.set_property exactly once")
    check(calls[0][0] == (obj.id, "properties.Source", "local x = 1\nlocal y = 2\n"), "save() sends the exact instance id/path/text")
    check(not doc.is_dirty(), "save() clears the dirty flag")
    check(obj.properties["Source"] == "local x = 1\nlocal y = 2\n", "authoritative Source is updated")

    print("test_save_calls_set_property_once_and_cleans PASSED")


def test_save_rejects_over_limit_without_truncating():
    obj = make_script(source="-- ok\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]

    calls = []
    bridge.set_property = lambda *a, **kw: calls.append((a, kw))

    too_long = "x" * (se.MAX_SOURCE_SIZE + 1)
    edit_text(doc.editor, too_long)
    ok = doc.save()

    check(not ok, "save() refuses Source over the 20000-character limit")
    check(len(calls) == 0, "an over-limit save never calls bridge.set_property")
    check(doc.is_dirty(), "a rejected save leaves the document dirty (not silently accepted)")
    check(obj.properties["Source"] == "-- ok\n", "authoritative Source is untouched by a rejected save")

    print("test_save_rejects_over_limit_without_truncating PASSED")


def test_utf8_and_multiline_preserved():
    text = "-- café éè\n\nlocal t = {\r\n  1,\r\n  2,\r\n}\n\ntrailing   \n"
    obj = make_script(source="")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]

    edit_text(doc.editor, text)
    saved_text = doc.editor.toPlainText()
    doc.save()

    check(obj.properties["Source"] == saved_text, "save() does not alter whitespace/newlines/unicode content")
    check(obj.properties["Source"].endswith("\n"), "trailing newline is preserved, not stripped")
    check("café" in obj.properties["Source"], "unicode content survives the round trip")

    print("test_utf8_and_multiline_preserved PASSED")


# ============================================================
# 4. Clean deletion vs dirty deletion
# ============================================================

def test_clean_deletion_closes_tab():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    check(obj.id in ws._documents, "sanity: tab is open")

    bridge.sync_delete(obj.id)
    check(obj.id not in ws._documents, "a clean tab closes automatically once its instance is deleted")
    check(ws.currentWidget() is ws.scene_widget, "closing the last Script tab returns to Scene")

    print("test_clean_deletion_closes_tab PASSED")


def test_dirty_deletion_kept_for_recovery():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]
    edit_text(doc.editor, "-- unsaved edit\n")
    check(doc.is_dirty(), "sanity: document is dirty before deletion")

    bridge.sync_delete(obj.id)
    check(obj.id in ws._documents, "a dirty tab is NOT closed when its instance is deleted")
    check(doc.deleted, "the surviving document is marked deleted")
    check("(Deleted)" in doc.display_name, "tab title reflects the deleted state")

    print("test_dirty_deletion_kept_for_recovery PASSED")


# ============================================================
# 5. Remote refresh: clean vs dirty (conflict)
# ============================================================

def test_clean_remote_refresh_updates_silently():
    obj = make_script(source="local a = 1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]
    check(not doc.is_dirty(), "sanity: clean after open")

    obj.properties["Source"] = "local a = 2\n"
    bridge.scene_changed.emit()

    check(doc.editor.toPlainText() == "local a = 2\n", "clean tab refreshes to the new authoritative Source")
    check(not doc.is_dirty(), "a programmatic refresh does not mark the document dirty")
    check(not doc.conflicted, "a clean refresh never raises a conflict")

    print("test_clean_remote_refresh_updates_silently PASSED")


def test_dirty_remote_update_conflicts_without_overwriting():
    obj = make_script(source="local a = 1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.show()  # isVisible() only reflects reality once there's a shown ancestor chain
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]
    edit_text(doc.editor, "local a = 999 -- my edit\n")
    check(doc.is_dirty(), "sanity: dirty before remote update")

    obj.properties["Source"] = "local a = 2 -- someone else's save\n"
    bridge.scene_changed.emit()

    check(doc.editor.toPlainText() == "local a = 999 -- my edit\n", "a dirty tab's local text is never silently overwritten")
    check(doc.conflicted, "a dirty tab facing a remote update enters the conflict state")
    check(doc.conflict_banner.isVisible(), "the conflict banner becomes visible")

    doc._resolve_conflict_reload_remote()
    check(doc.editor.toPlainText() == "local a = 2 -- someone else's save\n", "Reload Remote adopts the remote Source")
    check(not doc.is_dirty(), "Reload Remote leaves the document clean")
    check(not doc.conflicted, "Reload Remote clears the conflict state")

    print("test_dirty_remote_update_conflicts_without_overwriting PASSED")


def test_keep_local_requires_explicit_later_save():
    obj = make_script(source="local a = 1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]
    edit_text(doc.editor, "local a = 999 -- my edit\n")

    obj.properties["Source"] = "local a = 2\n"
    bridge.scene_changed.emit()
    check(doc.conflicted, "sanity: conflicted before Keep Local")

    doc._resolve_conflict_keep_local()
    check(doc.editor.toPlainText() == "local a = 999 -- my edit\n", "Keep Local preserves the local buffer")
    check(doc.is_dirty(), "Keep Local leaves the document dirty -- an explicit save is still required")
    check(not doc.conflicted, "Keep Local clears the conflict banner")

    print("test_keep_local_requires_explicit_later_save PASSED")


# ============================================================
# 6. Structured diagnostics: identity, session clearing, duplicates
# ============================================================

def test_diagnostics_apply_to_correct_open_document():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]

    bridge.lua_session_started.emit(1)
    bridge.lua_diagnostic.emit(obj.id, "error", "boom", 3, 1)

    check(3 in doc.editor._diagnostics_by_line, "a diagnostic for an OPEN document reaches its gutter immediately")
    check(doc.editor._diagnostics_by_line[3][0].severity == "error", "severity is preserved")
    check(doc.editor._diagnostics_by_line[3][0].message == "boom", "message is preserved")

    print("test_diagnostics_apply_to_correct_open_document PASSED")


def test_diagnostics_stored_for_unopened_script_until_opened():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)

    bridge.lua_session_started.emit(1)
    bridge.lua_diagnostic.emit(obj.id, "warning", "careful", 7, 1)
    check(obj.id not in ws._documents, "sanity: Script is not open yet")
    check(obj.id in ws._pending_diagnostics, "a diagnostic for an unopened Script is stored, not dropped")

    ws.open_script(obj.id)
    doc = ws._documents[obj.id]
    check(7 in doc.editor._diagnostics_by_line, "opening the Script applies its stored diagnostics immediately")

    print("test_diagnostics_stored_for_unopened_script_until_opened PASSED")


def test_new_session_clears_stale_diagnostics_and_ignores_stragglers():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    doc = ws._documents[obj.id]

    bridge.lua_session_started.emit(1)
    bridge.lua_diagnostic.emit(obj.id, "error", "session 1 error", 2, 1)
    check(2 in doc.editor._diagnostics_by_line, "sanity: session 1 diagnostic applied")

    bridge.lua_session_started.emit(2)
    check(doc.editor._diagnostics_by_line == {}, "starting a new Play session clears stale gutter markers, even before any new diagnostic arrives")
    check(ws._pending_diagnostics == {}, "the pending-diagnostics store is cleared on a new session too")

    # A straggler from the now-superseded session 1 must not reappear.
    bridge.lua_diagnostic.emit(obj.id, "error", "late straggler", 9, 1)
    check(doc.editor._diagnostics_by_line == {}, "a diagnostic tagged with an old session_id is dropped, not misattributed to the current session")

    bridge.lua_diagnostic.emit(obj.id, "error", "session 2 error", 5, 2)
    check(5 in doc.editor._diagnostics_by_line, "a diagnostic tagged with the CURRENT session_id is still applied")

    print("test_new_session_clears_stale_diagnostics_and_ignores_stragglers PASSED")


def test_duplicate_names_do_not_cross_pollute_diagnostics():
    obj_a = make_script(name="Dup")
    obj_b = make_script(name="Dup")
    bridge = make_bridge([obj_a, obj_b])
    ws = make_workspace(bridge)
    ws.open_script(obj_a.id)
    ws.open_script(obj_b.id)
    doc_a = ws._documents[obj_a.id]
    doc_b = ws._documents[obj_b.id]

    bridge.lua_session_started.emit(1)
    bridge.lua_diagnostic.emit(obj_a.id, "error", "only in A", 4, 1)

    check(4 in doc_a.editor._diagnostics_by_line, "diagnostic applies to the Script instance it names")
    check(doc_b.editor._diagnostics_by_line == {}, "a same-named sibling Script's gutter is untouched")

    print("test_duplicate_names_do_not_cross_pollute_diagnostics PASSED")


# ============================================================
# 7. Output-to-source navigation
# ============================================================

def test_navigate_to_diagnostic_opens_and_goes_to_line():
    obj = make_script(source="line1\nline2\nline3\nline4\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)

    ws.navigate_to_diagnostic(obj.id, 3)
    check(obj.id in ws._documents, "navigate_to_diagnostic opens the Script if it wasn't already")
    doc = ws._documents[obj.id]
    check(ws.currentWidget() is doc, "navigate_to_diagnostic focuses the Script's tab")
    check(doc.editor.textCursor().blockNumber() + 1 == 3, "the cursor lands on the exact reported line")

    print("test_navigate_to_diagnostic_opens_and_goes_to_line PASSED")


def test_navigate_to_deleted_script_does_not_redirect():
    obj_a = make_script(name="Target")
    obj_b = make_script(name="Target")  # same display name, different id
    bridge = make_bridge([obj_a, obj_b])
    ws = make_workspace(bridge)

    bridge.sync_delete(obj_a.id)
    shown = {}
    original_info = se.QMessageBox.information
    se.QMessageBox.information = staticmethod(lambda *a, **kw: shown.setdefault("called", True))
    try:
        ws.navigate_to_diagnostic(obj_a.id, 1)
    finally:
        se.QMessageBox.information = original_info

    check(shown.get("called"), "navigating to a deleted Script's stale id shows 'not found', not a redirect")
    check(obj_a.id not in ws._documents, "the deleted Script's id never gets an opened tab")
    check(obj_b.id not in ws._documents, "a same-named sibling is never opened as a substitute")

    print("test_navigate_to_deleted_script_does_not_redirect PASSED")


def test_output_panel_correlates_click_to_diagnostic_identity():
    obj = make_script()
    bridge = make_bridge([obj])
    panel = m.OutputPanel(bridge)

    navigated = []
    panel.set_navigate_callback(lambda script_id, line: navigated.append((script_id, line)))

    bridge.log("error", f"[LUA ERROR][{obj.name}:5] boom")
    bridge.lua_diagnostic.emit(obj.id, "error", "boom", 5, 1)

    check(len(panel._diagnostic_blocks) == 1, "exactly one Output block is tagged with diagnostic identity")
    block_number = next(iter(panel._diagnostic_blocks))
    panel._on_output_block_double_clicked(block_number)
    check(navigated == [(obj.id, 5)], "double-clicking the tagged block navigates using the STRUCTURED id/line, not parsed text")

    panel._clear_output()
    check(panel._diagnostic_blocks == {}, "Clear also drops stale block->diagnostic mappings")

    print("test_output_panel_correlates_click_to_diagnostic_identity PASSED")


# ============================================================
# 8. Save All / dirty-close prompts
# ============================================================

def test_save_all_saves_every_dirty_document():
    obj_a = make_script(name="A", source="a=1\n")
    obj_b = make_script(name="B", source="b=1\n")
    bridge = make_bridge([obj_a, obj_b])
    ws = make_workspace(bridge)
    ws.open_script(obj_a.id)
    ws.open_script(obj_b.id)
    edit_text(ws._documents[obj_a.id].editor, "a=2\n")
    # obj_b left clean deliberately.

    count = ws.save_all()
    check(count == 1, "save_all() only saves documents that are actually dirty")
    check(obj_a.properties["Source"] == "a=2\n", "the dirty document's Source is saved")
    check(not ws._documents[obj_a.id].is_dirty(), "save_all() cleans the document it saved")

    print("test_save_all_saves_every_dirty_document PASSED")


def test_prompt_save_all_before_closing_no_dirty_returns_true_without_dialog():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)

    result = ws.prompt_save_all_before_closing()
    check(result is True, "closing with nothing dirty proceeds without any prompt")

    print("test_prompt_save_all_before_closing_no_dirty_returns_true_without_dialog PASSED")


def test_prompt_save_all_before_closing_save_all_path():
    obj = make_script(source="x=1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    edit_text(ws._documents[obj.id].editor, "x=2\n")

    original_box = se.QMessageBox
    se.QMessageBox = _FakeMessageBox("Save All")
    try:
        result = ws.prompt_save_all_before_closing()
    finally:
        se.QMessageBox = original_box

    check(result is True, "choosing Save All allows the close to proceed")
    check(obj.properties["Source"] == "x=2\n", "Save All actually saved the dirty Source")

    print("test_prompt_save_all_before_closing_save_all_path PASSED")


def test_prompt_save_all_before_closing_cancel_path():
    obj = make_script(source="x=1\n")
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    ws.open_script(obj.id)
    edit_text(ws._documents[obj.id].editor, "x=2\n")

    original_box = se.QMessageBox
    se.QMessageBox = _FakeMessageBox("Cancel")
    try:
        result = ws.prompt_save_all_before_closing()
    finally:
        se.QMessageBox = original_box

    check(result is False, "Cancel blocks the close")
    check(obj.properties["Source"] == "x=1\n", "Cancel never saves anything")
    check(ws._documents[obj.id].is_dirty(), "Cancel leaves the document dirty")

    print("test_prompt_save_all_before_closing_cancel_path PASSED")


def test_tab_close_save_discard_cancel():
    for decision, expect_open, expect_saved in (("save", False, True), ("discard", False, False), ("cancel", True, False)):
        obj = make_script(source="x=1\n")
        bridge = make_bridge([obj])
        ws = make_workspace(bridge)
        ws.open_script(obj.id)
        edit_text(ws._documents[obj.id].editor, "x=2\n")
        index = ws.indexOf(ws._documents[obj.id])

        ws._prompt_save_discard_cancel = lambda _name, _decision=decision: _decision
        ws._on_tab_close_requested(index)

        check((obj.id in ws._documents) == expect_open, f"tab-close '{decision}': open-state mismatch")
        check((obj.properties["Source"] == "x=2\n") == expect_saved, f"tab-close '{decision}': saved-state mismatch")

    print("test_tab_close_save_discard_cancel PASSED")


# ============================================================
# 9. Focus-aware Undo/Redo routing (StudioMainWindow integration)
# ============================================================

def test_code_focus_routes_undo_redo_to_text_not_scene():
    obj = make_script(source="local a = 1\n")
    bridge = make_bridge([obj])
    win = m.StudioMainWindow(bridge)
    win.script_workspace.open_script(obj.id)
    doc = win.script_workspace._documents[obj.id]

    win.show()
    doc.editor.setFocus()
    QApplication.processEvents()
    check(win.script_workspace.focused_editor() is doc.editor, "the code editor is recognized as focused")

    scene_undo_calls = []
    bridge.undo = lambda: scene_undo_calls.append(1)
    doc.editor.insertPlainText("local b = 2\n")
    win._on_undo_triggered()
    check(len(scene_undo_calls) == 0, "Ctrl+Z with code focused never calls the scene CommandManager")
    check("local b = 2" not in doc.editor.toPlainText(), "Ctrl+Z with code focused undoes the text edit")

    win.close()

    print("test_code_focus_routes_undo_redo_to_text_not_scene PASSED")


def test_scene_focus_still_uses_scene_undo():
    bridge = make_bridge([])
    win = m.StudioMainWindow(bridge)
    win.script_workspace.setCurrentWidget(win.script_workspace.scene_widget)

    scene_undo_calls = []
    bridge.undo = lambda: scene_undo_calls.append(1)
    win._on_undo_triggered()
    check(len(scene_undo_calls) == 1, "Ctrl+Z with no code editor focused still calls the scene CommandManager")

    win.close()

    print("test_scene_focus_still_uses_scene_undo PASSED")


# ============================================================
# 10. Play with unsaved Source (StudioMainWindow integration)
# ============================================================

def test_play_guard_no_dirty_scripts_proceeds_silently():
    bridge = make_bridge([])
    win = m.StudioMainWindow(bridge)
    check(win._handle_play_guard() is True, "Play proceeds immediately when no Script tab is dirty")
    win.close()

    print("test_play_guard_no_dirty_scripts_proceeds_silently PASSED")


def test_play_guard_save_all_and_play():
    obj = make_script(source="x=1\n")
    bridge = make_bridge([obj])
    win = m.StudioMainWindow(bridge)
    win.script_workspace.open_script(obj.id)
    edit_text(win.script_workspace._documents[obj.id].editor, "x=2\n")

    original_box = m.QMessageBox
    m.QMessageBox = _FakeMessageBox("Save All and Play")
    try:
        result = win._handle_play_guard()
    finally:
        m.QMessageBox = original_box

    check(result is True, "Save All and Play allows Play to proceed")
    check(obj.properties["Source"] == "x=2\n", "Save All and Play actually saved the dirty Source first")
    # No win.close() here: saving Source correctly marks the SCENE dirty
    # too (Source edits go through the same bridge.set_property path as
    # any other property), which would make closeEvent()'s pre-existing
    # maybe_save() pop a real (unmocked) scene-save QMessageBox and block
    # forever under the offscreen platform. Each test already runs in its
    # own subprocess, so skipping close() here is harmless.

    print("test_play_guard_save_all_and_play PASSED")


def test_play_guard_play_saved_version_leaves_buffer_dirty():
    obj = make_script(source="x=1\n")
    bridge = make_bridge([obj])
    win = m.StudioMainWindow(bridge)
    win.script_workspace.open_script(obj.id)
    edit_text(win.script_workspace._documents[obj.id].editor, "x=2\n")

    original_box = m.QMessageBox
    m.QMessageBox = _FakeMessageBox("Play Saved Version")
    try:
        result = win._handle_play_guard()
    finally:
        m.QMessageBox = original_box

    check(result is True, "Play Saved Version allows Play to proceed")
    check(obj.properties["Source"] == "x=1\n", "Play Saved Version never saves the dirty buffer")
    check(win.script_workspace._documents[obj.id].is_dirty(), "the open tab remains dirty after Play Saved Version")
    # No win.close(): the Script tab is deliberately left dirty above, and
    # closeEvent()'s prompt_save_all_before_closing() would pop a real
    # (unmocked) QMessageBox for it and block forever offscreen -- see
    # test_play_guard_save_all_and_play's comment for the sibling case.

    print("test_play_guard_play_saved_version_leaves_buffer_dirty PASSED")


def test_play_guard_cancel_blocks_play():
    obj = make_script(source="x=1\n")
    bridge = make_bridge([obj])
    win = m.StudioMainWindow(bridge)
    win.script_workspace.open_script(obj.id)
    edit_text(win.script_workspace._documents[obj.id].editor, "x=2\n")

    original_box = m.QMessageBox
    m.QMessageBox = _FakeMessageBox("Cancel")
    try:
        result = win._handle_play_guard()
    finally:
        m.QMessageBox = original_box

    check(result is False, "Cancel blocks Play")
    check(obj.properties["Source"] == "x=1\n", "Cancel never saves anything")
    # No win.close(): see test_play_guard_save_all_and_play's comment --
    # the Script tab is still dirty here too.

    print("test_play_guard_cancel_blocks_play PASSED")


def test_play_guard_wired_into_bridge_play():
    """EngineBridge.play() itself must consult the guard -- this is what
    makes every existing Play trigger (Ribbon x2, Tests menu/F5, the
    `:play` console command) respect the prompt without individually
    being rewired."""
    obj = make_script(source="x=1\n")
    bridge = make_bridge([obj])
    win = m.StudioMainWindow(bridge)
    win.script_workspace.open_script(obj.id)
    edit_text(win.script_workspace._documents[obj.id].editor, "x=2\n")

    original_box = m.QMessageBox
    m.QMessageBox = _FakeMessageBox("Cancel")
    try:
        bridge.play()
    finally:
        m.QMessageBox = original_box

    check(bridge.is_playing is False, "bridge.play() itself is blocked by the guard, not just _handle_play_guard in isolation")
    # No win.close(): see test_play_guard_save_all_and_play's comment --
    # the Script tab is still dirty here too.

    print("test_play_guard_wired_into_bridge_play PASSED")


# ============================================================
# 11. Syntax highlighter multiline state
# ============================================================

def test_highlighter_tracks_multiline_long_comment_state():
    editor = se.LuaCodeEditor()
    edit_text(editor, "local a = 1\n--[[ this opens a long comment\nstill inside it\n]] local b = 2\n")

    doc = editor.document()
    block0 = doc.findBlockByNumber(1)  # the "--[[ this opens..." line
    block1 = doc.findBlockByNumber(2)  # "still inside it"
    block2 = doc.findBlockByNumber(3)  # "]] local b = 2"

    check(block0.userState() == se.LuaSyntaxHighlighter.STATE_COMMENT, "a line opening --[[ without closing ]] ends in comment state")
    check(block1.userState() == se.LuaSyntaxHighlighter.STATE_COMMENT, "a line entirely inside the long comment stays in comment state")
    check(block2.userState() == se.LuaSyntaxHighlighter.STATE_NORMAL, "the line closing ]] returns to normal state")

    print("test_highlighter_tracks_multiline_long_comment_state PASSED")


# ============================================================
# Explorer integration (open by double-click/Enter, non-Script untouched)
# ============================================================

def test_explorer_opens_script_and_ignores_other_types():
    script_obj = make_script(name="MyScript")
    part_obj = m.SceneObject(name="MyPart", object_type="Part", parent="Workspace")
    bridge = make_bridge([script_obj, part_obj])
    ws = make_workspace(bridge)
    panel = m.ExplorerPanel(bridge, ws)

    check(panel.activate_current() is False, "Enter with nothing selected does nothing")

    panel.tree.setCurrentItem(panel._find_item(panel.tree.invisibleRootItem(), part_obj.id))
    check(panel.activate_current() is False, "Enter on a non-Script instance does not open a Script tab")
    check(ws.count() == 1, "no tab was opened for the non-Script instance")

    panel.tree.setCurrentItem(panel._find_item(panel.tree.invisibleRootItem(), script_obj.id))
    check(panel.activate_current() is True, "Enter on a Script instance opens its tab")
    check(script_obj.id in ws._documents, "the opened tab is keyed by the Script's instance id")

    print("test_explorer_opens_script_and_ignores_other_types PASSED")


def test_inspector_open_script_button_routes_to_workspace():
    obj = make_script()
    bridge = make_bridge([obj])
    ws = make_workspace(bridge)
    inspector = m.InspectorPanel(bridge, ws)

    inspector._open_script_source_editor(obj.id, obj.name)
    check(obj.id in ws._documents, "Inspector's Open Script opens the SAME integrated workspace tab")

    print("test_inspector_open_script_button_routes_to_workspace PASSED")


# ============================================================
# Run everything
# ============================================================

ALL_TESTS = [
    test_open_by_id_and_dedup,
    test_same_name_separate_tabs,
    test_rename_updates_tab_title,
    test_reparent_preserves_open_tab,
    test_typing_marks_dirty_without_touching_bridge,
    test_save_calls_set_property_once_and_cleans,
    test_save_rejects_over_limit_without_truncating,
    test_utf8_and_multiline_preserved,
    test_clean_deletion_closes_tab,
    test_dirty_deletion_kept_for_recovery,
    test_clean_remote_refresh_updates_silently,
    test_dirty_remote_update_conflicts_without_overwriting,
    test_keep_local_requires_explicit_later_save,
    test_diagnostics_apply_to_correct_open_document,
    test_diagnostics_stored_for_unopened_script_until_opened,
    test_new_session_clears_stale_diagnostics_and_ignores_stragglers,
    test_duplicate_names_do_not_cross_pollute_diagnostics,
    test_navigate_to_diagnostic_opens_and_goes_to_line,
    test_navigate_to_deleted_script_does_not_redirect,
    test_output_panel_correlates_click_to_diagnostic_identity,
    test_save_all_saves_every_dirty_document,
    test_prompt_save_all_before_closing_no_dirty_returns_true_without_dialog,
    test_prompt_save_all_before_closing_save_all_path,
    test_prompt_save_all_before_closing_cancel_path,
    test_tab_close_save_discard_cancel,
    test_code_focus_routes_undo_redo_to_text_not_scene,
    test_scene_focus_still_uses_scene_undo,
    test_play_guard_no_dirty_scripts_proceeds_silently,
    test_play_guard_save_all_and_play,
    test_play_guard_play_saved_version_leaves_buffer_dirty,
    test_play_guard_cancel_blocks_play,
    test_play_guard_wired_into_bridge_play,
    test_highlighter_tracks_multiline_long_comment_state,
    test_explorer_opens_script_and_ignores_other_types,
    test_inspector_open_script_button_routes_to_workspace,
]


def _run_single(name):
    """Invoked as `python test_script_editor.py --one NAME` (see below) --
    runs exactly one test function in this process and exits 0/1."""
    for test in ALL_TESTS:
        if test.__name__ == name:
            test()
            return
    raise SystemExit(f"no such test: {name}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        _run_single(sys.argv[2])
        sys.exit(0)

    # Each test gets its OWN process/QApplication/native (offscreen) window
    # rather than sharing one across all ~34 tests in-process. Several
    # tests build a full StudioMainWindow (Ribbon/Explorer/Inspector/
    # ScriptEditorWorkspace, real icon rendering, a QApplication-level
    # focusChanged connection that outlives window.close()) -- empirically,
    # accumulating enough of those in a single process leads to an
    # intermittent native crash/hang partway through the suite that does
    # NOT reproduce for any test in isolation. Subprocess-per-test costs
    # more wall time but makes every test's result independent of run
    # order and of how many other tests ran before it.
    import subprocess

    failures = []
    for test in ALL_TESTS:
        result = subprocess.run(
            [sys.executable, __file__, "--one", test.__name__],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            failures.append(test.__name__)
            print(f"{test.__name__}: FAILED (exit={result.returncode})")
            if result.stdout.strip():
                print("  stdout:", result.stdout.strip().replace("\n", "\n  "))
            if result.stderr.strip():
                print("  stderr:", result.stderr.strip().replace("\n", "\n  "))

    print()
    if failures:
        print(f"{len(failures)} of {len(ALL_TESTS)} test function(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    print(f"All {len(ALL_TESTS)} test functions PASSED.")
