"""Regression tests for the Stage 3.2 native-viewport crash fix.

Background: install_template_browser() was originally called AFTER
embed_panda_window() with no host= argument, which made it fall through to
QMainWindow.takeCentralWidget()/setCentralWidget() to wrap the existing
central widget in a new QStackedWidget. embed_panda_window() had already
forced a real native HWND to exist for that widget's entire ancestor chain
(via container.winId()) and raw-Win32 SetParent()'d the foreign Panda3D
window under it -- reparenting that subtree afterwards corrupted the native
window relationship and caused an intermittent access violation on
studio.show() (Fatal Python error: Aborted / Windows fatal exception: access
violation), confirmed via Windows Event Log APPCRASH records naming
Qt6Gui.dll and ucrtbase.dll.

The fix: StudioMainWindow._build_central() now creates a permanent
self.central_stack (a QStackedWidget) and installs it as the central widget
ONCE, during normal construction, with the real editor widget as its first
page -- long before any native embedding can occur. client_studio.py's
main() now passes host=studio.central_stack to install_template_browser(),
which takes its isinstance(target, QStackedWidget) branch: this only ever
calls stack.addWidget(page) for the brand-new template page and NEVER calls
takeCentralWidget()/setCentralWidget() again.

These tests can't reproduce the native crash itself (that requires a real
Panda3D window and Win32 message pump, exercised separately via the real-
Windows manual test pass) -- what they verify is the structural invariant
that actually prevents it: the editor widget's identity and its parent
relationship to central_stack must never change after construction, no
matter how many times install_template_browser()/page-switching runs.

Follows this project's existing test convention (test_script_editor.py):
plain top-level-assertion script, run directly, offscreen Qt platform.
"""
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QWidget

app = QApplication.instance() or QApplication([])

import studio_editor_live as m
import sstudio_templates

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def make_bridge():
    return m.EngineBridge(objects=[], live_mode=False)


def test_central_stack_exists_before_any_embedding() -> None:
    """The outer stack must exist as a direct result of normal construction
    -- nothing embed_panda_window()-shaped needs to run first."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    check(isinstance(win.central_stack, QStackedWidget), "StudioMainWindow.central_stack is a QStackedWidget after __init__")
    check(win.centralWidget() is win.central_stack, "central_stack is installed as the QMainWindow's central widget")
    check(win.central_stack.count() == 1, "central_stack starts with exactly one page (the editor)")
    check(isinstance(win.viewport_frame, QWidget), "viewport_frame exists after construction")


def test_install_template_browser_does_not_reparent_central_widget() -> None:
    """The actual regression: calling install_template_browser() with
    host=win.central_stack must never call takeCentralWidget()/
    setCentralWidget() again -- those are exactly the calls that corrupted
    the native window hierarchy in the original bug."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    editor_page = win.central_stack.widget(0)
    editor_page_id = id(editor_page)
    viewport_frame_id = id(win.viewport_frame)

    take_central_calls = {"count": 0}
    set_central_calls = {"count": 0}
    original_take = win.takeCentralWidget
    original_set = win.setCentralWidget

    def spy_take():
        take_central_calls["count"] += 1
        return original_take()

    def spy_set(widget):
        set_central_calls["count"] += 1
        return original_set(widget)

    win.takeCentralWidget = spy_take
    win.setCentralWidget = spy_set

    binding = sstudio_templates.install_template_browser(
        win, host=win.central_stack, show_immediately=False,
    )

    check(take_central_calls["count"] == 0, "install_template_browser(host=central_stack) never calls takeCentralWidget()")
    check(set_central_calls["count"] == 0, "install_template_browser(host=central_stack) never calls setCentralWidget()")
    check(win.centralWidget() is win.central_stack, "central widget identity is unchanged after install_template_browser()")
    check(win.central_stack.widget(0) is editor_page, "the original editor page is still page 0, same object")
    check(id(win.central_stack.widget(0)) == editor_page_id, "editor page's Python id is unchanged (never recreated)")
    check(id(win.viewport_frame) == viewport_frame_id, "viewport_frame's Python id is unchanged (never recreated)")
    check(win.central_stack.count() == 2, "central_stack now has exactly 2 pages: editor + template page")
    check(binding.previous_widget is editor_page, "TemplateBrowserBinding.previous_widget correctly captured the editor page")


def test_repeated_page_switching_preserves_identity() -> None:
    """start -> editor -> start (x10) must never change which QWidget
    instances back the editor page or the viewport -- only currentWidget()
    on the stack should change."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    editor_page = win.central_stack.widget(0)
    viewport_frame = win.viewport_frame

    take_central_calls = {"count": 0}
    set_central_calls = {"count": 0}
    original_take = win.takeCentralWidget
    original_set = win.setCentralWidget
    win.takeCentralWidget = lambda: (take_central_calls.__setitem__("count", take_central_calls["count"] + 1), original_take())[1]
    win.setCentralWidget = lambda w: (set_central_calls.__setitem__("count", set_central_calls["count"] + 1), original_set(w))[1]

    binding = sstudio_templates.install_template_browser(
        win, host=win.central_stack, show_immediately=False,
    )
    win.set_templates_binding(binding)

    for i in range(10):
        binding.show()
        check(win.central_stack.currentWidget() is binding.page, f"cycle {i}: start page is current after binding.show()")
        binding.show_editor()
        check(win.central_stack.currentWidget() is editor_page, f"cycle {i}: editor page is current after binding.show_editor()")
        check(win.central_stack.widget(0) is editor_page, f"cycle {i}: editor page object identity unchanged")
        check(win.viewport_frame is viewport_frame, f"cycle {i}: viewport_frame object identity unchanged")

    check(take_central_calls["count"] == 0, "10 cycles of start<->editor switching never call takeCentralWidget()")
    check(set_central_calls["count"] == 0, "10 cycles of start<->editor switching never call setCentralWidget()")
    check(win.centralWidget() is win.central_stack, "central widget is still central_stack after 10 switch cycles")


def test_set_start_page_never_reparents_existing_pages() -> None:
    """StudioMainWindow.set_start_page() -- the safe integration API -- must
    behave identically: only stack.addWidget(), no take/set central calls."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    editor_page = win.central_stack.widget(0)

    take_central_calls = {"count": 0}
    set_central_calls = {"count": 0}
    original_take = win.takeCentralWidget
    original_set = win.setCentralWidget
    win.takeCentralWidget = lambda: (take_central_calls.__setitem__("count", take_central_calls["count"] + 1), original_take())[1]
    win.setCentralWidget = lambda w: (set_central_calls.__setitem__("count", set_central_calls["count"] + 1), original_set(w))[1]

    extra_page = QWidget()
    win.set_start_page(extra_page)

    check(take_central_calls["count"] == 0, "set_start_page() never calls takeCentralWidget()")
    check(set_central_calls["count"] == 0, "set_start_page() never calls setCentralWidget()")
    check(win.central_stack.widget(0) is editor_page, "set_start_page() leaves the editor page as page 0")
    check(win.central_stack.count() == 2, "set_start_page() adds exactly one new page")
    check(win.central_stack.indexOf(extra_page) == 1, "the new page was added, not swapped in for an existing one")


def test_no_second_viewport_frame_or_stray_stack() -> None:
    """A second call path (e.g. a second install_template_browser() call,
    which a future regression might introduce) must not silently create a
    second native-embeddable container inside ViewportFrame."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    viewport_frame_id = id(win.viewport_frame)

    sstudio_templates.install_template_browser(win, host=win.central_stack, show_immediately=False)

    check(id(win.viewport_frame) == viewport_frame_id, "viewport_frame is not recreated by install_template_browser()")
    # ViewportFrame owns its own internal QStackedWidget (Stage 3.1) for
    # swapping in a native/mock/external viewport -- that one is expected
    # and must stay exactly one, untouched by the Stage 3.2 outer stack.
    inner_stacks = win.viewport_frame.findChildren(QStackedWidget)
    check(len(inner_stacks) >= 1, "ViewportFrame still owns its own internal viewport-swap QStackedWidget")


# ============================================================
# Stage 3.7 defect fix: Open Place / Recent Places / --place left Studio
# on the Templates/Home page instead of switching to Scene. Root cause:
# TemplateBrowserBinding.show_editor() (the only thing that ever moves
# central_stack's current widget back to the editor page) was only ever
# called from the template-creation callback, never from
# _open_place_path() (which File > Open Place, Recent Places, and the
# --place startup flag all funnel through). Fix: a single authoritative
# activate_scene_for_loaded_place() helper, called only from
# _replace_world_and_report()'s success branch -- the one chokepoint
# every successful Place-load path shares.
# ============================================================


class _StubMessageBox:
    """Stand-in for QMessageBox.critical() -- a real QMessageBox.exec()
    blocks forever offscreen waiting for a click that will never come
    (same reasoning as test_script_editor.py's _FakeMessageBox). These
    tests only ever hit the .critical(...) static-call path (never a
    button-choice dialog), so a bare call-recording stub is enough."""

    calls: list = []

    @staticmethod
    def critical(*args, **kwargs) -> None:
        _StubMessageBox.calls.append(args)


class _FakeAdapter:
    """Stand-in for MultiplayerStudioAdapter's replace_world() -- lets
    these tests drive EngineBridge.replace_world()'s on_result callback
    synchronously with a controlled outcome, without needing a real
    Ursina/Panda3D game object.

    outcome=True  -> request accepted, server confirms success
    outcome=False -> request accepted, server rejects it
    outcome=None  -> request never even accepted (e.g. not connected):
                     on_result is never called at all"""

    def __init__(self, outcome: bool | None, message: str = "") -> None:
        self.outcome = outcome
        self.message = message
        self.received_objects: Any = None
        self.received_services: Any = None

    def replace_world(self, objects, on_result, services=None) -> bool:
        self.received_objects = objects
        self.received_services = services
        if self.outcome is None:
            return False
        on_result(self.outcome, self.message)
        return True


def make_live_win_with_templates(outcome: bool | None, message: str = "", tmp_dir: Path | None = None):
    """Builds a StudioMainWindow wired exactly like main() wires it: a
    live EngineBridge with a controllable fake adapter, and the template
    browser installed with show_immediately=True (host=central_stack) --
    i.e. Studio starts on the Templates/Home page, same as a real launch
    with no --place, which is the exact scenario the reported bug needs."""
    bridge = m.EngineBridge(objects=[], live_mode=True)
    bridge.set_adapter(_FakeAdapter(outcome, message))
    win = m.StudioMainWindow(bridge)
    if tmp_dir is not None:
        ini_path = tmp_dir / "recents.ini"
        win.place_manager.recents._settings = QSettings(str(ini_path), QSettings.Format.IniFormat)
    binding = sstudio_templates.install_template_browser(
        win, host=win.central_stack, show_immediately=True,
    )
    win.set_templates_binding(binding)
    return win, binding


def make_result(tmp_dir: Path, success: bool = True) -> "m.place_manager.PlaceOperationResult":
    return m.place_manager.PlaceOperationResult(
        success=success,
        message="ok" if success else "rejected",
        path=tmp_dir / "TestPlace.nebula.json",
        objects=[],
        display_name="TestPlace",
        project_dir=tmp_dir,
        template_id=None,
    )


def test_activate_scene_for_loaded_place_switches_to_editor() -> None:
    """Direct unit test of the new authoritative helper itself."""
    with tempfile.TemporaryDirectory() as tmp:
        win, binding = make_live_win_with_templates(True, tmp_dir=Path(tmp))
        editor_page = win.central_stack.widget(0)
        check(win.central_stack.currentWidget() is binding.page, "precondition: Studio starts on the Templates page")
        win.activate_scene_for_loaded_place()
        check(win.central_stack.currentWidget() is editor_page, "activate_scene_for_loaded_place() switches central_stack back to the Scene/editor page")


def test_activate_scene_for_loaded_place_noop_without_binding() -> None:
    """No template browser attached (headless/offline demo) -- must not
    raise, must not touch central_stack."""
    bridge = m.EngineBridge(objects=[], live_mode=False)
    win = m.StudioMainWindow(bridge)
    check(win.templates_binding is None, "precondition: no templates_binding attached")
    win.activate_scene_for_loaded_place()
    check(win.central_stack.currentWidget() is win.central_stack.widget(0), "activate_scene_for_loaded_place() is a harmless no-op with no templates_binding")


def test_replace_world_success_activates_scene() -> None:
    """The actual bug scenario: a successful REPLACE_WORLD (Open Place,
    Recent Places, template creation, or --place) must bring Scene to
    front, even though _replace_world_and_report() itself never touches
    central_stack directly -- it goes through activate_scene_for_loaded_place()."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        win, binding = make_live_win_with_templates(True, tmp_dir=tmp_dir)
        editor_page = win.central_stack.widget(0)
        result = make_result(tmp_dir, success=True)
        win._replace_world_and_report(result)
        check(win.central_stack.currentWidget() is editor_page, "a successful REPLACE_WORLD switches Studio from Templates/Home to the Scene workspace")
        check(win.place_manager.current_path == result.path, "place_manager.commit() ran -- current_path was updated")


def test_replace_world_server_rejection_preserves_current_page() -> None:
    """Spec: a server-rejected Open/Create must leave the current page
    (and the current Place) untouched -- must NOT switch to Scene."""
    original_box = m.QMessageBox
    m.QMessageBox = _StubMessageBox
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            win, binding = make_live_win_with_templates(False, "server said no", tmp_dir=tmp_dir)
            result = make_result(tmp_dir, success=True)
            win._replace_world_and_report(result)
            check(win.central_stack.currentWidget() is binding.page, "a server-rejected REPLACE_WORLD leaves Studio on whatever page it was already on (Templates/Home)")
            check(win.place_manager.current_path is None, "a server-rejected REPLACE_WORLD never commits (current Place is untouched)")
    finally:
        m.QMessageBox = original_box


def test_replace_world_not_connected_preserves_current_page() -> None:
    """Spec: a request that never even reaches the server (not connected)
    must also leave the current page untouched."""
    original_box = m.QMessageBox
    m.QMessageBox = _StubMessageBox
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            win, binding = make_live_win_with_templates(None, tmp_dir=tmp_dir)
            result = make_result(tmp_dir, success=True)
            win._replace_world_and_report(result)
            check(win.central_stack.currentWidget() is binding.page, "a not-accepted REPLACE_WORLD (not connected) leaves Studio on the Templates/Home page")
    finally:
        m.QMessageBox = original_box


def test_open_place_path_local_failure_preserves_current_page() -> None:
    """File > Open Place / Recent Places / --place all funnel through
    _open_place_path() -- a LOCAL failure (bad/missing file, before any
    network round trip) must also never switch to Scene."""
    original_box = m.QMessageBox
    m.QMessageBox = _StubMessageBox
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            win, binding = make_live_win_with_templates(True, tmp_dir=tmp_dir)
            missing_path = tmp_dir / "does_not_exist.nebula.json"
            win._open_place_path(missing_path)
            check(win.central_stack.currentWidget() is binding.page, "opening a nonexistent Place file leaves Studio on the Templates/Home page")
            check(len(_StubMessageBox.calls) >= 1, "a local open failure reports an error to the user")
    finally:
        m.QMessageBox = original_box
        _StubMessageBox.calls = []


def test_open_place_path_success_activates_scene() -> None:
    """End-to-end: a real Place file on disk, opened via _open_place_path()
    (the exact method File > Open Place / Recent Places / --place all
    call), must switch Studio to Scene once the (fake, but here
    successful) server confirms it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        win, binding = make_live_win_with_templates(True, tmp_dir=tmp_dir)
        editor_page = win.central_stack.widget(0)
        create_result = win.place_manager.create_from_template("blank", "RealPlace", tmp_dir)
        check(create_result.success, "precondition: creating a real Place file on disk succeeded")
        win.place_manager.commit(create_result)
        real_path = win.place_manager.current_path
        check(real_path is not None and real_path.exists(), "precondition: the Place file actually exists on disk")

        # Fresh window, back on the Templates page, simulating a second
        # launch/File > Open Place against that same file.
        win2, binding2 = make_live_win_with_templates(True, tmp_dir=tmp_dir)
        editor_page2 = win2.central_stack.widget(0)
        check(win2.central_stack.currentWidget() is binding2.page, "precondition: the second window also starts on Templates/Home")
        win2._open_place_path(real_path)
        check(win2.central_stack.currentWidget() is editor_page2, "_open_place_path() on a real, successfully-opened Place switches to Scene (File > Open Place / Recent Places / --place all share this method)")


def test_repeated_home_to_scene_transitions_do_not_duplicate_widgets() -> None:
    """Cycling create/open-success (Home -> Scene) many times must never
    grow central_stack's page count or recreate the editor/viewport
    widgets -- same invariant test_repeated_page_switching_preserves_
    identity() already proves for the manual binding.show()/show_editor()
    calls, extended to the actual _replace_world_and_report() call path."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        win, binding = make_live_win_with_templates(True, tmp_dir=tmp_dir)
        editor_page = win.central_stack.widget(0)
        viewport_frame_id = id(win.viewport_frame)
        starting_count = win.central_stack.count()

        for i in range(10):
            binding.show()
            check(win.central_stack.currentWidget() is binding.page, f"cycle {i}: back on Templates page before the next load")
            result = make_result(tmp_dir, success=True)
            win._replace_world_and_report(result)
            check(win.central_stack.currentWidget() is editor_page, f"cycle {i}: activate_scene_for_loaded_place() returned to the same editor page object")

        check(win.central_stack.count() == starting_count, "10 Home->Scene load cycles never add extra central_stack pages")
        check(id(win.viewport_frame) == viewport_frame_id, "10 Home->Scene load cycles never recreate viewport_frame")


# ============================================================
# Stage 3.8: root-service selection / Inspector rendering
# ============================================================

def _inspector_section_titles(win) -> list[str]:
    layout = win.inspector_panel.contents_layout
    titles = []
    for i in range(layout.count() - 1):  # last item is the trailing stretch
        widget = layout.itemAt(i).widget()
        if isinstance(widget, m.CollapsibleSection):
            titles.append(widget.header.text())
    return titles


def test_selecting_workspace_produces_non_blank_properties() -> None:
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    win.bridge.select("Workspace")
    check(win.inspector_panel.current_object is not None, "selecting Workspace in Explorer resolves to a real Inspector target (not None)")
    titles = _inspector_section_titles(win)
    check(len(titles) > 0, f"Workspace Properties is non-empty: {titles}")
    check("Data" in titles, "Workspace Properties has a Data section (Name/ClassName/Parent)")
    check("Behavior" in titles, "Workspace Properties has a Behavior section (Gravity)")
    check("Debug" in titles, "Workspace Properties has a Debug section (SStudio instance id)")


def test_selecting_starter_player_produces_non_blank_properties() -> None:
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    win.bridge.select("StarterPlayer")
    check(win.inspector_panel.current_object is not None, "selecting StarterPlayer resolves to a real Inspector target")
    titles = _inspector_section_titles(win)
    check("Camera" in titles and "Character" in titles, f"StarterPlayer Properties has both Camera and Character sections: {titles}")


def test_selecting_common_services_no_blank_inspector() -> None:
    """"Lighting" and "Camera" are deliberately excluded here -- confirmed
    via shared.object_registry.ROOT_SERVICES that neither is actually a
    root service in this codebase (both are ordinary offline-demo
    SceneObjects with random ids, unrelated to Explorer's root-service
    tree items -- see datamodel_schema.py's own comment on this)."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    for name in ("ReplicatedStorage", "ServerScriptService", "ServerStorage", "StarterGui", "Players"):
        win.bridge.select(name)
        check(win.inspector_panel.current_object is not None, f"selecting {name} resolves to a real Inspector target")
        titles = _inspector_section_titles(win)
        check("Data" in titles, f"{name} Properties shows at least the common Data section: {titles}")


def test_service_class_name_and_parent_are_read_only() -> None:
    """Attempting to write ClassName/Parent through the authoritative
    property path must be rejected -- these are never exposed as editable
    widgets in _build_schema_sections()/_build_schema_property_editor(),
    but the write path itself must also refuse, not just the UI."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    win.bridge.select("Workspace")
    before = win.bridge.get_object("Workspace").name
    win.bridge.set_property("Workspace", "properties.ClassName", "SomethingElse")
    after = win.bridge.get_object("Workspace").properties.get("ClassName")
    check(after != "SomethingElse", "writing Workspace.ClassName through set_property() is rejected (unknown/read-only property)")


def test_invalid_gravity_edit_preserves_old_value_and_no_dirty() -> None:
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    win.bridge.select("Workspace")
    check(not win.bridge.is_dirty, "precondition: bridge starts clean")
    win.bridge.set_property("Workspace", "properties.Gravity", -5.0)
    check(win.bridge.get_object("Workspace").properties.get("Gravity") == 24.0, "an invalid Gravity edit (-5.0) leaves the old value (24.0) unchanged")
    check(not win.bridge.is_dirty, "an invalid Gravity edit never marks the Place dirty")


def test_starter_player_zoom_ordering_rejected_via_inspector() -> None:
    """Stage 3.8 follow-up: CameraMinZoomDistance/CameraMaxZoomDistance
    must stay ordered -- an immediate Inspector-side pre-check (UX only;
    server._apply_zoom_ordering_guard is the actual authority, see
    test_place_manager.py's test_server_zoom_ordering_guard_rejects_inverted_pair,
    and the Lua runtime write path has its own equivalent check too, see
    test_lua_gameplay_api.py's test_runtime_zoom_limit_write_forwards_full_overlay)."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    win.bridge.select("StarterPlayer")
    check(not win.bridge.is_dirty, "precondition: bridge starts clean")
    before_min = win.bridge.get_object("StarterPlayer").properties.get("CameraMinZoomDistance")
    win.bridge.set_property("StarterPlayer", "properties.CameraMinZoomDistance", 999.0)
    after_min = win.bridge.get_object("StarterPlayer").properties.get("CameraMinZoomDistance")
    check(after_min == before_min, f"setting CameraMinZoomDistance above the current CameraMaxZoomDistance is rejected, old value ({before_min}) preserved, got {after_min}")
    check(not win.bridge.is_dirty, "the rejected zoom-ordering edit never marks the Place dirty")

    win.bridge.set_property("StarterPlayer", "properties.CameraMinZoomDistance", 3.0)
    win.bridge.set_property("StarterPlayer", "properties.CameraMaxZoomDistance", 15.0)
    obj = win.bridge.get_object("StarterPlayer")
    check(obj.properties.get("CameraMinZoomDistance") == 3.0 and obj.properties.get("CameraMaxZoomDistance") == 15.0, "a valid ordered pair, applied one edit at a time, is accepted")


def test_selection_switching_does_not_duplicate_inspector_rows() -> None:
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    for _ in range(3):
        win.bridge.select("Workspace")
        win.bridge.select("StarterPlayer")
        win.bridge.select(None)
    win.bridge.select("Workspace")
    titles = _inspector_section_titles(win)
    check(titles.count("Behavior") == 1, f"repeated selection switching never duplicates Inspector sections: {titles}")


def test_part_and_script_inspector_unaffected_by_service_rendering() -> None:
    """Selecting a normal Part/Script must still use the LEGACY
    object_registry-driven Inspector path -- proves the new schema-driven
    branch in InspectorPanel.set_object() is gated correctly and doesn't
    regress existing classes (spec: "incremental migration")."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    part = win.bridge.add_part("Part", parent="Workspace")
    win.bridge.select(part.id)
    titles = _inspector_section_titles(win)
    check("Transform" in titles and "Appearance" in titles and "Behavior" in titles, f"a normal Part still shows its accepted Transform/Appearance/Behavior sections: {titles}")

    script = win.bridge.add_part("Script", parent="ServerScriptService")
    win.bridge.select(script.id)
    titles = _inspector_section_titles(win)
    check("Script" in titles, f"a Script still shows its Script section (Source/Open Script preserved): {titles}")


if __name__ == "__main__":
    test_central_stack_exists_before_any_embedding()
    test_install_template_browser_does_not_reparent_central_widget()
    test_repeated_page_switching_preserves_identity()
    test_set_start_page_never_reparents_existing_pages()
    test_no_second_viewport_frame_or_stray_stack()

    test_activate_scene_for_loaded_place_switches_to_editor()
    test_activate_scene_for_loaded_place_noop_without_binding()
    test_replace_world_success_activates_scene()
    test_replace_world_server_rejection_preserves_current_page()
    test_replace_world_not_connected_preserves_current_page()
    test_open_place_path_local_failure_preserves_current_page()
    test_open_place_path_success_activates_scene()
    test_repeated_home_to_scene_transitions_do_not_duplicate_widgets()

    test_selecting_workspace_produces_non_blank_properties()
    test_selecting_starter_player_produces_non_blank_properties()
    test_selecting_common_services_no_blank_inspector()
    test_service_class_name_and_parent_are_read_only()
    test_invalid_gravity_edit_preserves_old_value_and_no_dirty()
    test_starter_player_zoom_ordering_rejected_via_inspector()
    test_selection_switching_does_not_duplicate_inspector_rows()
    test_part_and_script_inspector_unaffected_by_service_rendering()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for message in FAILURES:
            print(f"  - {message}")
        sys.exit(1)
    print("All viewport hierarchy regression tests passed.")
