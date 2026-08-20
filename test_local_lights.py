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
        ok, pid = scene.instance_new("PointLight", "Workspace")
        assert ok, pid
        check(pid in game.parts, "PointLight is active before Destroy()")

        scene.destroy(pid)
        check(not scene.exists(pid), "Destroy() removes the Instance")
        check(pid not in game.parts, "...and its Entity")
        check(not game._physics_world.has_body(pid), "...confirmed still no physics body")
    finally:
        teardown(game, manager, ctx)


def test_repeated_play_stop_does_not_leak_light_nodes() -> None:
    """Performance-discipline check from the sprint spec: repeated create/
    destroy cycles (the same thing a Play session's world teardown does at
    scale) must not accumulate stale light Entities/Panda light nodes."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        for _ in range(5):
            ok, pid = scene.instance_new("PointLight", "Workspace")
            assert ok, pid
            ok, spot_id = scene.instance_new("SpotLight", "Workspace")
            assert ok, spot_id
            scene.destroy(pid)
            scene.destroy(spot_id)
        check(len(game.parts) == 0, "no stale light Entities remain after 5 create/destroy cycles")
    finally:
        teardown(game, manager, ctx)


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
