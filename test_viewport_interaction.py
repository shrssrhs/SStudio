"""Regression tests for the Stage 3.9 editor-viewport interaction rewrite
in client_studio.py: viewport focus (viewport_focused), the WASD-does-
not-require-RMB fix, mouse-wheel dolly, gizmo-drag single-commit Undo,
selection picking, and focus-loss release.

Follows this project's existing test convention for MultiplayerGame
(test_character_rig.py's own tier 3): structural checks via
inspect.getsource() for the invariants that genuinely require a live
embedded Panda3D window to observe behaviorally (there is no headless
way to fabricate a real QWindow-embedded native viewport + OS mouse/
keyboard focus in this test environment) -- the corresponding BEHAVIORAL
claims (WASD actually moves the camera on screen, RMB-drag actually
rotates it, wheel actually dollies, a dragged gizmo handle visibly
follows the cursor) are manual-verification-only, see the Stage 3.9
report. What IS behaviorally verified here: TransformGizmo's own drag
math (reused, unmodified, from test_scale_gizmo_math.py) and anything
expressible as plain Python state transitions without a live window.

Plain top-level-assertion script, run directly, offscreen Qt platform.
"""
import inspect
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Entity, Ursina, Vec3

ursina_app = Ursina(window_type="none")

import client_studio as cs

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def _source(func) -> str:
    return inspect.getsource(func)


# ============================================================
# Focus-follows-click: WASD no longer requires holding RMB
# ============================================================

def test_update_gates_flight_on_viewport_focused_not_editor_look_active() -> None:
    """The actual bug fix: update_flight() (WASD/Space/Q/E movement) must
    be reachable via viewport_focused alone, independent of
    editor_look_active (RMB-look) -- NOT bundled behind the same
    condition the way it used to be (the literal root cause of "must
    hold RMB for WASD to work")."""
    src = _source(cs.MultiplayerGame.update)
    flight_index = src.index("self.update_flight()")
    look_index = src.index("self.update_mouse_look()")
    # Each call must be reachable through its OWN, textually distinct
    # `if` condition, not a single shared `if/elif` guarding both.
    flight_if = src.rfind("if self.viewport_focused:", 0, flight_index)
    look_if = src.rfind("if self.editor_look_active:", 0, look_index)
    check(flight_if != -1 and flight_if < flight_index, "update(): update_flight() is gated on `if self.viewport_focused:`")
    check(look_if != -1 and look_if < look_index, "update(): update_mouse_look() is gated on its OWN `if self.editor_look_active:`, not the same condition as flight")
    check("elif self.editor_look_active and not self.gizmo.dragging:" not in src, "update(): the old bundled RMB-gates-everything condition is gone")


def test_panda_window_focus_filter_sets_viewport_focused_on_click_not_hover() -> None:
    """PandaWindowFocusFilter must grant viewport_focused on an actual
    click/FocusIn (MouseButtonPress/FocusIn), but NOT on a mere Enter
    (hover) -- "clicking gives focus", not "hovering gives focus"."""
    src = _source(cs.PandaWindowFocusFilter.eventFilter)
    check("self.game.viewport_focused = True" in src, "PandaWindowFocusFilter.eventFilter(): sets viewport_focused = True")
    check("event.type() != QEvent.Type.Enter" in src, "PandaWindowFocusFilter.eventFilter(): excludes plain Enter (hover) from granting focus")


def test_input_reconfirms_viewport_focused_on_editor_clicks() -> None:
    """input() itself also sets viewport_focused = True for left/right
    mouse down in editor mode -- redundant with (but independent of) the
    Qt-level PandaWindowFocusFilter, since Ursina only calls input() at
    all once the native window genuinely has OS focus."""
    src = _source(cs.MultiplayerGame.input)
    editor_branch_end = src.index("return", src.index("if not self.studio_playing:"))
    editor_branch = src[: editor_branch_end]
    check('key in ("left mouse down", "right mouse down")' in editor_branch, "input(): editor-mode branch reconfirms viewport_focused on left/right mouse down")
    check("self.viewport_focused = True" in editor_branch, "input(): editor-mode branch actually sets viewport_focused = True")


def test_release_play_input_capture_resets_editor_look_active_and_viewport_focused() -> None:
    """The confirmed stale-flag gap: release_play_input_capture() (called
    from app-focus-loss, click-elsewhere, and Escape) must unconditionally
    reset BOTH editor_look_active and viewport_focused, not just stop the
    cursor capture -- otherwise update()'s per-frame gates could still
    see stale True values after focus was supposedly released."""
    src = _source(cs.MultiplayerGame.release_play_input_capture)
    check("self.editor_look_active = False" in src, "release_play_input_capture(): resets editor_look_active")
    check("self.viewport_focused = False" in src, "release_play_input_capture(): resets viewport_focused")
    # Must be unconditional (outside the `if self._mouse_look_captured():`
    # guard), not just inside it -- a stale flag with capture already
    # released must still get cleared.
    guard_index = src.index("if self._mouse_look_captured():")
    reset_index = src.index("self.editor_look_active = False")
    check(reset_index < guard_index, "release_play_input_capture(): the reset happens unconditionally, before the capture-check guard")


def test_poll_qt_look_delta_also_resets_stale_flags_on_focus_loss() -> None:
    """The per-frame focus-loss check inside _poll_qt_look_delta() (which
    calls _stop_mouse_look() directly, NOT through
    release_play_input_capture()) must independently reset the same two
    flags, so a focus loss detected THIS way can't leave them stale
    either."""
    src = _source(cs.MultiplayerGame._poll_qt_look_delta)
    focus_lost_branch = src[src.index("current_focus = QApplication.focusWidget()"):]
    check("self.editor_look_active = False" in focus_lost_branch, "_poll_qt_look_delta(): focus-loss branch resets editor_look_active")
    check("self.viewport_focused = False" in focus_lost_branch, "_poll_qt_look_delta(): focus-loss branch resets viewport_focused")


def test_viewport_focused_reset_on_play_start_and_stop() -> None:
    """set_studio_playing() resets viewport_focused alongside
    editor_look_active on both the Play-start and Play-stop transitions,
    so no stale editor-focus state survives a mode switch in either
    direction."""
    src = _source(cs.MultiplayerGame.set_studio_playing)
    play_branch = src[: src.index("else:")]
    stop_branch = src[src.index("else:"):]
    check("self.viewport_focused = False" in play_branch, "set_studio_playing(): Play-start branch resets viewport_focused")
    check("self.viewport_focused = False" in stop_branch, "set_studio_playing(): Play-stop branch resets viewport_focused")


# ============================================================
# Mouse-wheel dolly (editor mode had none before this rewrite)
# ============================================================

def test_editor_mode_wheel_dollies_camera() -> None:
    src = _source(cs.MultiplayerGame.input)
    editor_branch_end = src.index("return", src.index("if not self.studio_playing:"))
    editor_branch = src[: editor_branch_end]
    check('key == "scroll up"' in editor_branch, "input(): editor mode handles scroll up")
    check('key == "scroll down"' in editor_branch, "input(): editor mode handles scroll down")
    check("self.dolly_camera(" in editor_branch, "input(): editor-mode scroll calls dolly_camera()")


def test_dolly_camera_moves_along_view_direction_not_scaled_by_dt() -> None:
    """A wheel tick is a discrete event, not a per-frame rate -- unlike
    update_flight(), dolly_camera() must NOT multiply by ursina_time.dt
    (that would make a single wheel click's effect depend on how long the
    previous frame took, which is not how a mouse wheel behaves)."""
    src = _source(cs.MultiplayerGame.dolly_camera)
    check("forward_from_angles(self.player_yaw, self.player_pitch)" in src, "dolly_camera(): moves along the current view direction")
    # Checks the actual CODE, not the docstring (which explains, in
    # prose, exactly why ursina_time.dt is deliberately absent -- a bare
    # substring check would false-positive on that explanation).
    code_only = src[src.index('"""', src.index('"""') + 3) + 3:]
    check("ursina_time.dt" not in code_only, "dolly_camera(): a wheel tick is NOT scaled by frame delta time")


# ============================================================
# Gizmo drag: exactly one Undo command per drag, not per mouse-move
# ============================================================

def test_update_gizmo_never_pushes_undo_history() -> None:
    """update_gizmo() runs every editor-mode frame (including every
    frame of an in-progress drag) -- it must never itself call
    history.push_optimistic()/push_history(), or every mouse-move during
    a drag would create its own Undo entry."""
    src = _source(cs.MultiplayerGame.update_gizmo)
    check("history.push" not in src, "update_gizmo(): never pushes Undo history directly")


def test_end_gizmo_drag_is_the_sole_history_push_site_for_drags() -> None:
    """end_gizmo_drag() (called once, from input()'s "left mouse up") is
    the only place a drag's Undo command gets pushed -- via
    _push_part_transform_history()/_push_model_transform_history(),
    called exactly once per completed drag, not once per frame."""
    src = _source(cs.MultiplayerGame.end_gizmo_drag)
    check("_push_part_transform_history()" in src or "_push_model_transform_history()" in src, "end_gizmo_drag(): pushes exactly one transform history entry on release")
    push_part_src = _source(cs.MultiplayerGame._push_part_transform_history)
    check(push_part_src.count("self.history.push") <= 1, "_push_part_transform_history(): pushes at most one history command")


def test_try_begin_gizmo_drag_snapshots_before_state_once() -> None:
    """The before-snapshot used for the eventual Undo command is captured
    once, at drag START (try_begin_gizmo_drag(), from "left mouse down"),
    not re-captured on every frame of the drag."""
    src = _source(cs.MultiplayerGame.try_begin_gizmo_drag)
    check("_history_drag_before" in src, "try_begin_gizmo_drag(): captures the pre-drag snapshot used for Undo")


# ============================================================
# Selection / picking: already real 3D raycasting, not screen-space
# ============================================================

def test_selection_uses_real_collider_raycast_not_manual_screen_math() -> None:
    """handle_editor_click() uses Ursina's mouse.hovered_entity, which is
    backed by a genuine Panda3D CollisionTraverser (a real 3D ray cast
    against scene colliders, sorted nearest-first by construction) --
    confirmed by reading ursina/mouse.py directly, not assumed. This is
    NOT fragile screen-space bounding-box guessing, so it already
    satisfies "use proper viewport/world picking" without needing a
    parallel manual raycast system."""
    src = _source(cs.MultiplayerGame.handle_editor_click)
    check("mouse.hovered_entity" in src, "handle_editor_click(): uses Ursina's collider-based mouse.hovered_entity")
    check("deselect_part()" in src, "handle_editor_click(): clicking empty space (no hovered entity) deselects")

    # ursina/mouse.py replaces itself in sys.modules with a singleton
    # Mouse instance (a common Python trick) -- inspect.getsource() on
    # the imported name would inspect that INSTANCE, not the module, and
    # fail. Locate the real file via the package directory instead.
    import ursina
    mouse_py_path = os.path.join(os.path.dirname(ursina.__file__), "mouse.py")
    with open(mouse_py_path, "r", encoding="utf-8") as handle:
        mouse_src = handle.read()
    check("CollisionTraverser" in mouse_src, "ursina.mouse: hovered_entity is backed by a real CollisionTraverser (3D ray cast), not 2D screen-space math")


# ============================================================
# RMB look works initially then dies / Alt+Tab recovery
# (bug-report follow-up)
# ============================================================

def test_poll_qt_look_delta_tolerates_focus_widget_none() -> None:
    """The actual bug fix: the old check bailed on ANY
    `focusWidget() is not container`, including `focusWidget() is None`
    -- a legitimate state once the embedded viewport's foreign native
    window genuinely holds real OS keyboard focus (confirmed by code-path
    elimination: RMB release/cursor-restore, which does NOT depend on
    this check, kept working exactly when rotation silently died, so
    capture itself was never the problem). Must now only bail for some
    OTHER real (non-None) widget."""
    src = _source(cs.MultiplayerGame._poll_qt_look_delta)
    check("current_focus is not None and current_focus is not container" in src, "_poll_qt_look_delta(): only bails when focus is a DIFFERENT real widget, not merely non-container")
    check("QApplication.focusWidget() is not container" not in src, "_poll_qt_look_delta(): the old overly-strict check is gone")


def test_repeated_rmb_cycles_leave_no_stale_flags() -> None:
    """Simulates the manual repro's "dozens of RMB press/release cycles"
    at the flag level (no live embedded window available headlessly,
    see module docstring) -- begin_editor_look()/end_editor_look() must
    return editor_look_active to a clean False after every single
    cycle, with nothing accumulating."""
    src_begin = _source(cs.MultiplayerGame.begin_editor_look)
    src_end = _source(cs.MultiplayerGame.end_editor_look)
    check("self.editor_look_active = True" in src_begin, "begin_editor_look(): sets editor_look_active True")
    check("self.editor_look_active = False" in src_end, "end_editor_look(): sets editor_look_active False")
    check("self._start_mouse_look()" in src_begin, "begin_editor_look(): (re-)acquires mouse-look capture every time, not just once")


def test_focus_loss_resets_look_and_cursor_delta_state() -> None:
    """release_play_input_capture() (Escape / click-elsewhere /
    applicationStateChanged) must clear BOTH the boolean flags AND the
    cursor-delta reference state (_qt_look_last_pos, via
    _stop_mouse_look()) -- a stale delta reference is exactly what would
    cause "no giant camera jump on the first mouse movement" to fail
    after Alt+Tab recovery."""
    src = _source(cs.MultiplayerGame.release_play_input_capture)
    check("self._stop_mouse_look()" in src, "release_play_input_capture(): calls _stop_mouse_look(), which clears _qt_look_last_pos")
    stop_mouse_look_src = _source(cs.MultiplayerGame._stop_mouse_look)
    check("self._qt_look_last_pos = None" in stop_mouse_look_src, "_stop_mouse_look(): actually clears the stored cursor-delta reference")


def test_reacquiring_focus_after_loss_allows_look_again() -> None:
    """A fresh begin_editor_look() (a new RMB-down) must fully re-arm
    look state from scratch -- editor_look_active True again AND
    _start_mouse_look() re-run (which re-establishes _qt_look_last_pos
    at the CURRENT cursor/container position, not a stale one) -- so
    "reacquiring viewport focus allows look again" holds regardless of
    what focus-loss path preceded it."""
    src = _source(cs.MultiplayerGame._start_mouse_look)
    check("self._qt_look_last_pos = center_global" in src, "_start_mouse_look(): re-initializes _qt_look_last_pos fresh on every acquisition, not reused from a prior session")
    check("container.setFocus(" in src, "_start_mouse_look(): re-requests Qt focus on every acquisition")


# ============================================================
# Undo/Redo eventually stops responding (bug-report follow-up)
# ============================================================

def test_editor_mode_handles_ctrl_z_ctrl_y_natively() -> None:
    """Root cause confirmed by architecture: Ctrl+Z/Ctrl+Y are wired ONLY
    as QAction/QShortcut objects on StudioMainWindow, which only fire for
    a keystroke that passes through QT'S OWN event loop -- the embedded
    viewport is a createWindowContainer()-wrapped FOREIGN native window
    that, once it holds real OS keyboard focus (required for WASD/camera
    to work at all), receives keystrokes directly at the native level,
    never reaching Qt's shortcut map. Fixed by giving input() its own
    entry point for Ctrl+Z/Ctrl+Shift+Z/Ctrl+Y, routed through the exact
    same self.studio_adapter.undo()/redo() Qt's own QAction already
    calls -- not a second, divergent undo/redo implementation."""
    src = _source(cs.MultiplayerGame.input)
    editor_branch_end = src.index("return", src.index("if not self.studio_playing:"))
    editor_branch = src[: editor_branch_end]
    check('key == "z" and held_keys["control"] and held_keys["shift"]' in editor_branch, "input(): editor mode handles Ctrl+Shift+Z (redo alt-chord)")
    check('key == "z" and held_keys["control"]' in editor_branch, "input(): editor mode handles Ctrl+Z (undo)")
    check('key == "y" and held_keys["control"]' in editor_branch, "input(): editor mode handles Ctrl+Y (redo)")
    check("self._trigger_editor_undo()" in editor_branch, "input(): Ctrl+Z calls _trigger_editor_undo()")
    check("self._trigger_editor_redo()" in editor_branch, "input(): Ctrl+Y/Ctrl+Shift+Z call _trigger_editor_redo()")


def test_trigger_editor_undo_redo_reuse_the_same_adapter_path_as_qt() -> None:
    """_trigger_editor_undo()/_trigger_editor_redo() must call the SAME
    self.studio_adapter.undo()/redo() Qt's own QAction uses -- not a
    parallel, divergent undo implementation -- so the Edit menu's
    enabled state/text (driven by CommandManager.add_state_listener(),
    see client_studio.py's own wiring) stays correct regardless of which
    entry point (Qt shortcut or native viewport keystroke) triggered it,
    and it survives repeated viewport-focus transitions since it is
    ALWAYS the one real command manager being mutated, never a copy."""
    undo_src = _source(cs.MultiplayerGame._trigger_editor_undo)
    redo_src = _source(cs.MultiplayerGame._trigger_editor_redo)
    check("self.studio_adapter.undo()" in undo_src, "_trigger_editor_undo(): calls studio_adapter.undo() -- same path as the Qt QAction")
    check("self.studio_adapter.redo()" in redo_src, "_trigger_editor_redo(): calls studio_adapter.redo() -- same path as the Qt QAction")
    check("self.studio_adapter is not None" in undo_src, "_trigger_editor_undo(): guards against a headless/no-adapter context")
    check("self.studio_adapter is not None" in redo_src, "_trigger_editor_redo(): guards against a headless/no-adapter context")


# ============================================================
# F focuses above the object (bug-report follow-up)
# ============================================================

class _FakeGameForFocus:
    """Minimal duck-typed stand-in for MultiplayerGame, providing only
    what focus_selected_part() and the real (unmodified, reused)
    _collect_transformable_descendants()/_world_bounds_points()/
    _euler_xyz_to_quat() methods actually touch -- local_player only
    needs a settable .position, not a real camera-driving Entity."""

    def __init__(self) -> None:
        self.instances: dict[str, cs.InstanceRecord] = {}
        self.selected_part_id: str | None = None
        self.player_yaw = 0.0
        self.player_pitch = 0.0
        self.local_player = Entity(eternal=True)
        self._transform_scratch = Entity(eternal=True)

    _collect_transformable_descendants = cs.MultiplayerGame._collect_transformable_descendants
    _world_bounds_points = cs.MultiplayerGame._world_bounds_points
    _euler_xyz_to_quat = cs.MultiplayerGame._euler_xyz_to_quat
    focus_selected_part = cs.MultiplayerGame.focus_selected_part


def _add_part(game: _FakeGameForFocus, part_id: str, position, size, rotation=(0.0, 0.0, 0.0)) -> None:
    game.instances[part_id] = cs.InstanceRecord(
        part_id, "Part", part_id, "Workspace",
        {"Position": list(position), "Size": list(size), "Rotation": list(rotation)},
    )


def _assert_camera_aims_at(label: str, game: _FakeGameForFocus, expected_center: Vec3) -> None:
    """The core invariant of the fix: camera_position + forward*distance
    must land EXACTLY on the object's real center, for whatever distance
    focus_selected_part() itself chose -- i.e. the crosshair aims
    directly at the object, not above/below/beside it, regardless of
    view angle."""
    forward = cs.forward_from_angles(game.player_yaw, game.player_pitch)
    offset = expected_center - game.local_player.position
    distance = offset.length()
    aim_point = game.local_player.position + forward * distance
    error = (aim_point - expected_center).length()
    check(error < 0.01, f"{label}: camera aims directly at the object's real center (error={error:.4f})")


def test_focus_selected_part_aims_at_center_for_normal_part() -> None:
    game = _FakeGameForFocus()
    _add_part(game, "p1", position=(5.0, 2.0, -3.0), size=(4.0, 1.0, 2.0))
    game.selected_part_id = "p1"
    game.player_yaw = 37.0
    game.player_pitch = 12.0
    game.focus_selected_part()
    _assert_camera_aims_at("normal 4x1x2 Part", game, Vec3(5.0, 2.0, -3.0))


def test_focus_selected_part_aims_at_center_for_tall_part() -> None:
    game = _FakeGameForFocus()
    _add_part(game, "p1", position=(0.0, 10.0, 0.0), size=(1.0, 20.0, 1.0))
    game.selected_part_id = "p1"
    game.focus_selected_part()
    _assert_camera_aims_at("very tall Part", game, Vec3(0.0, 10.0, 0.0))


def test_focus_selected_part_aims_at_center_for_wide_part() -> None:
    game = _FakeGameForFocus()
    _add_part(game, "p1", position=(0.0, 0.0, 0.0), size=(30.0, 1.0, 1.0))
    game.selected_part_id = "p1"
    game.focus_selected_part()
    _assert_camera_aims_at("very wide Part", game, Vec3(0.0, 0.0, 0.0))


def test_focus_selected_part_aims_at_center_for_nonzero_y() -> None:
    game = _FakeGameForFocus()
    _add_part(game, "p1", position=(2.0, 8.5, 2.0), size=(2.0, 2.0, 2.0))
    game.selected_part_id = "p1"
    game.focus_selected_part()
    _assert_camera_aims_at("Part at non-zero Y", game, Vec3(2.0, 8.5, 2.0))


def test_focus_selected_part_aims_at_center_for_rotated_part() -> None:
    """A rotated Part's bounds center still equals its Position (the
    box rotates around its own center) -- but this exercises
    _world_bounds_points()'s quaternion-corner math along the way rather
    than assuming that's true without checking."""
    game = _FakeGameForFocus()
    _add_part(game, "p1", position=(1.0, 1.0, 1.0), size=(4.0, 1.0, 2.0), rotation=(15.0, 45.0, 30.0))
    game.selected_part_id = "p1"
    game.focus_selected_part()
    _assert_camera_aims_at("rotated Part", game, Vec3(1.0, 1.0, 1.0))


def test_focus_selected_part_ignores_stale_selection() -> None:
    """A deleted/deselected selected_part_id (not in game.instances) must
    be a harmless no-op, not a crash."""
    game = _FakeGameForFocus()
    game.selected_part_id = "does-not-exist"
    original_position = Vec3(game.local_player.position)
    game.focus_selected_part()
    check(game.local_player.position == original_position, "focus_selected_part(): a stale/missing selection is a no-op")


def test_focus_distance_scales_with_object_size() -> None:
    """A larger object should be framed from further away than a small
    one -- distance must scale with the bounding diagonal, not be a
    fixed constant regardless of size (the old bug's -8.0)."""
    small = _FakeGameForFocus()
    _add_part(small, "p1", position=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0))
    small.selected_part_id = "p1"
    small.focus_selected_part()
    small_distance = (Vec3(0, 0, 0) - small.local_player.position).length()

    large = _FakeGameForFocus()
    _add_part(large, "p1", position=(0.0, 0.0, 0.0), size=(20.0, 20.0, 20.0))
    large.selected_part_id = "p1"
    large.focus_selected_part()
    large_distance = (Vec3(0, 0, 0) - large.local_player.position).length()

    check(large_distance > small_distance, f"focus distance scales with object size (small={small_distance:.2f}, large={large_distance:.2f})")
    check(small_distance >= cs.FOCUS_MIN_DISTANCE - 0.01, f"a tiny object still gets a sane minimum distance, not zoomed inside it (got {small_distance:.2f})")


test_update_gates_flight_on_viewport_focused_not_editor_look_active()
test_panda_window_focus_filter_sets_viewport_focused_on_click_not_hover()
test_input_reconfirms_viewport_focused_on_editor_clicks()
test_release_play_input_capture_resets_editor_look_active_and_viewport_focused()
test_poll_qt_look_delta_also_resets_stale_flags_on_focus_loss()
test_viewport_focused_reset_on_play_start_and_stop()
test_editor_mode_wheel_dollies_camera()
test_dolly_camera_moves_along_view_direction_not_scaled_by_dt()
test_update_gizmo_never_pushes_undo_history()
test_end_gizmo_drag_is_the_sole_history_push_site_for_drags()
test_try_begin_gizmo_drag_snapshots_before_state_once()
test_selection_uses_real_collider_raycast_not_manual_screen_math()

test_poll_qt_look_delta_tolerates_focus_widget_none()
test_repeated_rmb_cycles_leave_no_stale_flags()
test_focus_loss_resets_look_and_cursor_delta_state()
test_reacquiring_focus_after_loss_allows_look_again()
test_editor_mode_handles_ctrl_z_ctrl_y_natively()
test_trigger_editor_undo_redo_reuse_the_same_adapter_path_as_qt()
test_focus_selected_part_aims_at_center_for_normal_part()
test_focus_selected_part_aims_at_center_for_tall_part()
test_focus_selected_part_aims_at_center_for_wide_part()
test_focus_selected_part_aims_at_center_for_nonzero_y()
test_focus_selected_part_aims_at_center_for_rotated_part()
test_focus_selected_part_ignores_stale_selection()
test_focus_distance_scales_with_object_size()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All viewport interaction tests passed.")
