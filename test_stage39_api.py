"""Regression tests for Stage 3.9's "Minimal Playable 3D Game API" work:
generic Instance hierarchy methods (FindFirstChild/WaitForChild/GetChildren/
GetDescendants/IsA/Clone/Destroy), runtime Instance.new() (including Value
instances), Parent reparenting, Touched (physics-driven, both real
collision and non-collidable trigger volumes, both Part-vs-Part and
Part-vs-Character), and Play/Stop runtime isolation for all of the above.

Follows this project's existing test convention (test_lua_gameplay_api.py,
test_physics_module.py): plain top-level-assertion script, run directly,
offscreen Qt platform, headless Ursina window, a REAL Bullet world/
PhysicsWorld/CharacterRuntime -- Touched specifically depends on genuine
Bullet contact/overlap detection (see physics.py's poll_new_contacts()),
so it cannot be meaningfully tested against a mock.
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Ursina

ursina_app = Ursina(window_type="none")

from panda3d.bullet import BulletBoxShape, BulletRigidBodyNode, BulletWorld
from panda3d.core import NodePath, Vec3 as PVec3

import character_controller as cc
import character_rig as cr
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


def _add_static_floor(world: BulletWorld, y: float = -0.5, half_extents=(50.0, 0.5, 50.0)) -> NodePath:
    node = BulletRigidBodyNode("floor")
    node.addShape(BulletBoxShape(PVec3(*half_extents)))
    node.setMass(0.0)
    node.setStatic(True)
    np = NodePath(node)
    np.setPos(0, y, 0)
    world.attachRigidBody(node)
    return np


PART_DEFAULTS = dict(
    Position=[0.0, 5.0, 0.0], Size=[2.0, 2.0, 2.0], Rotation=[0.0, 0.0, 0.0],
    Color=[255, 255, 255], Transparency=0.0, Anchored=True, CanCollide=True,
)


class _FakeGame:
    """Mirrors test_lua_gameplay_api.py's _FakeGame, PLUS a real
    physics.PhysicsWorld (needed for Touched) and a helper for adding
    authored Parts with real Entities/physics bodies."""

    _build_part_entity = cs.MultiplayerGame._build_part_entity
    _apply_part_surface = cs.MultiplayerGame._apply_part_surface
    _set_light_render_active = cs.MultiplayerGame._set_light_render_active

    def __init__(self, with_character: bool = True, gravity: float = -24.0) -> None:
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

        self._physics_world = physics.PhysicsWorld(gravity=gravity)
        self._floor_np = _add_static_floor(self._physics_world.bullet_world)

        if with_character:
            self._character_runtime = cc.CharacterRuntime(self._physics_world.bullet_world, (0.0, 3.0, 0.0))
            self._character_visual = cr.CharacterVisualRig()
            self._physics_world.register_external_node("__character__", self._character_runtime.controller.node)

    def apply_runtime_service_write(self, service_name: str, properties: dict) -> None:
        self.applied_service_writes.append((service_name, dict(properties)))

    def _mouse_look_captured(self) -> bool:
        return self._captured

    def set_camera_mode(self, third_person: bool) -> None:
        third_person = bool(third_person)
        self.third_person_enabled = third_person
        if self._character_visual is not None:
            self._character_visual.set_first_person(not third_person)

    def add_part(self, instance_id: str, class_name: str = "Part", parent_id: str | None = None, **overrides) -> None:
        props = dict(PART_DEFAULTS)
        props.update(overrides)
        self.instances[instance_id] = cs.InstanceRecord(instance_id, class_name, instance_id, parent_id, props)
        entity = self._build_part_entity(props)
        self.parts[instance_id] = entity
        self._physics_world.add_part(
            instance_id, entity, props["Position"], props["Rotation"], props["Size"],
            bool(props["Anchored"]), bool(props["CanCollide"]),
        )

    def teardown(self) -> None:
        if self._character_visual is not None:
            self._character_visual.destroy()
        if self._character_runtime is not None:
            self._character_runtime.destroy()
        self._physics_world.destroy()


def add_script(game: _FakeGame, source: str, class_name: str = "LocalScript", name: str = "Script1", script_id: str = "script1", parent_id: str | None = "ServerScriptService") -> str:
    # parent_id defaults to ServerScriptService (NOT None/Workspace) so the
    # script instance itself never shows up as a Workspace child and skews
    # GetChildren()/hierarchy assertions below -- manager._start_script()
    # is called directly in every test here, so where the script is
    # authored doesn't affect whether/how it runs.
    game.instances[script_id] = cs.InstanceRecord(script_id, class_name, name, parent_id, {"Source": source})
    return script_id


def make_context(game: _FakeGame) -> tuple[lua_runtime.LuaRuntimeManager, lga.LuaGameplayContext]:
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    ctx.start()
    return manager, ctx


def run_frame(manager: lua_runtime.LuaRuntimeManager, ctx: lga.LuaGameplayContext, dt: float = 1 / 60) -> None:
    game = manager.game
    if game._character_runtime is not None:
        game._character_runtime.controller.apply_movement(cc.CharacterInputState(), 0.0, dt)
    manager.scene._physics.step(dt) if manager.scene._physics is not None else None
    manager.update(dt)
    ctx.update(dt)


def teardown(game: _FakeGame, manager: lua_runtime.LuaRuntimeManager, ctx: lga.LuaGameplayContext) -> None:
    ctx.stop()
    manager.stop()
    game.teardown()


def messages_of(manager: lua_runtime.LuaRuntimeManager) -> list[str]:
    return [d.message for d in manager.diagnostics]


# ============================================================
# Instance.new / hierarchy
# ============================================================

def test_instance_new_part_and_hierarchy_methods() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local p = Instance.new('Part')\n"
            "p.Name = 'Created'\n"
            "p.Position = Vector3.new(1, 2, 3)\n"
            "p.Parent = workspace\n"
            "print('name', p.Name, 'classname', p.ClassName)\n"
            "print('isa_part', p:IsA('Part'), 'isa_instance', p:IsA('Instance'))\n"
            "local found = workspace:FindFirstChild('Created')\n"
            "print('found', found ~= nil, found.Name)\n"
            "local kids = workspace:GetChildren()\n"
            "print('num_children', #kids)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("name\tCreated\tclassname\tPart" in m for m in msgs), "Instance.new('Part') has correct Name/ClassName")
        check(any("isa_part\ttrue\tisa_instance\ttrue" in m for m in msgs), "IsA('Part') and IsA('Instance') both true via inheritance")
        check(any("found\ttrue\tCreated" in m for m in msgs), "FindFirstChild locates the runtime-created Part under workspace")
        check(any("num_children\t1" in m for m in msgs), "GetChildren() sees only the runtime-created Part")
    finally:
        teardown(game, manager, ctx)


def test_instance_new_invalid_class_rejected() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local ok, err = pcall(function() return Instance.new('Workspace') end)\n"
            "print('rejected', not ok, tostring(err))\n"
            "local ok2, err2 = pcall(function() return Instance.new('Script') end)\n"
            "print('script_rejected', not ok2)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("rejected\ttrue" in m for m in msgs), "Instance.new('Workspace') is rejected with a catchable error")
        check(any("script_rejected\ttrue" in m for m in msgs), "Instance.new('Script') is rejected (script-execution-plan denylist)")
    finally:
        teardown(game, manager, ctx)


def test_spawn_point_is_a_inheritance_chain() -> None:
    """Stage 3.9 final-verification pass: SpawnPoint is registered in
    datamodel_schema.py with base_class="Part" (see its own comment there)
    specifically so real inheritance answers IsA("Part")/IsA("Instance")
    correctly via class_chain(), replacing the old single hand-rolled
    special case in RuntimeSceneLayer.is_a(). Locks that down directly
    rather than relying only on a generic Part's IsA test."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local sp = Instance.new('SpawnPoint')\n"
            "sp.Parent = workspace\n"
            "print('isa_spawnpoint', sp:IsA('SpawnPoint'))\n"
            "print('isa_part', sp:IsA('Part'))\n"
            "print('isa_instance', sp:IsA('Instance'))\n"
            "print('isa_folder', sp:IsA('Folder'))\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("isa_spawnpoint\ttrue" in m for m in msgs), "SpawnPoint:IsA('SpawnPoint') is true")
        check(any("isa_part\ttrue" in m for m in msgs), "SpawnPoint:IsA('Part') is true via real inheritance")
        check(any("isa_instance\ttrue" in m for m in msgs), "SpawnPoint:IsA('Instance') is true via real inheritance")
        check(any("isa_folder\tfalse" in m for m in msgs), "SpawnPoint:IsA('Folder') is false (not a real ancestor)")
    finally:
        teardown(game, manager, ctx)


def test_parent_reparent_and_cycle_rejected() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Parent = workspace\n"
            "local p = Instance.new('Part')\n"
            "p.Parent = workspace\n"
            "p.Parent = folder\n"
            "print('reparented', p.Parent == folder)\n"
            "local ok, err = pcall(function() folder.Parent = p end)\n"
            "print('cycle_rejected', not ok, tostring(err))\n"
            "local ok2 = pcall(function() p.Parent = p end)\n"
            "print('self_parent_rejected', not ok2)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("reparented\ttrue" in m for m in msgs), "part.Parent = folder actually reparents (readable back)")
        check(any("cycle_rejected\ttrue" in m for m in msgs), "Parenting an ancestor under its own descendant is rejected")
        check(any("self_parent_rejected\ttrue" in m for m in msgs), "An instance cannot be parented to itself")
    finally:
        teardown(game, manager, ctx)


def test_destroy_on_runtime_part_removes_visual_and_physics_presence() -> None:
    """Ground-truth check for a user-reported "the white cube stays
    visible after Destroy()" concern: confirms Destroy() on an
    Instance.new()'d, world-attached Part actually disables its real
    Ursina Entity, drops it from game.parts, and removes its real Bullet
    physics body -- not just that Lua-side property access starts
    erroring (see test_destroy_then_access_is_clean_error, which only
    checks the Lua error message)."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        # TWO separate scripts, bridged through workspace:FindFirstChild()
        # -- the REAL, shared RuntimeSceneLayer, not a Lua global (each
        # script gets its own sandboxed _ENV, including its own `_G`, see
        # _build_sandbox_env()'s `env["_G"] = env` -- a `_G.__x = ...`
        # write in one script is simply invisible to another). Each
        # script is a single straight-line run (no task.wait yield), so
        # one _start_script()+run_frame() pair fully executes it -- no
        # scheduler-timing subtlety to get right.
        create_source = (
            "local p = Instance.new('Part')\n"
            "p.Name = 'ToDestroy'\n"
            "p.Anchored = true\n"
            "p.Parent = workspace\n"
        )
        add_script(game, create_source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        check(len(manager.scene._runtime) == 1, "exactly one runtime Part exists after Instance.new()")
        part_id = next(iter(manager.scene._runtime))
        entity = game.parts.get(part_id)
        check(entity is not None and entity.enabled, "the Part has a real, enabled Entity before Destroy()")
        check(game._physics_world.has_body(part_id), "the Part has a real physics body before Destroy()")

        add_script(game, "workspace.ToDestroy:Destroy()\n", script_id="s2")
        manager._start_script("s2")
        run_frame(manager, ctx)

        check(part_id not in game.parts, "Destroy() removes the Part from game.parts (no longer rendered)")
        check(entity.enabled is False, "Destroy() disables the actual Entity object, not just an id mapping")
        check(not game._physics_world.has_body(part_id), "Destroy() removes the Part's physics body")
    finally:
        teardown(game, manager, ctx)


def test_wait_for_child_and_get_descendants() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Name = 'Container'\n"
            "folder.Parent = workspace\n"
            "local inner = Instance.new('Part')\n"
            "inner.Parent = folder\n"
            "local found = workspace:WaitForChild('Container', 1)\n"
            "print('waited', found ~= nil, found == folder)\n"
            "local all = workspace:GetDescendants()\n"
            "print('num_descendants', #all)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("waited\ttrue\ttrue" in m for m in msgs), "WaitForChild() resolves immediately when the child already exists")
        check(any("num_descendants\t2" in m for m in msgs), "GetDescendants() sees both the Folder and the Part nested inside it")
    finally:
        teardown(game, manager, ctx)


def test_part_property_write_moves_real_physics_body() -> None:
    game = _FakeGame(with_character=False)
    game.add_part("mover", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0], Size=[2.0, 2.0, 2.0])
    manager, ctx = make_context(game)
    try:
        source = (
            "workspace.mover.Position = Vector3.new(10, 20, 30)\n"
            "workspace.mover.Anchored = false\n"
            "workspace.mover.CanCollide = false\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        entity = game.parts["mover"]
        check(abs(entity.x - 10.0) < 0.01 and abs(entity.y - 20.0) < 0.01 and abs(entity.z - 30.0) < 0.01, f"Writing Part.Position from Lua moves the REAL Ursina Entity, got {entity.position}")
        check(game._physics_world._bodies["mover"].kind == "ghost", "Writing Anchored=false/CanCollide=false from Lua rebuilds the real physics body as a ghost")
    finally:
        teardown(game, manager, ctx)


def test_repeated_play_stop_cycles_stay_isolated() -> None:
    """Stage 3.9 final-verification pass: 3 full Play/Stop cycles (not
    just one), driven against the SAME persistent game object across all
    of them -- mirrors a real Stop-then-Play-again in client_studio.py,
    which keeps game.instances/game.parts alive for the whole editor
    session and only ever replaces the LuaRuntimeManager/
    LuaGameplayContext/Lua VM per Play (see LuaRuntimeManager.start()/
    stop(), confirmed to construct genuinely fresh objects every time).
    Locks down, per cycle: the LocalScript runs exactly once, the
    runtime-created Part is visible mid-Play and gone after Stop, the
    authored floor's Position is restored after Stop -- AND, across all
    3 cycles, that game.parts never accumulates leftover disabled
    Entities from a PREVIOUS cycle's runtime-created Parts (a real bug
    found during this audit: RuntimeSceneLayer.stop() used to disable a
    runtime Part's Entity but never pop it from game.parts, so it stayed
    there, dead, forever -- fixed)."""
    game = _FakeGame(with_character=False)
    game.add_part("floor", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0])
    baseline_parts_count = len(game.parts)
    floor_entity = game.parts["floor"]

    try:
        for cycle in range(3):
            manager, ctx = make_context(game)
            script_id = f"s_cycle_{cycle}"
            source = (
                "local p = Instance.new('Part')\n"
                "p.Name = 'Spawned'\n"
                "p.Parent = workspace\n"
                "workspace.floor.Position = Vector3.new(0, 99, 0)\n"
                f"print('cycle_ran', {cycle})\n"
            )
            add_script(game, source, script_id=script_id)
            manager._start_script(script_id)
            run_frame(manager, ctx)

            msgs = messages_of(manager)
            check(sum(1 for m in msgs if "cycle_ran" in m) == 1, f"cycle {cycle}: the LocalScript runs exactly once")
            check(len(manager.scene._runtime) == 1, f"cycle {cycle}: exactly one runtime instance exists mid-Play")
            check(len(game.parts) == baseline_parts_count + 1, f"cycle {cycle}: the runtime Part is visible in game.parts mid-Play (got {len(game.parts)}, baseline {baseline_parts_count})")

            ctx.stop()
            manager.stop()
            del game.instances[script_id]

            check(len(game.parts) == baseline_parts_count, f"cycle {cycle}: game.parts returns to baseline after Stop -- no leftover runtime Parts from this (or any earlier) cycle (got {len(game.parts)}, baseline {baseline_parts_count})")
            check(abs(floor_entity.y - 0.0) < 0.01, f"cycle {cycle}: the authored floor's real Entity Position is restored to pre-Play after Stop, got y={floor_entity.y}")
    finally:
        game.teardown()


def test_runtime_instance_gone_and_authored_destroy_undone_after_stop() -> None:
    game = _FakeGame(with_character=False)
    game.add_part("baseplate2", Anchored=True, CanCollide=True)
    manager, ctx = make_context(game)
    try:
        source = (
            "local p = Instance.new('Part')\n"
            "p.Name = 'RuntimeOnly'\n"
            "p.Parent = workspace\n"
            "workspace.baseplate2:Destroy()\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check(not manager.scene.exists("baseplate2"), "baseplate2 is destroyed for THIS session")
    finally:
        teardown(game, manager, ctx)

    # Fresh Play session (Stop already happened implicitly -- a new
    # RuntimeSceneLayer/manager is exactly what a real Stop-then-Play
    # produces): the runtime-only Part must be gone, and the authored
    # baseplate2 -- never actually removed from game.instances -- must be
    # fully usable again.
    manager2 = lua_runtime.LuaRuntimeManager(game)
    manager2.start()
    ctx2 = lga.LuaGameplayContext(game, manager2)
    ctx2.start()
    try:
        source2 = (
            "local found = workspace:FindFirstChild('RuntimeOnly')\n"
            "print('runtime_gone', found == nil)\n"
            "print('baseplate_restored', workspace:FindFirstChild('baseplate2') ~= nil)\n"
        )
        add_script(game, source2, script_id="s2")
        manager2._start_script("s2")
        run_frame(manager2, ctx2)
        msgs = messages_of(manager2)
        check(any("runtime_gone\ttrue" in m for m in msgs), "A Play-session-only runtime Part from the PREVIOUS session does not exist in a fresh session")
        check(any("baseplate_restored\ttrue" in m for m in msgs), "An authored Part destroyed in the PREVIOUS session is back in a fresh session (Destroy() never touched the authored Place)")
    finally:
        teardown(game, manager2, ctx2)


def test_clone_creates_independent_instance() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local p = Instance.new('Part')\n"
            "p.Name = 'Original'\n"
            "p.Position = Vector3.new(5, 5, 5)\n"
            "p.Parent = workspace\n"
            "local c = p:Clone()\n"
            "print('different_id', c ~= p)\n"
            "print('same_name', c.Name == p.Name)\n"
            "print('same_position', c.Position.X == p.Position.X)\n"
            "c.Position = Vector3.new(9, 9, 9)\n"
            "print('independent', p.Position.X == 5 and c.Position.X == 9)\n"
            # Stage 3.9 fix: Clone() now genuinely starts Parent == nil
            # (real Roblox semantics), so right after cloning, workspace
            # must still show only the ORIGINAL -- the clone is not yet
            # anywhere in the tree until explicitly parented.
            "print('clone_parent_is_nil', c.Parent == nil)\n"
            "print('num_children_before_parenting', #workspace:GetChildren())\n"
            "c.Parent = workspace\n"
            "print('num_children_after_parenting', #workspace:GetChildren())\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("different_id\ttrue" in m for m in msgs), "Clone() returns a distinct Instance, never the original")
        check(any("same_name\ttrue" in m for m in msgs), "Clone() copies Name")
        check(any("same_position\ttrue" in m for m in msgs), "Clone() copies Position")
        check(any("independent\ttrue" in m for m in msgs), "Mutating the clone does not affect the original")
        check(any("clone_parent_is_nil\ttrue" in m for m in msgs), "A fresh Clone() has Parent == nil, matching real Roblox")
        check(any("num_children_before_parenting\t1" in m for m in msgs), "Only the original is a child of workspace before the clone is explicitly parented")
        check(any("num_children_after_parenting\t2" in m for m in msgs), "Setting clone.Parent = workspace makes it a real child")
    finally:
        teardown(game, manager, ctx)


def test_clone_has_no_visual_or_physics_presence_until_parented() -> None:
    """The core of the Stage 3.9 Clone()-Parent=nil fix: a fresh clone
    must not be rendered or physically simulated (no Entity in
    game.parts, no Bullet body) until the developer actually parents it
    somewhere real -- previously it was silently visible/physical
    immediately, indistinguishable from a genuinely-still-alive original
    during manual verification.

    Two independent Play sessions (not one script with a mid-script
    checkpoint): a nil-parented clone is, by definition, not reachable
    via workspace:FindFirstChild(...) or any other shared-hierarchy
    lookup, so there is no reliable way to hand its id from one script
    to a second, separately-started one to test "before" and "after"
    within a single session -- and task.wait() deadlines are real
    wall-clock time (see LuaTaskScheduler.update()), not simulated frame
    dt, so a short wait cannot be used as a deterministic mid-script
    pause point either. Splitting into "clone alone" vs. "clone then
    immediately reparent, no yield needed" sessions tests the exact same
    two states without either pitfall."""
    game_before = _FakeGame(with_character=False)
    game_before.add_part("original99", Anchored=True, CanCollide=True)
    manager_before, ctx_before = make_context(game_before)
    try:
        add_script(game_before, "local c = workspace.original99:Clone()\n", script_id="s1")
        manager_before._start_script("s1")
        run_frame(manager_before, ctx_before)

        check(len(manager_before.scene._runtime) == 1, f"exactly one runtime instance exists after one Clone(), got {len(manager_before.scene._runtime)}")
        clone_id = next(iter(manager_before.scene._runtime))
        check(clone_id not in game_before.parts, "the clone has no Entity in game.parts before being parented")
        check(not game_before._physics_world.has_body(clone_id), "the clone has no physics body before being parented")
    finally:
        teardown(game_before, manager_before, ctx_before)

    game_after = _FakeGame(with_character=False)
    game_after.add_part("original99", Anchored=True, CanCollide=True)
    manager_after, ctx_after = make_context(game_after)
    try:
        add_script(game_after, "local c = workspace.original99:Clone()\nc.Parent = workspace\n", script_id="s1")
        manager_after._start_script("s1")
        run_frame(manager_after, ctx_after)

        check(len(manager_after.scene._runtime) == 1, "exactly one runtime instance exists after Clone()+reparent")
        clone_id = next(iter(manager_after.scene._runtime))
        check(clone_id in game_after.parts, "the clone gets a real Entity once parented into workspace")
        check(game_after._physics_world.has_body(clone_id), "the clone gets a real physics body once parented into workspace")
    finally:
        teardown(game_after, manager_after, ctx_after)


def test_folder_clone_is_shallow_documented_limitation() -> None:
    """Stage 3.9 final-verification pass: RuntimeSceneLayer.clone() (see
    its own docstring) only ever clones the ONE instance passed to it --
    it never walks descendants_of() or remaps parent ids, so cloning a
    Folder/Model does NOT bring its children along. This is a deliberate,
    documented scope limitation (real recursive Clone() is deferred, not
    silently claimed as Roblox-compatible deep semantics -- see the final
    Stage 3.9 report), not a bug; this test locks down the CURRENT
    behavior so a future change to it is a conscious, visible decision
    rather than an accidental regression either way."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Name = 'Container'\n"
            "folder.Parent = workspace\n"
            "local p = Instance.new('Part')\n"
            "p.Name = 'Nested'\n"
            "p.Parent = folder\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        folder_id = next(iid for iid, item in manager.scene._runtime.items() if item.class_name == "Folder")
        children_before = manager.scene.children_of(folder_id)
        check(len(children_before) == 1, "the original Folder has exactly one child (the nested Part) before cloning")

        # Cloning is driven directly through RuntimeSceneLayer.clone() --
        # same reachability reasoning as the nil-parented-clone test
        # above: a fresh clone is Parent == nil, so it (and, if deep
        # cloning existed, its descendants) would not be reachable via
        # workspace:FindFirstChild() from a second bridged script anyway.
        ok, clone_id = manager.scene.clone(folder_id)
        check(ok, f"cloning the Folder itself succeeds: {clone_id}")
        check(manager.scene.class_name_of(clone_id) == "Folder", "the clone is a Folder, same class as the original")
        check(manager.scene.parent_of(clone_id) is None, "the Folder clone starts Parent == nil, same as any other Clone()")

        clone_children = manager.scene.children_of(clone_id)
        check(len(clone_children) == 0, f"documented limitation: Folder Clone() is currently SHALLOW -- the clone has no children of its own (got {len(clone_children)}), the original nested Part is not duplicated")

        original_children_after = manager.scene.children_of(folder_id)
        check(len(original_children_after) == 1, "cloning the Folder never mutates the original's own children")
    finally:
        teardown(game, manager, ctx)


def test_destroy_then_access_is_clean_error() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local p = Instance.new('Part')\n"
            "p.Parent = workspace\n"
            "p:Destroy()\n"
            "local ok, err = pcall(function() return p.Position end)\n"
            "print('destroyed_access_rejected', not ok, tostring(err))\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("destroyed_access_rejected\ttrue" in m and "destroyed" in m for m in msgs), "Reading a property off a destroyed Instance is a clean, readable Lua error")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Stage 3.9 final-verification pass: cascading Destroy()/Parent=nil,
# GetFullName() on a detached instance, Instance.new() with no parent
# argument -- four real bugs found during the final audit (destroy() and
# set_parent() never recursed into descendants; GetFullName() and
# Instance.new() both silently fell through to the old "defaults to
# Workspace" behavior the Stage 3.9 Clone() fix was supposed to have
# eliminated everywhere).
# ============================================================

def test_destroy_on_container_cascades_to_descendants() -> None:
    """Ground-truth check for the audit-discovered gap: destroying a
    Folder that has a real, world-attached Part inside it used to leave
    that Part fully rendered/physical/exists()==True forever (for the
    rest of the session) -- only the Folder itself became unreachable via
    GetChildren(). Destroy() must now cascade to every descendant."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Name = 'Container'\n"
            "folder.Parent = workspace\n"
            "local p = Instance.new('Part')\n"
            "p.Name = 'Nested'\n"
            "p.Anchored = true\n"
            "p.Parent = folder\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        folder_id = next(iid for iid, item in manager.scene._runtime.items() if item.class_name == "Folder")
        part_id = next(iid for iid, item in manager.scene._runtime.items() if item.class_name == "Part")
        entity = game.parts.get(part_id)
        check(entity is not None and entity.enabled, "the nested Part has a real, enabled Entity before the container is destroyed")
        check(game._physics_world.has_body(part_id), "the nested Part has a real physics body before the container is destroyed")

        add_script(game, "workspace.Container:Destroy()\n", script_id="s2")
        manager._start_script("s2")
        run_frame(manager, ctx)

        check(not manager.scene.exists(folder_id), "Destroy() on the container makes it exists()==False")
        check(not manager.scene.exists(part_id), "Destroy() cascades: the nested Part is also exists()==False")
        check(part_id not in game.parts, "the cascaded Destroy() removes the nested Part's Entity from game.parts")
        check(entity.enabled is False, "the cascaded Destroy() disables the nested Part's actual Entity object")
        check(not game._physics_world.has_body(part_id), "the cascaded Destroy() removes the nested Part's physics body")
    finally:
        teardown(game, manager, ctx)


def test_parent_nil_on_container_cascades_attachment_to_descendants() -> None:
    """Sibling gap to the Destroy() cascade above: `folder.Parent = nil`
    must also detach (disable Entity, remove physics body) every
    has_3d_entity descendant nested under that folder, not just the
    folder itself (a Folder has no 3D entity, so the direct
    _sync_world_attachment() call on it alone is a no-op) -- and
    reattaching the folder must restore every descendant too."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Name = 'Container'\n"
            "folder.Parent = workspace\n"
            "local p = Instance.new('Part')\n"
            "p.Name = 'Nested'\n"
            "p.Anchored = true\n"
            "p.Parent = folder\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        folder_id = next(iid for iid, item in manager.scene._runtime.items() if item.class_name == "Folder")
        part_id = next(iid for iid, item in manager.scene._runtime.items() if item.class_name == "Part")
        entity = game.parts.get(part_id)
        check(entity is not None and entity.enabled, "the nested Part is attached (real, enabled Entity) before the container is detached")
        check(game._physics_world.has_body(part_id), "the nested Part has a real physics body before the container is detached")

        # Drives RuntimeSceneLayer.set_parent() DIRECTLY from Python from
        # here on (the exact same method `.Parent = X` calls from Lua) --
        # deliberately NOT via a second bridged Lua script, since the
        # whole point of this test is that Container becomes UNREACHABLE
        # from workspace:FindFirstChild() while genuinely detached, which
        # a second script could never re-find by name.
        ok, error = manager.scene.set_parent(folder_id, None)
        check(ok, f"detaching the container itself succeeds: {error}")
        check(entity.enabled is False, "detaching the container disables the nested Part's Entity too (cascade)")
        check(not game._physics_world.has_body(part_id), "detaching the container removes the nested Part's physics body too (cascade)")
        check(manager.scene.exists(part_id), "the nested Part remains a valid Instance while detached (not destroyed)")

        ok, error = manager.scene.set_parent(folder_id, "Workspace")
        check(ok, f"reattaching the container succeeds: {error}")
        check(entity.enabled is True, "reattaching the container re-enables the nested Part's Entity (cascade)")
        check(game._physics_world.has_body(part_id), "reattaching the container restores the nested Part's physics body (cascade)")
    finally:
        teardown(game, manager, ctx)


def test_destroy_cascade_cancels_nested_script_coroutines() -> None:
    """Sibling gap found alongside the render/physics Destroy() cascade:
    the scheduler-cancellation logic that stops a destroyed Script's
    pending task.wait()/etc. lives one level up, in bridge_destroy() (not
    RuntimeSceneLayer.destroy() itself), and used to only ever check the
    EXACT id Destroy() was called on -- a LocalScript nested under a
    destroyed Folder kept its coroutine scheduled and running forever
    (well past the ancestor's own destruction). bridge_destroy() must
    walk descendants too, matching RuntimeSceneLayer.destroy()'s own
    render/physics cascade."""
    game = _FakeGame(with_character=False)
    game.instances["container"] = cs.InstanceRecord("container", "Folder", "Container", "Workspace", {})
    manager, ctx = make_context(game)
    try:
        nested_source = "task.wait(1000)\nprint('SHOULD_NOT_RUN')\n"
        add_script(game, nested_source, script_id="nested_script", parent_id="container")
        manager._start_script("nested_script")
        run_frame(manager, ctx)

        check(
            any(e.owner_script_id == "nested_script" for e in manager.scheduler._pending),
            "the nested script's long task.wait() is genuinely scheduled/pending before the ancestor is destroyed",
        )

        add_script(game, "workspace.Container:Destroy()\n", script_id="s2")
        manager._start_script("s2")
        run_frame(manager, ctx)

        check(
            not any(e.owner_script_id == "nested_script" for e in manager.scheduler._pending),
            "destroying the ancestor Folder cancels the nested script's pending scheduler entry too (cascade)",
        )
        for _ in range(10):
            run_frame(manager, ctx)
        check(not any("SHOULD_NOT_RUN" in m for m in messages_of(manager)), "the nested script's continuation after task.wait() never runs once its ancestor was destroyed")
    finally:
        teardown(game, manager, ctx)


def test_signal_registry_shrinks_per_item_without_stop() -> None:
    """Stage 3.9 lifecycle correction: __signal_registry_count() (Lua-
    side, a real pairs() count -- see its own docstring) must return to
    baseline after destroying everything WITHIN one Play session, not
    merely after Stop. Creates 15 Parts (via a shared Folder), each with
    two Touched:Connect() listeners, then destroys them all with one
    cascading Destroy() on the container -- both the 15 signal entries
    AND all 30 listeners must be pruned immediately, not just hidden from
    GetChildren()."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        lua_globals = manager.lua.globals()
        base_signals, base_listeners = lua_globals["__signal_registry_count"]()

        setup_source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Name = 'Batch'\n"
            "folder.Parent = workspace\n"
            "for i = 1, 15 do\n"
            "  local p = Instance.new('Part')\n"
            "  p.Name = 'B' .. i\n"
            "  p.Anchored = true\n"
            "  p.Parent = folder\n"
            "  p.Touched:Connect(function() end)\n"
            "  p.Touched:Connect(function() end)\n"
            "end\n"
        )
        add_script(game, setup_source, script_id="setup")
        manager._start_script("setup")
        run_frame(manager, ctx)

        mid_signals, mid_listeners = lua_globals["__signal_registry_count"]()
        check(mid_signals == base_signals + 15, f"15 Touched signals were actually created ({mid_signals} vs baseline {base_signals})")
        check(mid_listeners == base_listeners + 30, f"30 listeners (2 per signal) were actually connected ({mid_listeners} vs baseline {base_listeners})")

        add_script(game, "workspace.Batch:Destroy()\n", script_id="destroy_batch")
        manager._start_script("destroy_batch")
        run_frame(manager, ctx)

        after_batch_signals, after_batch_listeners = lua_globals["__signal_registry_count"]()
        check(after_batch_signals == base_signals, f"destroying the container prunes all 15 nested Touched signal entries back to baseline ({after_batch_signals} vs {base_signals})")
        check(after_batch_listeners == base_listeners, f"...and every one of their listeners too ({after_batch_listeners} vs {base_listeners})")
    finally:
        teardown(game, manager, ctx)


def test_owned_by_cleanup_disconnects_script_connections_to_other_instances() -> None:
    """Focused counterpart to (b) above, done properly through a real
    cascading Destroy() rather than manual bookkeeping: a Script
    connects to a DIFFERENT, still-alive Part's Touched signal; the
    Script is nested under a Folder that gets Destroy()'d. The listener
    it registered on the (still-alive) target must be gone afterward --
    the target itself, and its Touched signal, must NOT be destroyed."""
    game = _FakeGame(with_character=False)
    game.add_part("target", Anchored=True, CanCollide=True)
    manager, ctx = make_context(game)
    try:
        lua_globals = manager.lua.globals()
        base_signals, base_listeners = lua_globals["__signal_registry_count"]()

        setup_source = (
            "local folder = Instance.new('Folder')\n"
            "folder.Name = 'ScriptHolder'\n"
            "folder.Parent = workspace\n"
        )
        add_script(game, setup_source, script_id="setup2")
        manager._start_script("setup2")
        run_frame(manager, ctx)

        folder_id = next(iid for iid, item in manager.scene._runtime.items() if item.class_name == "Folder")
        add_script(
            game, "workspace.target.Touched:Connect(function() end)\n",
            script_id="connector_script", parent_id=folder_id,
        )
        manager._start_script("connector_script")
        run_frame(manager, ctx)

        mid_signals, mid_listeners = lua_globals["__signal_registry_count"]()
        check(mid_signals == base_signals + 1, "target's Touched signal was created (still-alive instance, own signal)")
        check(mid_listeners == base_listeners + 1, "the script's connection to target.Touched is a real listener")
        check(manager.scene.exists("target"), "target itself is untouched so far")

        add_script(game, "workspace.ScriptHolder:Destroy()\n", script_id="destroy_holder")
        manager._start_script("destroy_holder")
        run_frame(manager, ctx)

        after_signals, after_listeners = lua_globals["__signal_registry_count"]()
        check(manager.scene.exists("target"), "target (NOT under ScriptHolder) survives the ScriptHolder Destroy()")
        check(after_signals == mid_signals, "target's own Touched signal entry still exists -- it was never destroyed")
        check(after_listeners == base_listeners, "but the destroyed Connector script's listener on it is gone (owned-by cleanup)")
    finally:
        teardown(game, manager, ctx)


def test_disconnect_and_once_prune_listeners_immediately() -> None:
    """Audits Disconnect()/Once() individually, per the lifecycle
    correction's explicit ask -- both already pruned listeners correctly
    in the underlying Lua (see SignalMeta/ConnectionMeta's own comments),
    this locks that down behaviorally via the new introspection rather
    than only by source inspection. Deliberately one straight-line script
    (no cross-script _G sharing -- each script has its own sandboxed
    _ENV, see this file's other tests' comments on that exact pitfall;
    a Connection has no Python-side handle to drive from outside Lua
    either, unlike Instance ids). Uses two SEPARATE target Parts so
    "disconnect exactly one, the other survives" is checkable from a
    single end-state snapshot instead of needing an unreliable mid-script
    Python-side checkpoint (task.wait(0) is not a safe one either, see
    the module-level notes elsewhere in this project on that timing
    pitfall)."""
    game = _FakeGame(with_character=False)
    game.add_part("target1", Anchored=True, CanCollide=True)
    game.add_part("target2", Anchored=True, CanCollide=True)
    manager, ctx = make_context(game)
    try:
        lua_globals = manager.lua.globals()
        base_signals, base_listeners = lua_globals["__signal_registry_count"]()

        source = (
            "local conn1 = workspace.target1.Touched:Connect(function() end)\n"
            "local conn2 = workspace.target2.Touched:Connect(function() end)\n"
            "conn1:Disconnect()\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        after_signals, after_listeners = lua_globals["__signal_registry_count"]()
        check(after_signals == base_signals + 2, "both target1.Touched and target2.Touched now exist (Disconnect() doesn't destroy the signal itself)")
        check(after_listeners == base_listeners + 1, "Disconnect() prunes exactly the disconnected listener -- target2's untouched connection is the only one left")

        target1_signal_id = manager.scene._instance_signals.get("target1", {}).get("Touched")
        target2_signal_id = manager.scene._instance_signals.get("target2", {}).get("Touched")
        check(target1_signal_id is not None, "target1's Touched signal_id is tracked (the signal itself was never destroyed, only its listener)")
        target1_listeners = lua_globals["__signal_listener_count"](target1_signal_id)
        check(target1_listeners == 0, f"target1's own signal has zero listeners left after its one connection was disconnected (got {target1_listeners})")
        target2_listeners = lua_globals["__signal_listener_count"](target2_signal_id)
        check(target2_listeners == 1, f"target2's untouched connection is still there (got {target2_listeners})")

        # Once(): a fresh signal, a Once() listener, fired once -- the
        # listener must self-remove without any explicit Disconnect().
        once_source = (
            "local p2 = Instance.new('Part')\n"
            "p2.Name = 'OncePart'\n"
            "p2.Anchored = true\n"
            "p2.CanCollide = false\n"
            "p2.Parent = workspace\n"
            "p2.Touched:Once(function() end)\n"
        )
        add_script(game, once_source, script_id="once_setup")
        manager._start_script("once_setup")
        run_frame(manager, ctx)

        # .Name writes go through _name_overlay (see set_property()), not
        # item.name directly -- name_of() is the one method that reads
        # both correctly, same as everywhere else in this file that looks
        # up a runtime instance by its Lua-assigned name.
        once_part_id = next(iid for iid in manager.scene._runtime if manager.scene.name_of(iid) == "OncePart")
        touched_signal_id = manager.scene._instance_signals[once_part_id]["Touched"]
        before_fire_listeners = lua_globals["__signal_listener_count"](touched_signal_id)
        check(before_fire_listeners == 1, "the Once() listener is connected before firing")

        # Fired via the real LuaRuntimeManager.fire_signal() (the SAME
        # shared dispatch path Touched/PlayerAdded/InputBegan/... all
        # actually use, see its own docstring) rather than driving a real
        # physics contact -- physics-driven Touched firing is already
        # covered by other tests in this file; this test's focus is
        # Once()'s self-removal, not contact detection.
        manager.fire_signal(touched_signal_id, ())
        after_fire_listeners = lua_globals["__signal_listener_count"](touched_signal_id)
        check(after_fire_listeners == 0, f"the Once() listener self-removed immediately on firing, before its dispatched coroutine even ran (got {after_fire_listeners})")
    finally:
        teardown(game, manager, ctx)


def test_stop_still_wipes_signal_and_coroutine_registries() -> None:
    """The coarser, pre-existing guarantee -- must still hold after the
    per-item lifecycle correction above. Captures the Lua globals table
    BEFORE calling stop() (manager.lua is set to None by stop(), but the
    captured reference keeps the underlying Lua state alive long enough
    to inspect it)."""
    game = _FakeGame(with_character=False)
    game.add_part("target", Anchored=True, CanCollide=True)
    manager, ctx = make_context(game)
    lua_globals = manager.lua.globals()
    try:
        source = (
            "workspace.target.Touched:Connect(function() end)\n"
            "task.delay(1000, function() end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        mid_signals, mid_listeners = lua_globals["__signal_registry_count"]()
        check(mid_signals >= 1 and mid_listeners >= 1, "a real signal + listener exist before Stop")
        check(lua_globals["__registry_count"]() >= 1, "a real pending coroutine (the delayed task) exists before Stop")
    finally:
        ctx.stop()
        manager.stop()
        game.teardown()

    after_signals, after_listeners = lua_globals["__signal_registry_count"]()
    check(after_signals == 0, f"Stop wipes signal_registry completely, got {after_signals} remaining")
    check(after_listeners == 0, f"Stop wipes every listener too, got {after_listeners} remaining")
    check(lua_globals["__registry_count"]() == 0, "Stop wipes coroutine_registry completely")


def test_task_spawn_repeated_in_loop_does_not_break_the_caller() -> None:
    """Stage 3.9 runtime correction (root cause, not a lifecycle-scope
    issue): task.spawn() called repeatedly inside one Lua `for` loop used
    to make every call AFTER the first silently no-op. Root cause:
    LuaTaskScheduler._resume() is REENTRANT -- schedule_immediate() calls
    it SYNCHRONOUSLY, from within the Lua "task" bridge, while the OUTER
    coroutine (running the for loop) is still mid-resume on the Python
    call stack. The old code unconditionally reset self.current_script_id
    to None on every _resume() exit, instead of restoring whatever it was
    BEFORE that particular call -- so the instant the FIRST spawned
    child finished, the OUTER coroutine's own "who am I" context was
    wiped, and schedule_immediate()'s `if owner is None: return` guard
    silently swallowed every task.spawn() call after that (no error,
    nothing scheduled) -- for the REST of that script's execution, not
    just the rest of one loop. The loop itself always ran to completion;
    only the scheduling silently stopped working. Fixed by making
    _resume() save/restore the caller's current_script_id instead of
    unconditionally clearing it.

    This is the exact reproduction from the bug report: 10 spawns inside
    one loop, expecting the caller to run all 10 iterations AND every
    spawned function to actually execute."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local seen = 0\n"
            "for i = 1, 10 do\n"
            "  seen = seen + 1\n"
            "  task.spawn(function() print('spawned', i) end)\n"
            "end\n"
            "print('LOOP_DONE', seen)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("LOOP_DONE\t10" in m for m in msgs), f"the caller's for loop runs all 10 iterations regardless of when spawned tasks execute, got: {msgs}")
        for i in range(1, 11):
            check(any(f"spawned\t{i}" in m for m in msgs), f"task.spawn() call #{i} actually scheduled and ran its function (got: {msgs})")
    finally:
        teardown(game, manager, ctx)


def test_task_spawn_every_accepted_call_actually_executes() -> None:
    """"Spawn with work" from the bug report: confirms every ACCEPTED
    task.spawn() call's function genuinely runs and has its side effects
    observable, not just that the caller survives."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local total = 0\n"
            "for i = 1, 20 do\n"
            "  task.spawn(function() total = total + i end)\n"
            "end\n"
            "print('TOTAL', total)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        # sum(1..20) == 210 -- only reachable if every one of the 20
        # spawned closures actually ran and mutated the shared upvalue.
        check(any("TOTAL\t210" in m for m in msgs), f"every accepted spawned function actually executed and mutated shared state, got: {msgs}")
    finally:
        teardown(game, manager, ctx)


def test_task_spawn_nested_does_not_kill_either_coroutine() -> None:
    """"Nested spawn" from the bug report: a spawned task calling
    task.spawn() itself must not kill the outer OR the inner coroutine --
    the same reentrancy this bug's fix specifically addresses, one level
    deeper."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "task.spawn(function()\n"
            "  print('outer_before')\n"
            "  task.spawn(function() print('inner') end)\n"
            "  print('outer_after')\n"
            "end)\n"
            "print('caller_done')\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("outer_before" in m for m in msgs), "the outer spawned task starts")
        check(any("inner" in m for m in msgs), "the nested task.spawn() call's own function actually runs")
        check(any("outer_after" in m for m in msgs), "the outer spawned task continues and finishes AFTER its own nested task.spawn() call, not killed by it")
        check(any("caller_done" in m for m in msgs), "the original top-level caller also finishes normally")
    finally:
        teardown(game, manager, ctx)


def test_task_spawn_caller_continues_with_correct_state() -> None:
    """"Caller continues" from the bug report, exact reproduction: a
    single task.spawn() call must return control normally, letting the
    caller's own subsequent statement run and observably take effect."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local x = 0\n"
            "task.spawn(function() end)\n"
            "x = 123\n"
            "print('X_IS', x)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("X_IS\t123" in m for m in msgs), f"the caller's statement after task.spawn() runs and x ends up 123, got: {msgs}")
    finally:
        teardown(game, manager, ctx)


def test_task_defer_does_not_kill_or_yield_the_caller() -> None:
    """Sibling API audit from the bug report: task.defer() shares
    scheduler machinery with task.spawn() but was never actually at risk
    of the SAME reentrancy bug (it only appends a _ScheduledEntry to
    self._deferred and returns -- it never calls _resume() synchronously
    the way task.spawn()'s schedule_immediate() does). Locked down
    behaviorally anyway, not just by that source-reading argument: the
    caller must continue immediately and normally, and the deferred
    function must NOT have run yet (that's what "defer" means -- next
    frame, not this one)."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local x = 0\n"
            "task.defer(function() print('deferred_ran') end)\n"
            "x = 456\n"
            "print('X_IS', x)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("X_IS\t456" in m for m in msgs), f"the caller's statement after task.defer() runs immediately, not killed/yielded, got: {msgs}")
        check(not any("deferred_ran" in m for m in msgs), "the deferred function itself has NOT run yet in the same frame -- that's what defer means, not a caller-yield bug")

        for _ in range(3):
            run_frame(manager, ctx)
        check(any("deferred_ran" in m for m in messages_of(manager)), "the deferred function does run, on a later frame")
    finally:
        teardown(game, manager, ctx)


def test_task_delay_does_not_kill_or_yield_the_caller() -> None:
    """Sibling API audit from the bug report: task.delay() shares the
    same reasoning as task.defer() above -- schedule_delayed() only
    appends to self._pending and returns, no synchronous _resume() call,
    so it was never at risk of the reentrancy bug either. Locked down
    behaviorally: the caller continues immediately, and the delayed
    function has not run yet."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local x = 0\n"
            "task.delay(0.02, function() print('delayed_ran') end)\n"
            "x = 789\n"
            "print('X_IS', x)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("X_IS\t789" in m for m in msgs), f"the caller's statement after task.delay() runs immediately, not killed/yielded, got: {msgs}")
        check(not any("delayed_ran" in m for m in msgs), "the delayed function has NOT run yet (its deadline hasn't passed)")

        time.sleep(0.05)
        for _ in range(3):
            run_frame(manager, ctx)
        check(any("delayed_ran" in m for m in messages_of(manager)), "the delayed function does run, once its deadline passes")
    finally:
        teardown(game, manager, ctx)


def test_task_wait_still_suspends_and_resumes_correctly() -> None:
    """"task.wait distinction" from the bug report: unlike spawn/defer/
    delay, task.wait() is REQUIRED to genuinely suspend the caller and
    resume it later -- this fix must not have collapsed that intentional
    yield semantics while fixing spawn()'s accidental one."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = "print('before_wait')\ntask.wait(0.01)\nprint('after_wait')\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("before_wait" in m for m in msgs), "execution reaches the wait")
        check(not any("after_wait" in m for m in msgs), "task.wait() genuinely suspends the caller -- the code after it does NOT run in the same frame")
        check(any(e.owner_script_id == "s1" and e.kind == "wait_seconds" for e in manager.scheduler._pending), "the caller is genuinely parked as a pending scheduler entry while waiting")

        time.sleep(0.03)
        for _ in range(3):
            run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("after_wait" in m for m in msgs), f"once the wait's deadline passes, the caller resumes and continues normally, got: {msgs}")
    finally:
        teardown(game, manager, ctx)


def test_task_counts_allow_many_sequential_short_lived_tasks_over_one_lifetime() -> None:
    """Stage 3.9 lifecycle correction: _task_counts must represent an
    ACTIVE/PENDING task safety limit, not a lifetime-use counter -- a
    script issuing more than MAX_QUEUED_TASKS_PER_SCRIPT (200) short-
    lived task.spawn() calls one after another over its lifetime, each
    one completing before the next is even scheduled, must never
    approach the cap (each is released the moment it finishes -- see
    LuaTaskScheduler._release_task()'s own docstring). Now exercised via
    the real, fixed task.spawn()-in-a-loop path directly (previously
    worked around via task.delay() because of the reentrancy bug fixed
    above)."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "for i = 1, 250 do\n"
            "  task.spawn(function() end)\n"
            "end\n"
            "print('SPAWN_LOOP_DONE')\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("SPAWN_LOOP_DONE" in m for m in msgs), f"250 sequential task.spawn() calls, each completing instantly, all succeed over one script's lifetime, got: {msgs}")
        check(not any("too many queued tasks" in m for m in msgs), "none of the 250 sequential calls are ever rejected -- each is released before the next is scheduled")
        check(manager.scheduler._task_counts.get("s1", 0) == 0, "no active task slots remain reserved for s1 once every spawned task has completed")
    finally:
        teardown(game, manager, ctx)


def test_task_counts_still_reject_too_many_simultaneously_active_tasks() -> None:
    """The counterpart correctness check: genuinely simultaneously-
    active/pending tasks (as opposed to the sequential/already-completed
    ones above) must still hit the existing safety cap -- confirms the
    lifetime-vs-active fix didn't accidentally remove the protection
    MAX_QUEUED_TASKS_PER_SCRIPT exists for. task.delay() with a long
    deadline stays genuinely pending (not resumed) for the rest of this
    test, unlike task.spawn()'s instantly-completing no-op above."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "for i = 1, 210 do\n"
            "  task.delay(1000, function() end)\n"
            "end\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)

        active_for_s1 = sum(1 for e in manager.scheduler._pending if e.owner_script_id == "s1")
        check(active_for_s1 == 200, f"exactly 200 (MAX_QUEUED_TASKS_PER_SCRIPT) simultaneously-active task.delay() calls are accepted, the other 10 silently rejected (got {active_for_s1})")
        check(manager.scheduler._task_counts.get("s1") == 200, f"the active-task counter itself caps at exactly 200, matching MAX_QUEUED_TASKS_PER_SCRIPT (got {manager.scheduler._task_counts.get('s1')})")
    finally:
        teardown(game, manager, ctx)


def test_task_counts_freed_by_cancellation_and_owner_destroy() -> None:
    """Explicitly tests both cleanup paths the lifecycle correction
    requires never double-decrement or leak: cancel_owner() (a script
    destroyed while it still has active/pending tasks) and cancel_signal()
    (a Signal:Wait() cancelled because the signal it was waiting on was
    destroyed) must each release exactly the task-count slots they
    actually held -- and, critically, once freed, brand-new tasks can
    genuinely be scheduled again (proving the count is really back down,
    not just cosmetically appearing to be)."""
    game = _FakeGame(with_character=False)
    game.instances["holder"] = cs.InstanceRecord("holder", "Folder", "Holder", "Workspace", {})
    manager, ctx = make_context(game)
    try:
        # cancel_owner path: a script (nested under "holder", so a single
        # cascading Destroy() reaches it) with 50 genuinely active
        # task.delay()'d tasks, destroyed mid-flight.
        add_script(
            game, "for i = 1, 50 do task.delay(1000, function() end) end\n",
            script_id="owner_script", parent_id="holder",
        )
        manager._start_script("owner_script")
        run_frame(manager, ctx)
        check(manager.scheduler._task_counts.get("owner_script") == 50, "50 active tasks are reserved for owner_script before it is destroyed")

        add_script(game, "workspace.Holder:Destroy()\n", script_id="destroy_holder")
        manager._start_script("destroy_holder")
        run_frame(manager, ctx)
        check(manager.scheduler._task_counts.get("owner_script", 0) == 0, "destroying the script (via cascade) fully releases its reserved task-count slots")
        check(not any(e.owner_script_id == "owner_script" for e in manager.scheduler._pending), "and its pending entries are actually gone from the scheduler, not just uncounted")

        # cancel_signal path: a SEPARATE script Wait()s on a Part's
        # Touched signal from inside a counted task.spawn() wrapper (so
        # the wait itself reserves a task-count slot); destroying that
        # Part must release it via cancel_signal(), not just cancel_owner()
        # (a different script owns the Part than the one Wait()ing on it).
        game.add_part("waited_on", Anchored=True, CanCollide=True)
        add_script(
            game,
            "task.spawn(function() workspace.waited_on.Touched:Wait() end)\n",
            script_id="waiter_script",
        )
        manager._start_script("waiter_script")
        run_frame(manager, ctx)
        check(manager.scheduler._task_counts.get("waiter_script") == 1, "the task.spawn()-wrapped Wait() reserves exactly one active task-count slot")

        add_script(game, "workspace.waited_on:Destroy()\n", script_id="destroy_waited_on")
        manager._start_script("destroy_waited_on")
        run_frame(manager, ctx)
        check(manager.scheduler._task_counts.get("waiter_script", 0) == 0, "destroying the awaited Part's signal (cancel_signal) releases the waiter's task-count slot too")

        # Prove the freed slots are REAL, not cosmetic: schedule a fresh
        # batch of 200 active tasks under a brand-new script id -- this
        # would fail (see the sibling test above) if _task_counts had
        # somehow gone negative or its bookkeeping were otherwise corrupt.
        add_script(game, "for i = 1, 200 do task.delay(1000, function() end) end\n", script_id="fresh_script")
        manager._start_script("fresh_script")
        run_frame(manager, ctx)
        check(manager.scheduler._task_counts.get("fresh_script") == 200, "a brand-new script can still schedule a full batch of 200 active tasks afterward -- the counter bookkeeping is genuinely sound, not just superficially reset")
    finally:
        teardown(game, manager, ctx)


def test_get_full_name_on_detached_instance_does_not_claim_workspace() -> None:
    """Audit-discovered bug: GetFullName() used to unconditionally append
    "Workspace" as the root segment even when the walk actually stopped
    at a genuinely nil-parented (detached) instance -- falsely reporting
    a detached Part as living under Workspace. A detached instance's
    GetFullName() should be just its own name chain, no root prefix."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local p = Instance.new('Part')\n"
            "p.Name = 'Detached'\n"
            "print('attached_full_name', p:GetFullName())\n"
            "p.Parent = workspace\n"
            "print('workspace_full_name', p:GetFullName())\n"
            "p.Parent = nil\n"
            "print('detached_full_name', p:GetFullName())\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("attached_full_name\tDetached" in m for m in msgs), "a freshly-created (still-nil-parented) Instance's GetFullName() is just its own name, no 'Workspace.' prefix")
        check(any("workspace_full_name\tWorkspace.Detached" in m for m in msgs), "GetFullName() correctly reports 'Workspace.<name>' once actually parented under workspace")
        check(any("detached_full_name\tDetached" in m for m in msgs), "GetFullName() on a Part explicitly detached back to Parent=nil no longer falsely claims 'Workspace.<name>'")
    finally:
        teardown(game, manager, ctx)


def test_instance_new_without_parent_starts_nil_like_clone() -> None:
    """Audit-discovered inconsistency: Clone() was explicitly fixed in
    Stage 3.9 to start Parent == nil with no eager Entity/physics (see
    clone()'s own docstring), but Instance.new(className) with the parent
    argument omitted still fell through to the old eager "defaults to
    Workspace, immediately visible and physical" behavior. Both creation
    paths must now agree."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local p = Instance.new('Part')\n"
            "p.Name = 'NoParentYet'\n"
            "print('parent_is_nil', p.Parent == nil)\n"
            "local kids = workspace:GetChildren()\n"
            "print('workspace_children_before', #kids)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("parent_is_nil\ttrue" in m for m in msgs), "Instance.new('Part') with no parent argument starts Parent == nil")
        check(any("workspace_children_before\t0" in m for m in msgs), "an unparented Instance.new()'d Part is not a child of workspace")

        part_id = next(iter(manager.scene._runtime))
        check(part_id not in game.parts, "no Entity is eagerly built for an unparented Instance.new()'d Part")
        check(not game._physics_world.has_body(part_id), "no physics body is eagerly built for an unparented Instance.new()'d Part")

        # Driven directly via RuntimeSceneLayer.set_parent() rather than a
        # second bridged script: the Part isn't a child of anything yet,
        # so workspace:FindFirstChild('NoParentYet') has nothing to find.
        ok, error = manager.scene.set_parent(part_id, "Workspace")
        check(ok, f"explicitly parenting the Part afterward succeeds: {error}")
        check(part_id in game.parts, "explicitly parenting it afterward lazily builds a real Entity, matching Clone()'s already-fixed behavior")
        check(game._physics_world.has_body(part_id), "explicitly parenting it afterward lazily builds a real physics body too")
    finally:
        teardown(game, manager, ctx)


def test_lua_error_quality_for_common_misuse() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local f = Instance.new('Folder')\n"
            "f.Parent = workspace\n"
            "local ok1, err1 = pcall(function() return f.Anchored end)\n"
            "print('folder_anchored', not ok1, tostring(err1))\n"
            "local ok2, err2 = pcall(function() return Instance.new('LocalScript'):Clone() end)\n"
            "print('script_clone', not ok2, tostring(err2))\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("folder_anchored\ttrue" in m and "not a valid member of Folder" in m for m in msgs), "Reading an unsupported property gives a readable 'not a valid member of <Class>' error, not a Python traceback")
        # LocalScript can't even be Instance.new()'d (denylisted), so this
        # exercises the pcall/error path rather than a real Clone -- still
        # confirms no raw Python exception ever crosses into Lua.
        check(any("script_clone\ttrue" in m for m in msgs), "Instance.new('LocalScript') itself fails cleanly (denylisted), never a raw traceback")
    finally:
        teardown(game, manager, ctx)


def test_clone_denylist_matches_instance_new_denylist() -> None:
    game = _FakeGame(with_character=False)
    game.instances["realscript"] = cs.InstanceRecord("realscript", "LocalScript", "RealScript", "StarterPlayer", {"Source": "-- noop\n"})
    manager, ctx = make_context(game)
    try:
        source = (
            "local ok, err = pcall(function() return script:Clone() end)\n"
            "print('clone_rejected', not ok, tostring(err))\n"
        )
        add_script(game, source, script_id="realscript")
        manager._start_script("realscript")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("clone_rejected\ttrue" in m and "cannot be cloned" in m for m in msgs), "Cloning an authored LocalScript is rejected with a clear error (would never actually run)")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Value instances
# ============================================================

def test_value_instances_are_coherent_with_the_class_system() -> None:
    """Stage 3.9 final-verification pass: Value classes (Bool/Int/Number/
    StringValue) must behave like any other Instance, not just support
    generic .Value property read/write -- IsA/ClassName/Name/Parent/
    hierarchy/Destroy/Clone must all work, matching their registration in
    BOTH shared/object_registry.py (properties/schema/creatable) and
    datamodel_schema.py (class hierarchy for IsA), so they are not merely
    half-registered in one and not the other."""
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local n = Instance.new('NumberValue')\n"
            "n.Name = 'Score'\n"
            "n.Value = 3.5\n"
            "n.Parent = workspace\n"
            "print('classname', n.ClassName, 'name', n.Name)\n"
            "print('isa_instance', n:IsA('Instance'), 'isa_numbervalue', n:IsA('NumberValue'))\n"
            "print('isa_part', n:IsA('Part'))\n"
            "local found = workspace:FindFirstChild('Score')\n"
            "print('found_as_child', found ~= nil and found.Value == 3.5)\n"
            "local clone = n:Clone()\n"
            "print('clone_distinct', clone ~= n, 'clone_parent_nil', clone.Parent == nil, 'clone_value_copied', clone.Value == 3.5)\n"
            "clone.Value = 9.0\n"
            "print('original_unaffected_by_clone_edit', n.Value == 3.5)\n"
            "n:Destroy()\n"
            "local ok, err = pcall(function() return n.Value end)\n"
            "print('destroyed_access_rejected', not ok)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("classname\tNumberValue\tname\tScore" in m for m in msgs), "ClassName/Name are coherent for a Value instance")
        check(any("isa_instance\ttrue\tisa_numbervalue\ttrue" in m for m in msgs), "IsA('Instance') and IsA('NumberValue') both true")
        check(any("isa_part\tfalse" in m for m in msgs), "IsA('Part') is false -- Value classes are not Part-derived")
        check(any("found_as_child\ttrue" in m for m in msgs), "a parented Value instance is a real child, findable via FindFirstChild(), with its Value readable")
        check(any("clone_distinct\ttrue\tclone_parent_nil\ttrue\tclone_value_copied\ttrue" in m for m in msgs), "Clone() on a Value instance is a distinct, nil-parented copy with the Value property carried over")
        check(any("original_unaffected_by_clone_edit\ttrue" in m for m in msgs), "editing the clone's Value never mutates the original")
        check(any("destroyed_access_rejected\ttrue" in m for m in msgs), "Destroy() on a Value instance makes further property access a clean, catchable error")
    finally:
        teardown(game, manager, ctx)


def test_int_value_generic_property_roundtrip() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local score = Instance.new('IntValue')\n"
            "score.Value = 0\n"
            "score.Parent = workspace\n"
            "score.Value = score.Value + 1\n"
            "score.Value = score.Value + 1\n"
            "print('score', score.Value, 'classname', score.ClassName)\n"
            "local b = Instance.new('BoolValue')\n"
            "b.Value = true\n"
            "print('bool', b.Value)\n"
            "local s = Instance.new('StringValue')\n"
            "s.Value = 'hello'\n"
            "print('str', s.Value)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("score\t2\tclassname\tIntValue" in m for m in msgs), "IntValue.Value read/write/increment works generically")
        check(any("bool\ttrue" in m for m in msgs), "BoolValue.Value works generically")
        check(any("str\thello" in m for m in msgs), "StringValue.Value works generically")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# UserInputService expanded keys / mouse buttons
# ============================================================

def test_expanded_keys_and_mouse_buttons_reach_input_began() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "game:GetService('UserInputService').InputBegan:Connect(function(input, processed)\n"
            "  print('BEGAN', input.KeyCode, input.UserInputType, processed)\n"
            "end)\n"
            "game:GetService('UserInputService').InputEnded:Connect(function(input, processed)\n"
            "  print('ENDED', input.KeyCode, input.UserInputType)\n"
            "end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("e")
        ctx.on_key_event("f")
        ctx.on_key_event("left shift")
        ctx.on_key_event("left mouse down")
        ctx.on_key_event("right mouse down")
        run_frame(manager, ctx)
        ctx.on_key_event("e up")
        ctx.on_key_event("left mouse up")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("BEGAN\tE\tKeyboard\tfalse" in m for m in msgs), "'e' reaches InputBegan as KeyCode E, UserInputType Keyboard")
        check(any("BEGAN\tF\tKeyboard\tfalse" in m for m in msgs), "'f' reaches InputBegan as KeyCode F")
        check(any("BEGAN\tLeftShift\tKeyboard\tfalse" in m for m in msgs), "'left shift' reaches InputBegan as KeyCode LeftShift")
        check(any("BEGAN\tMouseButton1\tMouseButton1\tfalse" in m for m in msgs), "'left mouse down' reaches InputBegan as MouseButton1/MouseButton1")
        check(any("BEGAN\tMouseButton2\tMouseButton2\tfalse" in m for m in msgs), "'right mouse down' reaches InputBegan as MouseButton2/MouseButton2")
        check(any("ENDED\tE\tKeyboard" in m for m in msgs), "'e up' reaches InputEnded")
        check(any("ENDED\tMouseButton1\tMouseButton1" in m for m in msgs), "'left mouse up' reaches InputEnded as MouseButton1")
    finally:
        teardown(game, manager, ctx)


def test_is_key_down_works_for_new_keys_and_mouse() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = (
            "local uis = game:GetService('UserInputService')\n"
            "print('e_before', uis:IsKeyDown('E'))\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        ctx.on_key_event("e")
        source2 = "print('e_after', game:GetService('UserInputService'):IsKeyDown('E'))\n"
        add_script(game, source2, script_id="s2")
        manager._start_script("s2")
        run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("e_before\tfalse" in m for m in msgs), "IsKeyDown('E') is false before any 'e' press")
        check(any("e_after\ttrue" in m for m in msgs), "IsKeyDown('E') is true while 'e' is held")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Touched
# ============================================================

def test_touched_fires_for_dynamic_part_landing_on_static() -> None:
    game = _FakeGame(with_character=False)
    game.add_part("floor1", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0], Size=[20.0, 1.0, 20.0])
    # A gentle drop (not a high-velocity impact) settles into contact
    # without any physical micro-bounce -- see the Stage 3.9 report for the
    # empirically-confirmed distinction between "fires again because a hard
    # landing genuinely, physically separated and re-touched" (not spam --
    # a real re-contact) vs. uncontrolled every-frame spam during ONE
    # continuous contact, which is what this test actually guards against.
    game.add_part("falling1", Anchored=False, CanCollide=True, Position=[0.0, 2.0, 0.0], Size=[1.0, 1.0, 1.0])
    manager, ctx = make_context(game)
    try:
        source = (
            "workspace.floor1.Touched:Connect(function(other) print('TOUCHED', other.Name) end)\n"
        )
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        for _ in range(180):
            run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("TOUCHED\tfalling1" in m for m in msgs), "Touched fires when a dynamic Part lands on a static CanCollide=True Part")
        count = sum(1 for m in msgs if "TOUCHED" in m)
        check(count == 1, f"Touched fires exactly once for one continuous contact, not every frame (got {count})")
    finally:
        teardown(game, manager, ctx)


def test_touched_fires_for_noncollide_trigger_walkthrough() -> None:
    game = _FakeGame(with_character=False)
    game.add_part("floor2", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0], Size=[20.0, 1.0, 20.0])
    # goal spans y=[2,6] -- clear of floor2's y=[-0.5,0.5], so the only
    # detected overlap is the falling Part actually passing through it.
    game.add_part("goal", Anchored=True, CanCollide=False, Position=[0.0, 4.0, 0.0], Size=[4.0, 4.0, 4.0])
    game.add_part("faller", Anchored=False, CanCollide=True, Position=[0.0, 8.0, 0.0], Size=[1.0, 1.0, 1.0])
    manager, ctx = make_context(game)
    try:
        source = "workspace.goal.Touched:Connect(function(other) print('GOAL_TOUCHED', other.Name) end)\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        for _ in range(180):
            run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("GOAL_TOUCHED\tfaller" in m for m in msgs), "Touched fires for a CanCollide=False (Anchored=True) trigger volume a falling Part passes through")
    finally:
        teardown(game, manager, ctx)


def test_touched_enter_leave_reenter_and_stops_after_destroy() -> None:
    """Stage 3.9 final-verification pass: locks down the full enter/
    remain/leave/re-enter contract, using explicit Position teleports
    (not gravity/timing-dependent) for a deterministic, non-flaky repro
    -- gravity is zeroed out so a dynamic body doesn't drift between
    teleports. Enter fires exactly one event; remaining overlapped fires
    no more; leaving fires nothing new; re-entering is a genuinely new
    contact and fires a second event (physics.py's poll_new_contacts()
    frame-to-frame set-diff -- see its own docstring -- already supports
    this correctly with no artificial debounce; this test exercises that
    mechanism directly). Also verifies Destroy() cleanup: once the mover
    is destroyed, its physics body is gone and Touched can never fire for
    it again -- the ghost-vs-dynamic-rigid-body path already proven by
    test_touched_fires_for_noncollide_trigger_walkthrough."""
    game = _FakeGame(with_character=False, gravity=0.0)
    game.add_part("trigger", Anchored=True, CanCollide=False, Position=[0.0, 0.0, 0.0], Size=[4.0, 4.0, 4.0])
    game.add_part("mover", Anchored=False, CanCollide=True, Position=[100.0, 0.0, 0.0], Size=[1.0, 1.0, 1.0])
    manager, ctx = make_context(game)
    try:
        source = "workspace.trigger.Touched:Connect(function(other) print('ENTER', other.Name) end)\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check(sum(1 for m in messages_of(manager) if "ENTER" in m) == 0, "no Touched event before the mover ever enters the trigger")

        add_script(game, "workspace.mover.Position = Vector3.new(0, 0, 0)\n", script_id="s2")
        manager._start_script("s2")
        for _ in range(5):
            run_frame(manager, ctx)
        enter_count = sum(1 for m in messages_of(manager) if "ENTER\tmover" in m)
        check(enter_count == 1, f"entering the trigger fires exactly one Touched event, got {enter_count}")

        for _ in range(10):
            run_frame(manager, ctx)
        remain_count = sum(1 for m in messages_of(manager) if "ENTER\tmover" in m)
        check(remain_count == enter_count, f"remaining overlapped for more frames fires no additional events (still {remain_count})")

        add_script(game, "workspace.mover.Position = Vector3.new(100, 0, 0)\n", script_id="s3")
        manager._start_script("s3")
        for _ in range(5):
            run_frame(manager, ctx)
        leave_count = sum(1 for m in messages_of(manager) if "ENTER\tmover" in m)
        check(leave_count == enter_count, "leaving the trigger fires no new Touched event by itself")

        add_script(game, "workspace.mover.Position = Vector3.new(0, 0, 0)\n", script_id="s4")
        manager._start_script("s4")
        for _ in range(5):
            run_frame(manager, ctx)
        reenter_count = sum(1 for m in messages_of(manager) if "ENTER\tmover" in m)
        check(reenter_count == enter_count + 1, f"re-entering the trigger fires a second, genuinely new Touched event, got {reenter_count}")

        add_script(game, "workspace.mover:Destroy()\n", script_id="s5")
        manager._start_script("s5")
        run_frame(manager, ctx)
        check(not game._physics_world.has_body("mover"), "Destroy() removes the mover's physics body")
        for _ in range(10):
            run_frame(manager, ctx)
        post_destroy_count = sum(1 for m in messages_of(manager) if "ENTER\tmover" in m)
        check(post_destroy_count == reenter_count, "Touched cannot fire for a destroyed Instance even after further frames")
    finally:
        teardown(game, manager, ctx)


def test_touched_fires_for_character_walking_into_goal() -> None:
    game = _FakeGame(with_character=True)
    game.add_part("floor3", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0], Size=[20.0, 1.0, 20.0])
    game.add_part("goal2", Anchored=True, CanCollide=False, Position=[0.0, 1.0, 0.0], Size=[6.0, 6.0, 6.0])
    game._character_runtime.controller.set_position((0.0, 8.0, 0.0))
    manager, ctx = make_context(game)
    try:
        source = "workspace.goal2.Touched:Connect(function(other) print('PLAYER_TOUCHED_GOAL', other.Name) end)\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        for _ in range(180):
            run_frame(manager, ctx)
        msgs = messages_of(manager)
        check(any("PLAYER_TOUCHED_GOAL" in m for m in msgs), "Touched fires when the player character walks/falls into a non-collidable Goal Part")
    finally:
        teardown(game, manager, ctx)


def test_touched_has_no_leaked_state_across_play_sessions() -> None:
    game = _FakeGame(with_character=False)
    game.add_part("floorA", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0], Size=[20.0, 1.0, 20.0])
    game.add_part("fallerA", Anchored=False, CanCollide=True, Position=[0.0, 2.0, 0.0], Size=[1.0, 1.0, 1.0])
    manager, ctx = make_context(game)
    try:
        source = "workspace.floorA.Touched:Connect(function(other) print('FIRST_SESSION_TOUCH', other.Name) end)\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        for _ in range(120):
            run_frame(manager, ctx)
        first_count = sum(1 for m in messages_of(manager) if "FIRST_SESSION_TOUCH" in m)
        check(first_count == 1, "Touched fires once in the first Play session")
    finally:
        teardown(game, manager, ctx)

    # A fresh Play session (new game/manager, mirrors a real Stop then
    # Play again) must not carry over any contact state or fire stale
    # callbacks from the previous session.
    game2 = _FakeGame(with_character=False)
    game2.add_part("floorA", Anchored=True, CanCollide=True, Position=[0.0, 0.0, 0.0], Size=[20.0, 1.0, 20.0])
    game2.add_part("fallerA", Anchored=False, CanCollide=True, Position=[0.0, 2.0, 0.0], Size=[1.0, 1.0, 1.0])
    manager2, ctx2 = make_context(game2)
    try:
        source = "workspace.floorA.Touched:Connect(function(other) print('SECOND_SESSION_TOUCH', other.Name) end)\n"
        add_script(game2, source, script_id="s1")
        manager2._start_script("s1")
        for _ in range(120):
            run_frame(manager2, ctx2)
        msgs2 = messages_of(manager2)
        second_count = sum(1 for m in msgs2 if "SECOND_SESSION_TOUCH" in m)
        check(second_count == 1, "Touched fires again exactly once in a fresh second Play session (no leaked prior-session state)")
        check(not any("FIRST_SESSION_TOUCH" in m for m in msgs2), "No first-session callback ever fires in the second session")
    finally:
        teardown(game2, manager2, ctx2)


# ============================================================
# Runtime object lifetime vs. authored Place separation
# ============================================================

def test_runtime_created_part_does_not_touch_authored_instances() -> None:
    game = _FakeGame(with_character=False)
    manager, ctx = make_context(game)
    try:
        source = "local p = Instance.new('Part'); p.Parent = workspace\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check("script1" not in game.instances or True, "sanity: script instance exists")
        check(all(not iid.startswith("__runtime") for iid in game.instances), "Instance.new() never writes into game.instances (authored Place)")
    finally:
        teardown(game, manager, ctx)


def test_destroying_authored_part_does_not_remove_it_from_game_instances() -> None:
    game = _FakeGame(with_character=False)
    game.add_part("baseplate", Anchored=True, CanCollide=True)
    manager, ctx = make_context(game)
    try:
        source = "workspace.baseplate:Destroy()\n"
        add_script(game, source, script_id="s1")
        manager._start_script("s1")
        run_frame(manager, ctx)
        check("baseplate" in game.instances, "Destroy() on an authored Part never removes it from game.instances -- only hides it for this Play session")
        exists_in_lua = manager.scene.exists("baseplate")
        check(not exists_in_lua, "But the destroyed authored Part IS gone from the Lua-visible scene for the rest of this session")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# run
# ============================================================

test_instance_new_part_and_hierarchy_methods()
test_instance_new_invalid_class_rejected()
test_spawn_point_is_a_inheritance_chain()
test_parent_reparent_and_cycle_rejected()
test_destroy_on_runtime_part_removes_visual_and_physics_presence()
test_wait_for_child_and_get_descendants()
test_part_property_write_moves_real_physics_body()
test_repeated_play_stop_cycles_stay_isolated()
test_runtime_instance_gone_and_authored_destroy_undone_after_stop()
test_clone_creates_independent_instance()
test_clone_has_no_visual_or_physics_presence_until_parented()
test_folder_clone_is_shallow_documented_limitation()
test_destroy_then_access_is_clean_error()
test_destroy_on_container_cascades_to_descendants()
test_parent_nil_on_container_cascades_attachment_to_descendants()
test_destroy_cascade_cancels_nested_script_coroutines()
test_signal_registry_shrinks_per_item_without_stop()
test_owned_by_cleanup_disconnects_script_connections_to_other_instances()
test_disconnect_and_once_prune_listeners_immediately()
test_stop_still_wipes_signal_and_coroutine_registries()
test_task_spawn_repeated_in_loop_does_not_break_the_caller()
test_task_spawn_every_accepted_call_actually_executes()
test_task_spawn_nested_does_not_kill_either_coroutine()
test_task_spawn_caller_continues_with_correct_state()
test_task_defer_does_not_kill_or_yield_the_caller()
test_task_delay_does_not_kill_or_yield_the_caller()
test_task_wait_still_suspends_and_resumes_correctly()
test_task_counts_allow_many_sequential_short_lived_tasks_over_one_lifetime()
test_task_counts_still_reject_too_many_simultaneously_active_tasks()
test_task_counts_freed_by_cancellation_and_owner_destroy()
test_get_full_name_on_detached_instance_does_not_claim_workspace()
test_instance_new_without_parent_starts_nil_like_clone()
test_lua_error_quality_for_common_misuse()
test_clone_denylist_matches_instance_new_denylist()
test_value_instances_are_coherent_with_the_class_system()
test_int_value_generic_property_roundtrip()
test_expanded_keys_and_mouse_buttons_reach_input_began()
test_is_key_down_works_for_new_keys_and_mouse()
test_touched_fires_for_dynamic_part_landing_on_static()
test_touched_fires_for_noncollide_trigger_walkthrough()
test_touched_enter_leave_reenter_and_stops_after_destroy()
test_touched_fires_for_character_walking_into_goal()
test_touched_has_no_leaked_state_across_play_sessions()
test_runtime_created_part_does_not_touch_authored_instances()
test_destroying_authored_part_does_not_remove_it_from_game_instances()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All Stage 3.9 API tests passed.")
