"""Regression tests for Stage 3.3's Play Mode character controller
foundation (character_controller.py) and its integration points in
client_studio.py.

Three tiers, matching what's actually testable without a live Ursina/
Panda3D graphics window (see this project's existing test suite -- none of
it ever constructs a real MultiplayerGame/Ursina app; that requires a
genuine window, which none of these tests open):

1. Pure math (CharacterInputState, CharacterCamera, compute_target_velocity,
   accelerate_toward, select_spawn_point, spawn positioning) -- no engine
   import at all beyond what character_controller.py itself needs.

2. Real headless Bullet physics (CharacterController) -- panda3d.bullet
   works standalone with no window/app required (confirmed empirically
   during Stage 3.3 development: BulletWorld/BulletCharacterControllerNode/
   BulletCapsuleShape/BulletRigidBodyNode all construct and step correctly
   with zero Panda3D window or Ursina app present). This tier is what
   proves the actual physical behavior: falling, landing, grounded
   detection, jump gating, wall collision, CanCollide=false pass-through,
   and no leaked Bullet character nodes across repeated create/destroy.

3. Structural checks on client_studio.py's integration (via
   inspect.getsource(), same technique as test_legacy_content_removal.py)
   for the invariants that genuinely require a live MultiplayerGame/Ursina
   app to observe behaviorally: Play/Stop lifecycle wiring, and that
   runtime character movement never dirties the Place, never touches
   Undo/Redo history, and never invokes the legacy PlayerVisual/player.glb
   path. The corresponding BEHAVIORAL claims (one controller spawns on
   Play, no duplicate/leaked controller across repeated Play/Stop, no
   runtime character survives Save Place) are verified separately via the
   real Windows GUI test pass documented in the Stage 3.3 report -- keep
   that report's list in sync with what is/isn't covered here.

Follows this project's existing test convention: plain top-level-assertion
script, run directly, offscreen Qt platform.
"""
import inspect
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from panda3d.bullet import BulletRigidBodyNode, BulletBoxShape, BulletWorld
from panda3d.core import NodePath, Vec3 as PVec3

import character_controller as cc
import client_studio as cs

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def make_world() -> BulletWorld:
    world = BulletWorld()
    world.setGravity((0, -24.0, 0))
    return world


def add_static_floor(world: BulletWorld, y: float = -0.5, half_extents=(50.0, 0.5, 50.0)) -> NodePath:
    node = BulletRigidBodyNode("floor")
    node.addShape(BulletBoxShape(PVec3(*half_extents)))
    node.setMass(0.0)
    node.setStatic(True)
    np = NodePath(node)
    np.setPos(0, y, 0)
    world.attachRigidBody(node)
    return np


def add_static_wall(world: BulletWorld, z: float, half_extents=(5.0, 3.0, 0.5)) -> NodePath:
    node = BulletRigidBodyNode("wall")
    node.addShape(BulletBoxShape(PVec3(*half_extents)))
    node.setMass(0.0)
    node.setStatic(True)
    np = NodePath(node)
    np.setPos(0, 2, z)
    world.attachRigidBody(node)
    return np


# ============================================================
# TIER 1: pure math
# ============================================================

def test_movement_axes() -> None:
    state = cc.CharacterInputState()
    check(state.movement_axes() == (0.0, 0.0), "no keys held -> zero axes")
    state.forward = True
    check(state.movement_axes() == (0.0, 1.0), "W held -> forward axis 1")
    state.right = True
    check(state.movement_axes() == (1.0, 1.0), "W+D held -> both axes 1 (not yet normalized)")


def test_diagonal_movement_not_faster_than_straight() -> None:
    state = cc.CharacterInputState()
    state.forward = True
    straight = cc.compute_target_velocity(state, 0.0, 6.0)
    straight_speed = cc._length3(straight)

    state.right = True
    diagonal = cc.compute_target_velocity(state, 0.0, 6.0)
    diagonal_speed = cc._length3(diagonal)

    check(abs(straight_speed - 6.0) < 1e-6, f"straight-line speed equals walk_speed, got {straight_speed}")
    check(abs(diagonal_speed - 6.0) < 1e-6, f"diagonal speed also equals walk_speed (normalized), got {diagonal_speed}")
    check(abs(straight_speed - diagonal_speed) < 1e-6, "diagonal movement is not faster than straight movement")


def test_compute_target_velocity_zero_input() -> None:
    state = cc.CharacterInputState()
    velocity = cc.compute_target_velocity(state, 45.0, 6.0)
    check(velocity == (0.0, 0.0, 0.0), "no input -> zero target velocity regardless of yaw")


def test_accelerate_toward_reaches_target_without_overshoot() -> None:
    result = cc.accelerate_toward((0.0, 0.0, 0.0), (6.0, 0.0, 0.0), accel=40.0, dt=1.0 / 60.0)
    check(cc._length3(result) <= 6.0 + 1e-6, "one accel step never overshoots the target speed")
    check(result[0] > 0.0, "accelerates toward positive target")

    # Large dt should snap exactly to target (never overshoot past it).
    snapped = cc.accelerate_toward((0.0, 0.0, 0.0), (6.0, 0.0, 0.0), accel=40.0, dt=10.0)
    check(snapped == (6.0, 0.0, 0.0), "a large dt clamps exactly to target, no overshoot")

    # Zero/negative dt is a no-op.
    unchanged = cc.accelerate_toward((1.0, 2.0, 3.0), (6.0, 0.0, 0.0), accel=40.0, dt=0.0)
    check(unchanged == (1.0, 2.0, 3.0), "dt<=0 leaves velocity unchanged")


def test_select_spawn_point_deterministic() -> None:
    candidates = [
        {"id": "zzz", "position": (1.0, 0.0, 1.0)},
        {"id": "aaa", "position": (2.0, 0.0, 2.0)},
        {"id": "mmm", "position": (3.0, 0.0, 3.0)},
    ]
    chosen = cc.select_spawn_point(candidates)
    check(chosen is not None and chosen["id"] == "aaa", f"lowest-id SpawnPoint chosen deterministically, got {chosen}")

    # Re-running with the same (even reordered) input must choose the same one.
    reordered = list(reversed(candidates))
    chosen_again = cc.select_spawn_point(reordered)
    check(chosen_again is not None and chosen_again["id"] == "aaa", "selection is stable regardless of input order")


def test_select_spawn_point_empty() -> None:
    check(cc.select_spawn_point([]) is None, "no SpawnPoint candidates -> None (caller must fall back)")


def test_spawn_position_from_point_clears_the_pad() -> None:
    spawn_point = {"id": "sp1", "position": (5.0, 0.0, -3.0), "size": (6.0, 0.4, 6.0)}
    position = cc.spawn_position_from_point(spawn_point, capsule_half_height=0.9)
    expected_y = 0.0 + 0.2 + 0.9 + cc.SPAWN_CLEARANCE
    check(abs(position[1] - expected_y) < 1e-6, f"spawn Y clears the pad's top surface + capsule half-height + clearance, got {position[1]}")
    check(position[0] == 5.0 and position[2] == -3.0, "spawn X/Z match the SpawnPoint's own position")


def test_fallback_spawn_position_documented() -> None:
    position = cc.fallback_spawn_position(capsule_half_height=0.9)
    check(position[0] == cc.FALLBACK_SPAWN_POSITION[0], "fallback X matches the documented constant")
    check(position[2] == cc.FALLBACK_SPAWN_POSITION[2], "fallback Z matches the documented constant")
    check(position[1] == cc.FALLBACK_SPAWN_POSITION[1] + 0.9, "fallback Y is lifted by the capsule half-height")


def test_character_camera_yaw_wraps() -> None:
    camera = cc.CharacterCamera(yaw_degrees=179.0, sensitivity_x=1.0)
    camera.apply_delta(5.0, 0.0)
    check(-181.0 <= camera.yaw_degrees <= 181.0, f"yaw stays wrapped to (-180, 180], got {camera.yaw_degrees}")
    check(camera.yaw_degrees < 0, f"179 + 5 wraps past 180 to a negative value, got {camera.yaw_degrees}")


def test_character_camera_pitch_clamps() -> None:
    camera = cc.CharacterCamera(pitch_degrees=0.0, sensitivity_y=1.0, min_pitch=-80.0, max_pitch=80.0)
    camera.apply_delta(0.0, -1000.0)  # large upward look
    check(camera.pitch_degrees == 80.0, f"pitch clamps to max_pitch, got {camera.pitch_degrees}")
    camera.apply_delta(0.0, 2000.0)  # large downward look
    check(camera.pitch_degrees == -80.0, f"pitch clamps to min_pitch, got {camera.pitch_degrees}")


# ============================================================
# TIER 2: real headless Bullet physics
# ============================================================

def test_capsule_dimensions_configurable() -> None:
    world = make_world()
    controller = cc.CharacterController(world, radius=0.5, height=2.0, step_height=0.3, walk_speed=7.0)
    check(controller.radius == 0.5, "radius is configurable")
    check(controller.height == 2.0, "height is configurable")
    check(controller.step_height == 0.3, "step_height is configurable")
    check(controller.walk_speed == 7.0, "walk_speed is configurable")
    check(controller.half_height == 1.0, "half_height derives from height")
    controller.destroy()


def test_default_dimensions() -> None:
    world = make_world()
    controller = cc.CharacterController(world)
    check(controller.radius == cc.DEFAULT_RADIUS, "default radius applied")
    check(controller.height == cc.DEFAULT_HEIGHT, "default height applied")
    controller.destroy()


def test_falls_and_lands_grounded() -> None:
    world = make_world()
    add_static_floor(world)
    controller = cc.CharacterController(world)
    controller.set_position((0.0, 5.0, 0.0))
    inputs = cc.CharacterInputState()

    check(not controller.is_grounded(), "not grounded before any physics step (fresh spawn high in the air)")
    for _ in range(150):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    position = controller.get_position()
    check(abs(position[1] - controller.half_height) < 0.15, f"capsule settles near half_height above the floor, got y={position[1]}")
    check(controller.is_grounded(), "grounded is True after landing on a static floor")
    controller.destroy()


def test_jump_allowed_while_grounded() -> None:
    world = make_world()
    add_static_floor(world)
    controller = cc.CharacterController(world, jump_speed=8.0)
    controller.set_position((0.0, 0.9, 0.0))
    inputs = cc.CharacterInputState()
    for _ in range(30):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)
    check(controller.is_grounded(), "sanity: settled and grounded before testing jump")

    jumped = controller.try_jump()
    check(jumped, "jump succeeds while grounded")
    check(controller._velocity_y == 8.0, f"jump sets vertical velocity to jump_speed, got {controller._velocity_y}")
    controller.destroy()


def test_no_repeated_air_jump() -> None:
    world = make_world()
    add_static_floor(world)
    controller = cc.CharacterController(world, jump_speed=8.0)
    controller.set_position((0.0, 0.9, 0.0))
    inputs = cc.CharacterInputState()
    for _ in range(30):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    controller.try_jump()
    # Step a few frames while airborne (moving away from the floor).
    for _ in range(6):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)
    check(not controller.is_grounded(), "sanity: airborne shortly after a jump")
    second_jump = controller.try_jump()
    check(not second_jump, "a second jump attempt while airborne fails (no double jump)")
    controller.destroy()


def test_grounded_jump_gate_via_input_state() -> None:
    """Same as above but driven through apply_movement()'s own jump_held
    handling (the actual path client_studio.py's update_character() uses),
    not try_jump() called directly."""
    world = make_world()
    add_static_floor(world)
    controller = cc.CharacterController(world, jump_speed=8.0)
    controller.set_position((0.0, 0.9, 0.0))
    inputs = cc.CharacterInputState()
    for _ in range(30):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    inputs.jump_held = True
    controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
    world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)
    check(controller._velocity_y > 0.0, "jump_held while grounded imparts upward velocity via apply_movement()")

    # Keep jump_held true while airborne -- must not keep re-triggering.
    velocity_after_first_jump = controller._velocity_y
    for _ in range(3):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)
    check(controller._velocity_y < velocity_after_first_jump, "holding jump while airborne does not re-trigger the jump speed (gravity keeps reducing velocity_y instead)")
    controller.destroy()


def test_wall_blocks_movement() -> None:
    world = make_world()
    add_static_floor(world)
    add_static_wall(world, z=5.0)
    controller = cc.CharacterController(world, walk_speed=6.0)
    controller.set_position((0.0, 0.9, 0.0))
    inputs = cc.CharacterInputState()
    for _ in range(30):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    inputs.forward = True  # yaw=0 -> +Z is forward, walking straight at the wall
    for _ in range(240):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    position = controller.get_position()
    check(position[2] < 4.5, f"a CanCollide=true wall stops the capsule before it, got z={position[2]}")
    controller.destroy()


def test_no_wall_allows_free_passage() -> None:
    """Models a CanCollide=false Part: physics.PhysicsWorld.add_part()
    never attaches such a Part to the Bullet world at all (see physics.py's
    "ghost" body handling), so there is structurally nothing for the
    capsule to collide with -- this test confirms the capsule really does
    pass straight through empty space where a CanCollide=true wall would
    have stopped it (see test_wall_blocks_movement above, same walk
    distance/duration, no wall registered this time)."""
    world = make_world()
    add_static_floor(world)
    controller = cc.CharacterController(world, walk_speed=6.0)
    controller.set_position((0.0, 0.9, 0.0))
    inputs = cc.CharacterInputState()
    for _ in range(30):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    inputs.forward = True
    for _ in range(240):
        controller.apply_movement(inputs, 0.0, 1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)

    position = controller.get_position()
    check(position[2] > 5.0, f"with no CanCollide=true geometry in the way, the capsule passes z=5 freely, got z={position[2]}")
    controller.destroy()


def test_destroy_is_idempotent_and_removes_from_world() -> None:
    world = make_world()
    check(world.getNumCharacters() == 0, "sanity: world starts with zero characters")
    controller = cc.CharacterController(world)
    check(world.getNumCharacters() == 1, "attaching the controller registers exactly one character")
    controller.destroy()
    check(world.getNumCharacters() == 0, "destroy() removes the character from the Bullet world")
    controller.destroy()  # must not raise, must not go negative
    check(world.getNumCharacters() == 0, "calling destroy() twice is a safe no-op (idempotent)")


def test_repeated_create_destroy_does_not_leak() -> None:
    world = make_world()
    for i in range(8):
        controller = cc.CharacterController(world)
        controller.set_position((0.0, 5.0, 0.0))
        check(world.getNumCharacters() == 1, f"iteration {i}: exactly one character while alive")
        controller.destroy()
        check(world.getNumCharacters() == 0, f"iteration {i}: zero characters after destroy (no leak)")


def test_character_runtime_wraps_controller_lifecycle() -> None:
    world = make_world()
    add_static_floor(world)
    runtime = cc.CharacterRuntime(world, spawn_position=(1.0, 5.0, 2.0), initial_yaw_degrees=45.0)
    check(world.getNumCharacters() == 1, "CharacterRuntime construction creates exactly one Bullet character")
    check(runtime.camera.yaw_degrees == 45.0, "CharacterRuntime seeds the camera's initial yaw")
    check(runtime.debug_visual is None, "no debug visual by default (Stage 3.3: invisible unless explicitly toggled)")

    position_before = runtime.controller.get_position()
    check(abs(position_before[0] - 1.0) < 1e-3 and abs(position_before[2] - 2.0) < 1e-3, "spawn_position is applied to the controller")

    for _ in range(5):
        runtime.step(1.0 / 60.0)
        world.doPhysics(1.0 / 60.0, 10, 1.0 / 120.0)
    synced = runtime.synced_position()
    check(isinstance(synced, tuple) and len(synced) == 3, "synced_position() returns a 3-tuple")

    runtime.destroy()
    check(world.getNumCharacters() == 0, "CharacterRuntime.destroy() removes the underlying Bullet character")
    runtime.destroy()  # idempotent
    check(world.getNumCharacters() == 0, "CharacterRuntime.destroy() is idempotent")


# ============================================================
# TIER 3: structural checks on client_studio.py's integration
# ============================================================

def test_play_lifecycle_wires_character_start_stop_in_order() -> None:
    source = inspect.getsource(cs.MultiplayerGame.set_studio_playing)
    start_physics_idx = source.find("self._start_physics()")
    start_character_idx = source.find("self._start_character()")
    start_lua_idx = source.find("self._start_lua()")
    check(start_physics_idx != -1 and start_character_idx != -1 and start_lua_idx != -1, "set_studio_playing() calls all three of _start_physics/_start_character/_start_lua")
    check(start_physics_idx < start_character_idx < start_lua_idx, "start order is physics -> character -> lua")

    stop_lua_idx = source.find("self._stop_lua()")
    stop_character_idx = source.find("self._stop_character()")
    stop_physics_idx = source.find("self._stop_physics()")
    check(stop_lua_idx != -1 and stop_character_idx != -1 and stop_physics_idx != -1, "set_studio_playing() calls all three of _stop_lua/_stop_character/_stop_physics")
    check(stop_lua_idx < stop_character_idx < stop_physics_idx, "stop order is lua -> character -> physics (character removed before its Bullet world is torn down)")


def test_runtime_character_never_dirties_place_or_touches_history() -> None:
    for method_name in ("update_character", "sync_character_camera", "_start_character", "_stop_character"):
        source = inspect.getsource(getattr(cs.MultiplayerGame, method_name))
        check("mark_place_dirty" not in source, f"{method_name}() never calls mark_place_dirty()")
        check(".history." not in source, f"{method_name}() never touches self.history (no Undo/Redo commands from runtime movement)")


def test_character_path_never_touches_legacy_playervisual() -> None:
    for method_name in ("_start_character", "_stop_character", "update_character", "sync_character_camera"):
        source = inspect.getsource(getattr(cs.MultiplayerGame, method_name))
        check("PlayerVisual" not in source, f"{method_name}() never references PlayerVisual")
        check("player.glb" not in source, f"{method_name}() never references player.glb")
        check("load_fresh_model_node" not in source, f"{method_name}() never calls load_fresh_model_node()")
    # character_controller.py's own module docstring documents the ABSENCE
    # of PlayerVisual/player.glb by name (explaining what this module
    # deliberately does not do) -- checking the whole module source for
    # those substrings would flag that explanatory prose as a false
    # positive. What actually matters is that none of the real code
    # (classes/functions, not docstrings) constructs a PlayerVisual or
    # loads player.glb -- checked precisely via each class/function's own
    # source below instead of the whole module text.
    def _defined_here(o: object) -> bool:
        return (inspect.isclass(o) or inspect.isfunction(o)) and getattr(o, "__module__", None) == cc.__name__

    for name, obj in inspect.getmembers(cc, predicate=_defined_here):
        source = inspect.getsource(obj)
        check("PlayerVisual(" not in source, f"character_controller.{name} never constructs a PlayerVisual")
        check("loadModel" not in source and "load_fresh_model_node" not in source, f"character_controller.{name} never loads a model file")


def test_character_not_included_in_place_save_path() -> None:
    """export_world()/sync_full_scene() are the two paths that decide what
    a Place file / Explorer ever sees -- neither reads self._character_
    runtime, so a runtime character structurally cannot end up serialized
    or listed as a persistent object."""
    adapter_export_source = inspect.getsource(cs.MultiplayerStudioAdapter.export_world)
    check("_character_runtime" not in adapter_export_source, "export_world() (used by Save Place) never reads _character_runtime")
    system_objects_source = inspect.getsource(cs.MultiplayerStudioAdapter._system_objects)
    check("_character_runtime" not in system_objects_source, "_system_objects() (Explorer's synthetic entries) never references _character_runtime")


def test_pointer_capture_fix_checks_qt_focus() -> None:
    source = inspect.getsource(cs.MultiplayerGame._poll_qt_look_delta)
    check("focusWidget" in source, "_poll_qt_look_delta() checks QApplication.focusWidget() every frame (the actual pointer-capture bug fix)")
    check("_stop_mouse_look" in source, "_poll_qt_look_delta() can self-release capture when focus has left the viewport")


def test_escape_releases_rather_than_toggles_in_play() -> None:
    source = inspect.getsource(cs.MultiplayerGame.input)
    check("release_play_input_capture" in source, "Play-mode Escape handling calls release_play_input_capture()")


if __name__ == "__main__":
    test_movement_axes()
    test_diagonal_movement_not_faster_than_straight()
    test_compute_target_velocity_zero_input()
    test_accelerate_toward_reaches_target_without_overshoot()
    test_select_spawn_point_deterministic()
    test_select_spawn_point_empty()
    test_spawn_position_from_point_clears_the_pad()
    test_fallback_spawn_position_documented()
    test_character_camera_yaw_wraps()
    test_character_camera_pitch_clamps()

    test_capsule_dimensions_configurable()
    test_default_dimensions()
    test_falls_and_lands_grounded()
    test_jump_allowed_while_grounded()
    test_no_repeated_air_jump()
    test_grounded_jump_gate_via_input_state()
    test_wall_blocks_movement()
    test_no_wall_allows_free_passage()
    test_destroy_is_idempotent_and_removes_from_world()
    test_repeated_create_destroy_does_not_leak()
    test_character_runtime_wraps_controller_lifecycle()

    test_play_lifecycle_wires_character_start_stop_in_order()
    test_runtime_character_never_dirties_place_or_touches_history()
    test_character_path_never_touches_legacy_playervisual()
    test_character_not_included_in_place_save_path()
    test_pointer_capture_fix_checks_qt_focus()
    test_escape_releases_rather_than_toggles_in_play()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for message in FAILURES:
            print(f"  - {message}")
        sys.exit(1)
    print("All character controller tests passed.")
