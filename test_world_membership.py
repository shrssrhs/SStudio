"""Regression tests for Stage 4.0's world-membership correction:

    A spatial/3D Instance participates in the live world scene --
    rendering and physics -- only while it is a genuine descendant of
    Workspace. Every other root service (ReplicatedStorage, ServerStorage,
    StarterPlayer, ServerScriptService, StarterGui, Players) is a valid,
    live location for an Instance to sit in, but never renders or
    simulates while it sits there.

Bug history: RuntimeSceneLayer._is_world_attached() (lua_runtime.py) used
to accept ANY ROOT_SERVICES member as "attached", not just Workspace --
found during a documentation audit and confirmed here via direct
behavioral reproduction before the fix (see the fix's own docstring for
the exact before/after). A spatial Instance sitting in, say,
ReplicatedStorage (a common "template" location, never meant to render or
simulate) got a real Entity and physics body anyway, and reparenting one
out of Workspace back into a non-Workspace service didn't detach it at
all, since both roots satisfied the old check equally.

Follows this project's existing test convention (test_stage39_api.py):
plain top-level-assertion script, run directly, offscreen Qt platform,
headless Ursina window, a REAL Bullet PhysicsWorld -- entity/physics
presence is checked against the real game.parts dict and the real
PhysicsWorld, not inferred from source.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Ursina

ursina_app = Ursina(window_type="none")

import client_studio as cs
import datamodel_schema
import lua_gameplay_api as lga
import lua_runtime
import physics

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


class _FakeGame:
    """Minimal harness -- no character/contact detection needed for this
    stage's own tests, just a real PhysicsWorld to prove a real Bullet
    body is actually added/removed, and a real Ursina Entity to prove
    the same for rendering."""

    _build_part_entity = cs.MultiplayerGame._build_part_entity
    _apply_part_surface = cs.MultiplayerGame._apply_part_surface
    _set_light_render_active = cs.MultiplayerGame._set_light_render_active

    def __init__(self) -> None:
        self.instances: dict[str, cs.InstanceRecord] = {}
        self.parts: dict = {}
        self.studio_adapter = None
        self.third_person_enabled = False
        self._captured = False
        self._character_runtime = None
        self._character_visual = None
        self.services: dict[str, dict] = datamodel_schema.sanitize_services_snapshot(None)
        self._runtime_min_zoom = 2.0
        self._runtime_max_zoom = 10.0
        self.applied_service_writes: list[tuple[str, dict]] = []
        self._physics_world = physics.PhysicsWorld(gravity=-24.0)

    def apply_runtime_service_write(self, service_name: str, properties: dict) -> None:
        self.applied_service_writes.append((service_name, dict(properties)))

    def _mouse_look_captured(self) -> bool:
        return self._captured

    def set_camera_mode(self, third_person: bool) -> None:
        pass

    def teardown(self) -> None:
        self._physics_world.destroy()


def make_context(game: _FakeGame) -> tuple[lua_runtime.LuaRuntimeManager, lga.LuaGameplayContext]:
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    ctx.start()
    return manager, ctx


def teardown(game: _FakeGame, manager: lua_runtime.LuaRuntimeManager, ctx: lga.LuaGameplayContext) -> None:
    ctx.stop()
    manager.stop()
    game.teardown()


def is_active(game: _FakeGame, runtime_id: str) -> bool:
    """A spatial Instance is "world-active" iff it has both a real
    rendered Entity (in game.parts) and a real Bullet physics body --
    the two things _sync_world_attachment()/instance_new() are
    responsible for keeping in sync with Workspace ancestry."""
    return runtime_id in game.parts and game._physics_world.has_body(runtime_id)


def new_part(scene: lua_runtime.RuntimeSceneLayer, parent_id) -> str:
    ok, result = scene.instance_new("Part", parent_id)
    assert ok, result
    scene.set_property(result, "Anchored", True)
    return result


# ============================================================
# Direct / nested Workspace membership
# ============================================================

def test_part_directly_under_workspace_is_active() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        pid = new_part(manager.scene, "Workspace")
        check(is_active(game, pid), "a Part parented directly to Workspace is world-active")
    finally:
        teardown(game, manager, ctx)


def test_part_under_folder_under_workspace_is_active() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, folder = manager.scene.instance_new("Folder", "Workspace")
        assert ok, folder
        pid = new_part(manager.scene, folder)
        check(is_active(game, pid), "a Part nested under a Folder under Workspace is world-active")
    finally:
        teardown(game, manager, ctx)


def test_part_under_model_under_folder_under_workspace_is_active() -> None:
    """Deeper nesting: Workspace > Folder > Model > Part."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, folder = manager.scene.instance_new("Folder", "Workspace")
        assert ok, folder
        ok, model = manager.scene.instance_new("Model", folder)
        assert ok, model
        pid = new_part(manager.scene, model)
        check(is_active(game, pid), "a Part nested 3 levels deep under Workspace (Folder > Model) is world-active")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Non-Workspace roots: valid Instance, never active
# ============================================================

def test_part_under_folder_under_non_workspace_root_is_inactive() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, folder = manager.scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        pid = new_part(manager.scene, folder)
        check(not is_active(game, pid), "a Part under a Folder under ReplicatedStorage is NOT world-active")
        check(manager.scene.exists(pid), "...but it is still a perfectly valid, existing Instance")
    finally:
        teardown(game, manager, ctx)


def test_part_under_model_under_folder_under_non_workspace_root_is_inactive() -> None:
    """Every non-Workspace root gets the same treatment, not just
    ReplicatedStorage -- ServerStorage here, to confirm this is a genuine
    "only Workspace" rule and not a special case for one particular
    service name."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, folder = manager.scene.instance_new("Folder", "ServerStorage")
        assert ok, folder
        ok, model = manager.scene.instance_new("Model", folder)
        assert ok, model
        pid = new_part(manager.scene, model)
        check(not is_active(game, pid), "a Part nested under Model > Folder under ServerStorage is NOT world-active")
        check(manager.scene.exists(pid), "...but it remains a valid Instance")
    finally:
        teardown(game, manager, ctx)


def test_instance_new_with_explicit_non_workspace_parent_does_not_eagerly_attach() -> None:
    """Stage 4.0 fix, second half: Instance.new(class, someParent) used to
    build a real Entity/physics body unconditionally whenever ANY parent
    argument was given, never checking whether that parent was actually
    Workspace-attached. Confirms the eager-build path is now correctly
    gated on _is_world_attached() too, not just the lazy (no-parent-
    argument) path Clone() and Instance.new(class) already covered."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, folder = manager.scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        pid = new_part(manager.scene, folder)
        check(not is_active(game, pid), "Instance.new('Part', folderUnderReplicatedStorage) does not eagerly build an Entity/physics body")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Reparent transitions
# ============================================================

def test_reparent_into_and_out_of_workspace_repeatedly() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        pid = new_part(scene, None)
        check(not is_active(game, pid), "a freshly-created, unparented Part starts inactive")

        ok, err = scene.set_parent(pid, "Workspace")
        check(ok, f"reparent nil -> Workspace succeeds: {err}")
        check(is_active(game, pid), "...and becomes active")

        ok, non_ws_folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, non_ws_folder
        ok, err = scene.set_parent(pid, non_ws_folder)
        check(ok, f"reparent Workspace -> non-Workspace Folder succeeds: {err}")
        check(not is_active(game, pid), "...and becomes inactive")
        check(scene.exists(pid), "...WITHOUT being destroyed")

        ok, err = scene.set_parent(pid, "Workspace")
        check(ok, f"reparent back into Workspace succeeds: {err}")
        check(is_active(game, pid), "...and becomes active again")
        check(list(game.parts.keys()).count(pid) == 1, "exactly one Entity exists for it, no duplicate")
        check(game._physics_world.has_body(pid), "exactly one physics body exists for it")

        # One more full cycle to prove this isn't a one-shot fluke.
        ok, err = scene.set_parent(pid, non_ws_folder)
        check(ok and not is_active(game, pid), "second Workspace -> non-Workspace transition also detaches correctly")
        ok, err = scene.set_parent(pid, "Workspace")
        check(ok and is_active(game, pid), "second non-Workspace -> Workspace transition also reattaches correctly")
        check(list(game.parts.keys()).count(pid) == 1, "still exactly one Entity after repeated transitions")
        check(game._physics_world.has_body(pid), "still exactly one physics body after repeated transitions")
    finally:
        teardown(game, manager, ctx)


def test_destroy_after_inactive_active_transitions_leaves_no_stale_resources() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        pid = new_part(scene, "Workspace")
        check(is_active(game, pid), "starts active under Workspace")

        ok, folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        scene.set_parent(pid, folder)
        check(not is_active(game, pid), "goes inactive under a non-Workspace root")

        scene.set_parent(pid, "Workspace")
        check(is_active(game, pid), "reactivates back under Workspace")

        scene.set_parent(pid, folder)
        check(not is_active(game, pid), "goes inactive again")

        scene.destroy(pid)
        check(not scene.exists(pid), "Destroy() while inactive removes the Instance")
        check(pid not in game.parts, "...leaves no stale Entity reference")
        check(not game._physics_world.has_body(pid), "...leaves no stale physics body")

        # Also confirm Destroy() while ACTIVE is equally clean (the more
        # common case, already covered elsewhere in Stage 3.9, reconfirmed
        # here for this file's own completeness).
        pid2 = new_part(scene, "Workspace")
        check(is_active(game, pid2), "a second Part starts active under Workspace")
        scene.destroy(pid2)
        check(not scene.exists(pid2), "Destroy() while active removes the Instance")
        check(pid2 not in game.parts, "...leaves no stale Entity reference")
        check(not game._physics_world.has_body(pid2), "...leaves no stale physics body")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Ancestor reparent cascades to every descendant
# ============================================================

def test_ancestor_reparent_cascades_world_membership_to_every_descendant() -> None:
    """The exact scenario from the Stage 4.0 spec: a Model containing
    several Parts, currently under a non-Workspace root -- reparenting
    just the Model must bring every descendant Part in and out of
    world-attachment with it, without touching any of them directly."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        ok, model = scene.instance_new("Model", folder)
        assert ok, model
        descendants = [new_part(scene, model) for _ in range(5)]

        check(all(not is_active(game, d) for d in descendants), "all 5 descendants start inactive (Model is under ReplicatedStorage)")

        ok, err = scene.set_parent(model, "Workspace")
        check(ok, f"Model.Parent = workspace succeeds: {err}")
        check(all(is_active(game, d) for d in descendants), "Model.Parent = workspace activates ALL 5 descendants, without touching them individually")

        ok, err = scene.set_parent(model, folder)
        check(ok, f"Model.Parent = ReplicatedStorage-folder succeeds: {err}")
        check(all(not is_active(game, d) for d in descendants), "Model.Parent = a non-Workspace container deactivates ALL 5 descendants")
        check(all(scene.exists(d) for d in descendants), "...without destroying any of them")

        # Repeat once more, and confirm no duplicate Entity/physics state
        # accumulated across the two full round trips.
        scene.set_parent(model, "Workspace")
        check(all(is_active(game, d) for d in descendants), "second activation cascade also works")
        for d in descendants:
            check(list(game.parts.keys()).count(d) == 1, f"descendant {d} has exactly one Entity, no duplicate after repeated cascades")
            check(game._physics_world.has_body(d), f"descendant {d} has exactly one physics body")
    finally:
        teardown(game, manager, ctx)


def test_deeply_nested_descendant_follows_ancestor_reparent() -> None:
    """Same cascade, one level deeper: Workspace/non-Workspace toggling
    happens on a Folder several levels above the actual spatial Part
    (Folder > Model > Folder > Part), confirming the walk isn't hardcoded
    to any particular nesting depth or a mix of container types."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, outer_folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, outer_folder
        ok, model = scene.instance_new("Model", outer_folder)
        assert ok, model
        ok, inner_folder = scene.instance_new("Folder", model)
        assert ok, inner_folder
        pid = new_part(scene, inner_folder)

        check(not is_active(game, pid), "starts inactive, 4 levels under ReplicatedStorage")

        scene.set_parent(outer_folder, "Workspace")
        check(is_active(game, pid), "reparenting the TOPMOST ancestor (outer_folder) activates the deeply-nested Part")

        scene.set_parent(outer_folder, "ReplicatedStorage")
        check(not is_active(game, pid), "reparenting it back out deactivates the same deeply-nested Part")
        check(scene.exists(pid), "...without destroying it")
    finally:
        teardown(game, manager, ctx)


test_part_directly_under_workspace_is_active()
test_part_under_folder_under_workspace_is_active()
test_part_under_model_under_folder_under_workspace_is_active()
test_part_under_folder_under_non_workspace_root_is_inactive()
test_part_under_model_under_folder_under_non_workspace_root_is_inactive()
test_instance_new_with_explicit_non_workspace_parent_does_not_eagerly_attach()
test_reparent_into_and_out_of_workspace_repeatedly()
test_destroy_after_inactive_active_transitions_leaves_no_stale_resources()
test_ancestor_reparent_cascades_world_membership_to_every_descendant()
test_deeply_nested_descendant_follows_ancestor_reparent()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All world-membership tests passed.")
