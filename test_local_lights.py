"""Regression tests for Stage 4.1's local lighting foundation: PointLight
and SpotLight as real spatial Instances (replacing the old "editor-only
placeholder -- not implemented yet" registration), plus the Environment
ambient authoring these tests also cover indirectly through the same
world-membership/lifecycle machinery every other has_3d_entity class uses.

Scope, matching the sprint spec: registration/schema, property validation,
save/open, Inspector metadata, Lua get/set, Workspace attachment/reparent,
Clone, Destroy, repeated Play/Stop (no duplicate light nodes), and --
critically -- that lights NEVER get a Bullet physics body. Does not assert
anything about how the light visually looks; that is manual acceptance.
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
from shared.object_registry import LIGHT_CLASS_NAMES

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


class _FakeGame:
    _build_part_entity = cs.MultiplayerGame._build_part_entity
    _build_light_entity = cs.MultiplayerGame._build_light_entity
    _apply_light_properties = cs.MultiplayerGame._apply_light_properties
    _apply_part_surface = cs.MultiplayerGame._apply_part_surface
    _apply_mesh_geometry = cs.MultiplayerGame._apply_mesh_geometry
    _apply_instance_properties = cs.MultiplayerGame._apply_instance_properties
    _set_light_render_active = cs.MultiplayerGame._set_light_render_active
    remove_instance = cs.MultiplayerGame.remove_instance
    spawn_instance = cs.MultiplayerGame.spawn_instance

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
        self.studio_playing = False
        self.applied_service_writes: list[tuple[str, dict]] = []
        self._physics_world = physics.PhysicsWorld(gravity=-24.0)
        # Blocker-fix regression harness additions: remove_instance() (the
        # real client_studio.py editor/Explorer/Place-switch deletion
        # path) also touches these.
        self.selected_part_id: str | None = None
        self._physics_snapshot: dict = {}

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
    return runtime_id in game.parts and game.parts[runtime_id].enabled


def count_active_render_lights() -> int:
    """Blocker-fix regression helper: counts lights CURRENTLY registered
    on Panda3D's real render.setLight()/LightAttrib -- the actual thing
    that determines whether a light illuminates the scene, independent
    of game.parts/game.instances bookkeeping. This is the ground truth
    the ghost-light bug investigation was missing: every pre-existing
    test in this file only ever checked game.parts (Python/DataModel
    state), never this. Includes the engine's own AmbientLight/
    DirectionalLight (self.ambient_light/self.sun in create_world()) --
    not created by this test harness, so callers should diff against a
    baseline taken before their own PointLight/SpotLight instances
    existed, not assume an absolute count."""
    from panda3d.core import LightAttrib
    from ursina import application
    attrib = application.base.render.node().getAttrib(LightAttrib)
    return 0 if attrib is None else attrib.getNumOnLights()


# ============================================================
# Registration / schema
# ============================================================

def test_lights_are_registered_and_no_longer_placeholders() -> None:
    for class_name in ("PointLight", "SpotLight"):
        definition = cs.object_registry.get_object_type(class_name)
        check(definition is not None, f"{class_name} is registered in object_registry")
        check(definition.has_3d_entity, f"{class_name}.has_3d_entity is True")
        check(not definition.editor_only, f"{class_name} is no longer editor_only (real Instance, not a placeholder)")
        check(definition.creatable, f"{class_name} is creatable")
        check(class_name in LIGHT_CLASS_NAMES, f"{class_name} is in LIGHT_CLASS_NAMES")
        check(datamodel_schema.is_a(class_name, "Instance"), f"IsA('Instance') is true for {class_name}")

    point_props = cs.object_registry.get_object_type("PointLight").property_schema
    for expected in ("Position", "Color", "Intensity", "Range"):
        check(expected in point_props, f"PointLight declares {expected}")
    check("Rotation" not in point_props, "PointLight has no Rotation (no look-vector concept)")
    check("Angle" not in point_props, "PointLight has no Angle (that's SpotLight-only)")

    spot_props = cs.object_registry.get_object_type("SpotLight").property_schema
    for expected in ("Position", "Rotation", "Color", "Intensity", "Range", "Angle"):
        check(expected in spot_props, f"SpotLight declares {expected}")

    check(cs.object_registry.get_object_type("PointLight").inspector_sections == ("light",), "PointLight Inspector shows the light section")
    check(cs.object_registry.get_object_type("SpotLight").inspector_sections == ("light",), "SpotLight Inspector shows the light section")


# ============================================================
# Property validation
# ============================================================

def test_property_validation() -> None:
    from shared.object_registry import sanitize_properties_for_type
    clean = sanitize_properties_for_type("PointLight", {"Intensity": 2.5, "Range": 12.0, "Color": [10, 20, 30], "Position": [1, 2, 3]})
    check(clean.get("Intensity") == 2.5, "PointLight Intensity sanitizes as a float")
    check(clean.get("Range") == 12.0, "PointLight Range sanitizes as a float")
    check(clean.get("Color") == [10, 20, 30], "PointLight Color sanitizes")
    check(clean.get("Position") == [1.0, 2.0, 3.0], "PointLight Position sanitizes")

    clean_spot = sanitize_properties_for_type("SpotLight", {"Angle": 60.0, "Rotation": [0, 45, 0]})
    check(clean_spot.get("Angle") == 60.0, "SpotLight Angle sanitizes as a float")
    check(clean_spot.get("Rotation") == [0.0, 45.0, 0.0], "SpotLight Rotation sanitizes")


# ============================================================
# Save / open
# ============================================================

def test_lights_persist_through_save_open_round_trip() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "lights_roundtrip.nebula.json")
        objects = [
            {
                "id": "pl1", "class_name": "PointLight", "name": "TestPointLight", "parent_id": "Workspace",
                "properties": {"Position": [1.0, 2.0, 3.0], "Color": [255, 200, 150], "Intensity": 2.0, "Range": 15.0},
                "tags": [], "attributes": {}, "enabled": True,
            },
            {
                "id": "sl1", "class_name": "SpotLight", "name": "TestSpotLight", "parent_id": "Workspace",
                "properties": {"Position": [0.0, 4.0, 0.0], "Rotation": [90.0, 0.0, 0.0], "Color": [255, 255, 255], "Intensity": 1.5, "Range": 20.0, "Angle": 30.0},
                "tags": [], "attributes": {}, "enabled": True,
            },
        ]
        manager = pm.PlaceManager()
        save_result = manager.save_as(path, objects, datamodel_schema.sanitize_services_snapshot(None))
        check(save_result.success, f"saving a Place with PointLight+SpotLight succeeds: {save_result.message}")

        reopened = pm.PlaceManager()
        open_result = reopened.open(path)
        check(open_result.success, f"reopening succeeds: {open_result.message}")
        loaded = {o["class_name"]: o for o in (open_result.objects or [])}
        check("PointLight" in loaded and "SpotLight" in loaded, "both lights round-trip")
        if "PointLight" in loaded:
            props = loaded["PointLight"]["properties"]
            check(props.get("Intensity") == 2.0 and props.get("Range") == 15.0, "PointLight Intensity/Range survive save/open")
        if "SpotLight" in loaded:
            props = loaded["SpotLight"]["properties"]
            check(props.get("Angle") == 30.0 and props.get("Rotation") == [90.0, 0.0, 0.0], "SpotLight Angle/Rotation survive save/open")


# ============================================================
# Lua get/set
# ============================================================

def test_lua_create_and_property_get_set() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("PointLight", "Workspace")
        check(ok, f"Instance.new('PointLight', workspace) succeeds: {pid}")

        kind, value = scene.get_property(pid, "Intensity")
        check(kind == "number" and value == 1.0, "PointLight.Intensity reads back its default")
        kind, value = scene.get_property(pid, "Range")
        check(kind == "number" and value == 8.0, "PointLight.Range reads back its default")

        ok, err = scene.set_property(pid, "Intensity", 3.0)
        check(ok, f"PointLight.Intensity = 3.0 succeeds: {err}")
        ok, err = scene.set_property(pid, "Range", 25.0)
        check(ok, f"PointLight.Range = 25.0 succeeds: {err}")
        kind, value = scene.get_property(pid, "Intensity")
        check(value == 3.0, "Intensity write is reflected on read")

        ok, spot_id = scene.instance_new("SpotLight", "Workspace")
        check(ok, f"Instance.new('SpotLight', workspace) succeeds: {spot_id}")
        ok, err = scene.set_property(spot_id, "Angle", 60.0)
        check(ok, f"SpotLight.Angle = 60.0 succeeds: {err}")
        kind, value = scene.get_property(spot_id, "Angle")
        check(kind == "number" and value == 60.0, "Angle write/read round-trips")
    finally:
        teardown(game, manager, ctx)


def test_lua_write_rejects_bad_types() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("PointLight", "Workspace")
        assert ok, pid
        ok, err = scene.set_property(pid, "Position", "not a vector")
        check(not ok, "a non-vector Position write is rejected")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Workspace attach / reparent / world-membership
# ============================================================

def test_light_world_membership() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("PointLight", None)
        assert ok, pid
        check(not is_active(game, pid), "an unparented PointLight has no Entity yet (lazy build)")

        ok, err = scene.set_parent(pid, "Workspace")
        check(ok, f"parenting into Workspace succeeds: {err}")
        check(is_active(game, pid), "...and it becomes active (Entity built, enabled)")
        check(game.parts[pid]._light is not None, "...with a real Panda3D light node")

        ok, folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        ok, err = scene.set_parent(pid, folder)
        check(ok, f"reparenting out of Workspace succeeds: {err}")
        check(not is_active(game, pid), "...and it becomes inactive (disabled, not destroyed)")
        check(scene.exists(pid), "...the Instance itself still exists")

        ok, err = scene.set_parent(pid, "Workspace")
        check(ok, f"reparenting back into Workspace succeeds: {err}")
        check(is_active(game, pid), "...and it reactivates")
    finally:
        teardown(game, manager, ctx)


def test_light_never_gets_a_physics_body() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("PointLight", "Workspace")
        assert ok, pid
        check(not game._physics_world.has_body(pid), "a Workspace-attached PointLight has NO physics body")

        ok, err = scene.set_property(pid, "Position", [5.0, 5.0, 5.0])
        check(ok, f"moving the light succeeds: {err}")
        check(not game._physics_world.has_body(pid), "...and moving it still does not create a physics body")

        ok, spot_id = scene.instance_new("SpotLight", "Workspace")
        assert ok, spot_id
        check(not game._physics_world.has_body(spot_id), "a Workspace-attached SpotLight has NO physics body either")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Clone / Destroy
# ============================================================

def test_clone_preserves_light_properties() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, original = scene.instance_new("PointLight", "Workspace")
        assert ok, original
        scene.set_property(original, "Intensity", 4.0)
        scene.set_property(original, "Range", 30.0)

        ok, clone_id = scene.clone(original)
        check(ok, f"cloning a PointLight succeeds: {clone_id}")
        check(clone_id != original, "the clone is a new, independent instance id")
        kind, value = scene.get_property(clone_id, "Intensity")
        check(kind == "number" and value == 4.0, "the clone's Intensity matches the original")
        check(not is_active(game, clone_id), "a fresh clone has no Entity yet (starts Parent == nil)")

        ok, err = scene.set_parent(clone_id, "Workspace")
        check(ok, f"parenting the clone into Workspace succeeds: {err}")
        check(is_active(game, clone_id), "...and now it has a real Entity")
        check(game.parts[clone_id] is not game.parts[original], "clone and original have distinct Entities")
    finally:
        teardown(game, manager, ctx)


def test_destroy_cleans_up_light_entity_and_marker() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        baseline = count_active_render_lights()
        ok, pid = scene.instance_new("PointLight", "Workspace")
        assert ok, pid
        check(pid in game.parts, "PointLight is active before Destroy()")
        check(count_active_render_lights() == baseline + 1, "...and is actually registered on Panda3D's render (not just game.parts)")

        scene.destroy(pid)
        check(not scene.exists(pid), "Destroy() removes the Instance")
        check(pid not in game.parts, "...and its Entity")
        check(not game._physics_world.has_body(pid), "...confirmed still no physics body")
        check(count_active_render_lights() == baseline, "BLOCKER FIX: the real Panda3D light is also cleared from render -- it no longer illuminates the scene as a ghost light with no corresponding Instance")
    finally:
        teardown(game, manager, ctx)


def test_repeated_play_stop_does_not_leak_light_nodes() -> None:
    """Performance-discipline check from the sprint spec: repeated create/
    destroy cycles (the same thing a Play session's world teardown does at
    scale) must not accumulate stale light Entities/Panda light nodes --
    and, after the blocker fix, must not accumulate real active render
    lights either (the actual bug: game.parts was always correctly
    empty, but render's LightAttrib grew by 2 every cycle before the
    fix)."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        baseline = count_active_render_lights()
        for _ in range(5):
            ok, pid = scene.instance_new("PointLight", "Workspace")
            assert ok, pid
            ok, spot_id = scene.instance_new("SpotLight", "Workspace")
            assert ok, spot_id
            scene.destroy(pid)
            scene.destroy(spot_id)
        check(len(game.parts) == 0, "no stale light Entities remain after 5 create/destroy cycles")
        check(count_active_render_lights() == baseline, "BLOCKER FIX: no accumulating illumination -- render's real active-light count returns exactly to baseline, not baseline+10")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Blocker fix: ghost/non-DataModel lights (found during Stage 4.3D
# packaged manual acceptance) -- render.setLight() self-registration
# was never matched by a render.clearLight() anywhere Ursina's generic
# destroy()/entity.enabled=False touch a PointLight/SpotLight. Every
# test below asserts against the REAL Panda3D render light count, not
# just game.parts/game.instances (which were always correct -- that's
# exactly why this went unnoticed: Explorer/DataModel genuinely showed
# zero lights while the renderer kept an orphaned one active forever).
# ============================================================

def test_remove_instance_editor_delete_path_clears_the_real_light() -> None:
    """The client_studio.py editor/Explorer/Place-switch authoritative
    deletion path (remove_instance()) -- DIFFERENT code path from Lua
    :Destroy() above (scene.destroy()), and the one load_world_snapshot()
    itself calls for every stale instance when switching Places."""
    game = _FakeGame()
    try:
        baseline = count_active_render_lights()
        entity = game._build_light_entity("PointLight", {
            "Position": [0, 3, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0,
        })
        game.parts["diagnostic_light"] = entity
        game.instances["diagnostic_light"] = cs.InstanceRecord("diagnostic_light", "PointLight", "Light", "Workspace", {}, True)
        check(count_active_render_lights() == baseline + 1, "light is registered on render after being built")

        game.remove_instance("diagnostic_light")
        check("diagnostic_light" not in game.parts, "remove_instance() removes the Entity")
        check(count_active_render_lights() == baseline, "BLOCKER FIX: remove_instance() (the Place-switch/Explorer-delete path) also clears the real render light")
    finally:
        game.teardown()


def test_place_switch_removing_multiple_lights_leaves_zero_active() -> None:
    """Simulates exactly what load_world_snapshot() does when switching
    from a Place with local lights (e.g. rendering_showcase_v2) to one
    with none (e.g. transparency_showcase before this fix) -- the
    reported bug's actual real-world trigger."""
    game = _FakeGame()
    try:
        baseline = count_active_render_lights()
        light_ids = []
        for i in range(3):
            light_id = f"light_{i}"
            entity = game._build_light_entity("PointLight" if i % 2 == 0 else "SpotLight", {
                "Position": [float(i), 3, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0, "Rotation": [90, 0, 0], "Angle": 45.0,
            })
            game.parts[light_id] = entity
            game.instances[light_id] = cs.InstanceRecord(light_id, "PointLight", "Light", "Workspace", {}, True)
            light_ids.append(light_id)
        check(count_active_render_lights() == baseline + 3, "3 lights registered, matching a Place like rendering_showcase_v2")

        # load_world_snapshot()'s own stale-removal loop, reproduced
        # directly (it just calls remove_instance() per stale id).
        for light_id in light_ids:
            game.remove_instance(light_id)
        check(len(game.parts) == 0 and len(game.instances) == 0, "switching to a Place with 0 lights leaves 0 DataModel light Instances")
        check(count_active_render_lights() == baseline, "BLOCKER FIX: ...and 0 active render lights -- no ghost illumination from the previous Place")
    finally:
        game.teardown()


def test_workspace_detach_and_reattach_toggles_the_real_render_light() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        baseline = count_active_render_lights()
        ok, pid = scene.instance_new("PointLight", "Workspace")
        assert ok, pid
        check(count_active_render_lights() == baseline + 1, "attached PointLight is registered on render")

        ok, folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        scene.set_parent(pid, folder)
        check(not is_active(game, pid), "detaching disables the Entity")
        check(count_active_render_lights() == baseline, "BLOCKER FIX: detaching from Workspace also clears the real render light -- a stored/non-world-attached light must not illuminate")

        scene.set_parent(pid, "Workspace")
        check(is_active(game, pid), "reattaching re-enables the Entity")
        check(count_active_render_lights() == baseline + 1, "BLOCKER FIX: ...and re-registers the real render light")
    finally:
        teardown(game, manager, ctx)


def test_runtime_light_destroyed_during_play_stops_illuminating() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        baseline = count_active_render_lights()
        ok, pid = scene.instance_new("SpotLight", "Workspace")
        assert ok, pid
        check(count_active_render_lights() == baseline + 1, "a runtime-created SpotLight is registered on render")

        scene.destroy(pid)
        check(count_active_render_lights() == baseline, "BLOCKER FIX: Lua :Destroy() on a runtime light immediately clears the real render light")
    finally:
        teardown(game, manager, ctx)


def test_authored_light_survives_play_stop_exactly_once_in_the_renderer() -> None:
    """Spec requirement: 'Editor authored light -> Play -> Stop -> must
    return to exactly one authored light -- not two.' Checked against
    the real render light count, not just len(game.parts)."""
    game = _FakeGame()
    baseline = count_active_render_lights()
    entity = game._build_light_entity("PointLight", {
        "Position": [0, 3, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0,
    })
    game.parts["authored_light"] = entity
    game.instances["authored_light"] = cs.InstanceRecord("authored_light", "PointLight", "Light", "Workspace", {}, True)
    check(count_active_render_lights() == baseline + 1, "one authored light active before Play")

    manager, ctx = make_context(game)
    try:
        check(count_active_render_lights() == baseline + 1, "still exactly one active render light at Play start (no duplicate registration)")
        manager.scene.destroy("authored_light")
        check(count_active_render_lights() == baseline, "BLOCKER FIX: Lua-side Destroy() of the AUTHORED light clears the real render light mid-session")
    finally:
        teardown(game, manager, ctx)
    check(count_active_render_lights() == baseline + 1, "BLOCKER FIX: Stop restores it to exactly ONE active render light -- not zero (still hidden) and not two (duplicate registration)")


def test_runtime_light_never_explicitly_destroyed_still_cleared_at_stop() -> None:
    """The Stop() loop itself (not a manual scene.destroy() call) is what
    must clear a runtime light that a script simply left alive when Play
    ended -- the most common real case (most scripts don't manually
    clean up every light they spawn)."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    baseline = count_active_render_lights()
    ok, pid = scene.instance_new("PointLight", "Workspace")
    assert ok, pid
    check(count_active_render_lights() == baseline + 1, "runtime light active mid-session")
    ctx.stop()
    manager.stop()
    check(count_active_render_lights() == baseline, "BLOCKER FIX: Stop()'s own runtime-instance cleanup loop clears a never-explicitly-destroyed runtime light's real render registration")
    game.teardown()


def test_real_place_switch_v2_to_transparency_showcase_leaves_no_ghost_lights() -> None:
    """The exact reported reproduction ('Test B' from the blocker
    report): open a real Place with local lights
    (rendering_showcase_v2.nebula.json, 4 real PointLight/SpotLight
    Instances), then switch to transparency_showcase.nebula.json (2
    lights) WITHOUT restarting -- using the actual production
    spawn_instance()/remove_instance() methods (the same ones
    load_world_snapshot() itself calls for every stale/incoming
    instance), not a reimplementation. Proves DataModel count, Explorer-
    equivalent (game.instances) count, and the real render light count
    all agree at every step -- the three-layer disagreement the blocker
    report asked to isolate."""
    import place_manager as pm

    game = _FakeGame()
    try:
        baseline = count_active_render_lights()

        v2 = pm.PlaceManager().open("test_scenes/rendering_showcase_v2.nebula.json")
        assert v2.success, v2.message
        v2_light_ids = [o["id"] for o in v2.objects if o["class_name"] in ("PointLight", "SpotLight")]
        check(len(v2_light_ids) == 4, f"rendering_showcase_v2 has 4 local lights, found {len(v2_light_ids)}")
        for obj in v2.objects:
            game.spawn_instance(obj)
        datamodel_lights_v2 = [i for i in game.instances.values() if i.class_name in ("PointLight", "SpotLight")]
        check(len(datamodel_lights_v2) == 4, f"DataModel (game.instances) shows 4 light Instances after opening v2, got {len(datamodel_lights_v2)}")
        check(count_active_render_lights() == baseline + 4, f"renderer shows 4 active local lights after opening v2, got {count_active_render_lights() - baseline}")

        # Switch to transparency_showcase WITHOUT restarting -- exactly
        # load_world_snapshot()'s own stale-removal + spawn loop.
        transparency = pm.PlaceManager().open("test_scenes/transparency_showcase.nebula.json")
        assert transparency.success, transparency.message
        incoming_ids = {o["id"] for o in transparency.objects}
        for stale_id in list(game.instances):
            if stale_id not in incoming_ids:
                game.remove_instance(stale_id)
        for obj in transparency.objects:
            game.spawn_instance(obj)

        datamodel_lights_after = [i for i in game.instances.values() if i.class_name in ("PointLight", "SpotLight")]
        check(len(datamodel_lights_after) == 2, f"DataModel shows exactly transparency_showcase's own 2 lights (RoomBLight, ShowcaseFixture) after the switch, got {len(datamodel_lights_after)}")
        check(count_active_render_lights() == baseline + 2, f"BLOCKER FIX: renderer ALSO shows exactly 2 active lights -- v2's 4 old lights are gone from render, not ghosted, got {count_active_render_lights() - baseline}")
    finally:
        game.teardown()


def test_repeated_play_stop_of_runtime_lights_leaves_zero_active_lights() -> None:
    """Same 'no accumulating illumination' requirement as the existing
    game.parts-only test above, now proven against the real renderer
    across several full Play/Stop cycles."""
    game = _FakeGame()
    baseline = count_active_render_lights()
    for _ in range(4):
        manager, ctx = make_context(game)
        ok, pid = manager.scene.instance_new("SpotLight", "Workspace")
        assert ok, pid
        ctx.stop()
        manager.stop()
    check(count_active_render_lights() == baseline, "BLOCKER FIX: 4 repeated Play/Stop cycles, each spawning a never-cleaned-up runtime light, leave render's active-light count exactly at baseline")
    game.teardown()


# ============================================================
# Shadow-caster decision: SpotLight yes, PointLight deferred
# ============================================================

def test_spotlight_gets_a_shadow_caster_pointlight_does_not() -> None:
    game = _FakeGame()
    try:
        point_entity = game._build_light_entity("PointLight", {"Position": [0, 0, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0})
        check(not getattr(point_entity, "_shadow_caster_enabled", False), "PointLight does not get a shadow caster (deferred -- would need a 6-face cube map)")

        spot_entity = game._build_light_entity("SpotLight", {"Position": [0, 3, 0], "Rotation": [90, 0, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0, "Angle": 45.0})
        check(getattr(spot_entity, "_shadow_caster_enabled", False), "SpotLight gets a shadow caster (one PerspectiveLens, same shape as DirectionalLight's)")
    finally:
        game.teardown()


def test_spotlight_shadow_caster_is_not_re_enabled_on_repeated_property_apply() -> None:
    """Same buffer-churn guard DirectionalLight already has -- repeated
    property applies (e.g. every Inspector edit, or Play/Stop) with an
    unchanged Angle/Color/Intensity must not repeatedly reallocate the
    SpotLight's shadow buffer. Panda3D's setShadowCaster is a read-only
    bound C++ method (can't be monkeypatched to count calls, same
    limitation test_environment_service.py hit for DirectionalLight) --
    this instead confirms the guard flag itself (_shadow_caster_enabled)
    stays set and repeated applies don't raise, which is what
    _apply_light_properties's own guard condition actually checks."""
    game = _FakeGame()
    try:
        spot_entity = game._build_light_entity("SpotLight", {"Position": [0, 3, 0], "Rotation": [90, 0, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0, "Angle": 45.0})
        check(spot_entity._shadow_caster_enabled is True, "shadow caster guard flag is set after the initial build")
        for _ in range(3):
            game._apply_light_properties(spot_entity, "SpotLight", {"Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0, "Angle": 45.0})
        check(spot_entity._shadow_caster_enabled is True, "repeated identical applies do not clear/rebuild the guard flag")
    finally:
        game.teardown()


# ============================================================
# Editor-only marker visibility (Play/Stop)
# ============================================================

def test_editor_marker_hidden_during_play_shown_after_stop() -> None:
    game = _FakeGame()
    try:
        entity = game._build_light_entity("PointLight", {"Position": [0, 0, 0], "Color": [255, 255, 255], "Intensity": 1.0, "Range": 8.0})
        check(entity is not None, "light entity built directly via _build_light_entity")
        game.parts["diagnostic_light"] = entity  # _set_light_markers_visible iterates self.parts
        marker = getattr(entity, "_editor_marker", None)
        check(marker is not None, "the light carries an editor-only marker child")
        check(marker.enabled, "marker starts visible (not playing)")

        cs.MultiplayerGame._set_light_markers_visible(game, False)
        check(not marker.enabled, "marker is hidden once markers are set invisible (Play start)")
        cs.MultiplayerGame._set_light_markers_visible(game, True)
        check(marker.enabled, "marker is shown again once markers are set visible (Stop)")
    finally:
        game.teardown()


test_lights_are_registered_and_no_longer_placeholders()
test_property_validation()
test_lights_persist_through_save_open_round_trip()
test_lua_create_and_property_get_set()
test_lua_write_rejects_bad_types()
test_light_world_membership()
test_light_never_gets_a_physics_body()
test_clone_preserves_light_properties()
test_destroy_cleans_up_light_entity_and_marker()
test_repeated_play_stop_does_not_leak_light_nodes()
test_remove_instance_editor_delete_path_clears_the_real_light()
test_place_switch_removing_multiple_lights_leaves_zero_active()
test_workspace_detach_and_reattach_toggles_the_real_render_light()
test_runtime_light_destroyed_during_play_stops_illuminating()
test_authored_light_survives_play_stop_exactly_once_in_the_renderer()
test_runtime_light_never_explicitly_destroyed_still_cleared_at_stop()
test_real_place_switch_v2_to_transparency_showcase_leaves_no_ghost_lights()
test_repeated_play_stop_of_runtime_lights_leaves_zero_active_lights()
test_spotlight_gets_a_shadow_caster_pointlight_does_not()
test_spotlight_shadow_caster_is_not_re_enabled_on_repeated_property_apply()
test_editor_marker_hidden_during_play_shown_after_stop()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All local-lighting tests passed.")
