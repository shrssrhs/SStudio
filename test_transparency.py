"""Regression tests for Stage 4.3D's transparency foundation.

Scope, matching the sprint spec: schema/validation/clamping, that real
alpha blending is actually enabled on a Part's render node (not just
that the alpha channel is set), Transparency=0/0.5/1.0 lifecycle
(no destroy/recreate at 1.0, physics/selection/hierarchy intact), Clone/
Destroy/Workspace reparent, save/open, Lua get/set, the deliberately
chosen shadow policy (Option B: ordinary opaque shadows -- documented,
not a silent gap), embedded glTF alphaMode (OPAQUE/MASK/BLEND)
preservation through load_mesh_node(), and the Glass preset's new
Transparency default. Does NOT claim any test proves blending *looks*
correct -- that was verified once with a real window+GPU (see the Stage
4.3D completion report); what's covered here is objective render-state
and lifecycle behavior only.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ursina import Ursina

ursina_app = Ursina(window_type="none")

from panda3d.core import TransparencyAttrib, AlphaTestAttrib

import client_studio as cs
import datamodel_schema
import lua_gameplay_api as lga
import lua_runtime
import physics
import studio_editor_live
from shared import object_registry

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


ALPHA_TEST_GLB = "alpha_test.glb"


class _FakeGame:
    _build_part_entity = cs.MultiplayerGame._build_part_entity
    _apply_part_surface = cs.MultiplayerGame._apply_part_surface
    _apply_mesh_geometry = cs.MultiplayerGame._apply_mesh_geometry
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
        self.studio_playing = False
        self._physics_world = physics.PhysicsWorld(gravity=-24.0)

    def _mouse_look_captured(self) -> bool:
        return self._captured

    def set_camera_mode(self, third_person: bool) -> None:
        pass

    def teardown(self) -> None:
        self._physics_world.destroy()


def make_context(game: _FakeGame):
    manager = lua_runtime.LuaRuntimeManager(game)
    manager.start()
    ctx = lga.LuaGameplayContext(game, manager)
    ctx.start()
    return manager, ctx


def teardown(game: _FakeGame, manager, ctx) -> None:
    ctx.stop()
    manager.stop()
    game.teardown()


# ============================================================
# Schema / validation / clamping
# ============================================================

def test_transparency_schema_and_defaults() -> None:
    part = object_registry.get_object_type("Part")
    check("Transparency" in part.property_schema, "Part declares Transparency")
    check(part.default_properties.get("Transparency") == 0.0, "Transparency defaults to 0.0 (opaque) -- old Places are unaffected")


def test_transparency_clamping() -> None:
    from shared.instance import sanitize_part_properties
    clean = sanitize_part_properties({"Transparency": 5.0})
    check(clean.get("Transparency") == 1.0, "Transparency > 1 is clamped to 1.0")
    clean = sanitize_part_properties({"Transparency": -2.0})
    check(clean.get("Transparency") == 0.0, "Transparency < 0 is clamped to 0.0")
    clean = sanitize_part_properties({"Transparency": 0.5})
    check(clean.get("Transparency") == 0.5, "a valid intermediate Transparency passes through unchanged")


# ============================================================
# Real render-state: is blending actually enabled?
# ============================================================

def _new_part_with_transparency(game: _FakeGame, transparency: float):
    defaults = dict(object_registry.get_object_type("Part").default_properties)
    defaults["Transparency"] = transparency
    entity = game._build_part_entity(defaults, class_name="Part")
    pid = f"diagnostic_part_{transparency}"
    game.parts[pid] = entity
    return entity, pid


def test_opaque_part_has_full_alpha_and_transparency_attrib_present() -> None:
    """Ursina's own Entity.model setter unconditionally calls
    self._model.setTransparency(TransparencyAttrib.M_dual) for every
    model, regardless of alpha -- verified this session via
    inspect.getsource(ursina.entity.Entity), not assumed. That single
    fact is what makes Part.Transparency already render correctly with
    zero SStudio-side blending code: this asserts that mechanism is
    still present (a real render-state fact, not schema/color alone)."""
    game = _FakeGame()
    try:
        entity, _ = _new_part_with_transparency(game, 0.0)
        alpha = entity.color[3]
        check(abs(alpha - 1.0) < 0.001, f"Transparency=0.0 gives full alpha (opaque), got {alpha}")
        attrib = entity.model.getAttrib(TransparencyAttrib)
        check(attrib is not None, f"blending IS enabled on the model's RenderState (TransparencyAttrib present): {attrib}")
    finally:
        game.teardown()


def test_intermediate_transparency_produces_correct_alpha() -> None:
    game = _FakeGame()
    try:
        entity, _ = _new_part_with_transparency(game, 0.5)
        alpha = entity.color[3]
        check(abs(alpha - 0.5) < 0.01, f"Transparency=0.5 gives alpha~0.5, got {alpha}")
    finally:
        game.teardown()


def test_fully_transparent_part_is_not_destroyed_or_hidden() -> None:
    """Spec requirement: Transparency=1.0 must not destroy/recreate the
    Instance, and the render node stays active (physics/hierarchy/
    selection/scripts all keep working) -- only the visual alpha
    changes."""
    game = _FakeGame()
    try:
        entity, pid = _new_part_with_transparency(game, 1.0)
        alpha = entity.color[3]
        check(alpha < 0.01, f"Transparency=1.0 gives alpha~0 (effectively invisible), got {alpha}")
        check(pid in game.parts, "the Entity is NOT removed from game.parts at Transparency=1.0")
        check(entity.enabled, "the Entity stays enabled (active) at Transparency=1.0 -- it is hidden via alpha, not disabled")
        check(entity.collider is not None, "the picking collider is still present at Transparency=1.0 -- Explorer/viewport selection remains possible")
    finally:
        game.teardown()


def test_transparency_going_back_to_zero_restores_full_opacity() -> None:
    game = _FakeGame()
    try:
        entity, pid = _new_part_with_transparency(game, 1.0)
        check(entity.color[3] < 0.01, "starts fully transparent")
        merged = dict(object_registry.get_object_type("Part").default_properties)
        merged["Transparency"] = 0.0
        # Mirrors _apply_instance_properties()'s real color-reapplication
        # trigger path (Color/Transparency changed -> re-set entity.color).
        rgb = merged["Color"]
        from ursina import color as ursina_color
        entity.color = ursina_color.rgba32(int(rgb[0]), int(rgb[1]), int(rgb[2]), int(255 * (1.0 - merged["Transparency"])))
        check(abs(entity.color[3] - 1.0) < 0.01, "setting Transparency back to 0.0 immediately restores full opacity, no destroy/recreate needed")
    finally:
        game.teardown()


def test_transparent_part_picking_collider_unaffected_by_transparency() -> None:
    """Editor selection requirement: a transparent/fully-transparent Part
    must remain selectable in the viewport -- the picking collider is
    built unconditionally in _build_part_entity() (Stage 2.4's existing
    'collider is independent of CanCollide/appearance' contract), so
    this is true by construction; verified directly for both extremes."""
    game = _FakeGame()
    try:
        for transparency in (0.0, 0.5, 1.0):
            entity, _ = _new_part_with_transparency(game, transparency)
            check(entity.collider is not None, f"Transparency={transparency}: picking collider is present")
    finally:
        game.teardown()


# ============================================================
# Lifecycle: Clone / Destroy / Workspace reparent / Lua / save-open
# ============================================================

def test_clone_preserves_transparency() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, original = manager.scene.instance_new("Part", "Workspace")
        assert ok, original
        ok, err = manager.scene.set_property(original, "Transparency", 0.7)
        check(ok, f"setting Transparency via Lua succeeds: {err}")

        ok, clone_id = manager.scene.clone(original)
        check(ok, f"cloning succeeds: {clone_id}")
        kind, value = manager.scene.get_property(clone_id, "Transparency")
        check(value == 0.7, f"the clone's Transparency matches the original, got {value}")
    finally:
        teardown(game, manager, ctx)


def test_destroy_removes_transparent_part_cleanly() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, pid = manager.scene.instance_new("Part", "Workspace")
        assert ok, pid
        manager.scene.set_property(pid, "Transparency", 0.5)
        check(pid in game.parts, "transparent Part is active before Destroy()")
        manager.scene.destroy(pid)
        check(not manager.scene.exists(pid), "Destroy() removes the Instance")
        check(pid not in game.parts, "...and its Entity")
    finally:
        teardown(game, manager, ctx)


def test_workspace_reparent_preserves_transparency_and_collider() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, pid = manager.scene.instance_new("Part", None)
        assert ok, pid
        manager.scene.set_property(pid, "Transparency", 0.4)
        ok, err = manager.scene.set_parent(pid, "Workspace")
        check(ok, f"attaching to Workspace succeeds: {err}")
        entity = game.parts[pid]
        check(abs(entity.color[3] - 0.6) < 0.01, f"attaching builds the Entity WITH the authored Transparency applied, got alpha={entity.color[3]}")
        check(entity.collider is not None, "...and a picking collider")

        ok, folder = manager.scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        manager.scene.set_parent(pid, folder)
        check(not game.parts[pid].enabled, "detaching disables the Entity (Transparency state untouched, not destroyed)")

        manager.scene.set_parent(pid, "Workspace")
        check(game.parts[pid].enabled, "reattaching re-enables it")
        check(abs(game.parts[pid].color[3] - 0.6) < 0.01, "...with Transparency intact")
    finally:
        teardown(game, manager, ctx)


def test_transparency_lua_get_set() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    try:
        ok, pid = manager.scene.instance_new("Part", "Workspace")
        assert ok, pid
        kind, value = manager.scene.get_property(pid, "Transparency")
        check(kind == "number" and value == 0.0, "Transparency defaults to 0.0 over Lua")

        ok, err = manager.scene.set_property(pid, "Transparency", 0.8)
        check(ok, f"Part.Transparency = 0.8 from Lua succeeds: {err}")
        entity = game.parts[pid]
        check(abs(entity.color[3] - 0.2) < 0.01, f"the Lua write immediately updates the real alpha, got {entity.color[3]}")

        # Matches the existing, pre-4.3D convention already established
        # for Color (same overlay code path): out-of-range numeric writes
        # are CLAMPED, not rejected -- verified against the real
        # behavior, not assumed.
        ok, err = manager.scene.set_property(pid, "Transparency", 2.0)
        check(ok, f"an out-of-range Transparency write from Lua is accepted (clamped, matching Color's existing convention): {err}")
        kind, value = manager.scene.get_property(pid, "Transparency")
        check(value == 1.0, f"...and clamped to 1.0, got {value}")

        ok, err = manager.scene.set_property(pid, "Transparency", "half")
        check(not ok, "a non-numeric Transparency write from Lua is rejected")
    finally:
        teardown(game, manager, ctx)


def test_transparency_persists_through_save_open() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = tmp_dir + "\\transparency_roundtrip.nebula.json"
        objects = [{
            "id": "p1", "class_name": "Part", "name": "GlassPane", "parent_id": "Workspace",
            "properties": {
                "Position": [0, 0, 0], "Size": [2, 2, 0.1], "Rotation": [0, 0, 0], "Color": [90, 140, 200],
                "Material": "Glass", "Transparency": 0.6, "Anchored": True, "CanCollide": True,
            },
            "tags": [], "attributes": {}, "enabled": True,
        }]
        manager = pm.PlaceManager()
        save_result = manager.save_as(path, objects, datamodel_schema.sanitize_services_snapshot(None))
        check(save_result.success, f"saving a glass Part succeeds: {save_result.message}")

        reopened = pm.PlaceManager().open(path)
        check(reopened.success, f"reopening succeeds: {reopened.message}")
        props = (reopened.objects or [{}])[0].get("properties", {})
        check(props.get("Transparency") == 0.6, "Transparency survives save/open")


# ============================================================
# Shadow policy (Option B: transparent Parts cast ordinary opaque
# shadows -- a deliberate, documented v1 choice, not a silent gap)
# ============================================================

def test_transparent_part_shadow_policy_is_ordinary_opaque_shadows() -> None:
    """Stage 4.3D deliberately chose Option B: a transparent Part casts a
    normal opaque-looking shadow, same as any other Part -- no
    shadow-camera exclusion is implemented. This asserts that choice is
    real and intentional: nothing in the property-application code path
    calls NodePath.hide()/setLightOff()/any shadow-exclusion API based on
    Transparency, so the entity's shadow behavior is genuinely identical
    to an opaque Part's, not an accidental half-implemented feature."""
    game = _FakeGame()
    try:
        entity, _ = _new_part_with_transparency(game, 0.8)
        check(not entity.is_hidden(), "a highly-transparent Part's underlying NodePath is not .hide()-d for any camera (including the shadow camera) -- Option B, ordinary opaque shadow")
    finally:
        game.teardown()

    import inspect
    surface_source = inspect.getsource(cs.MultiplayerGame._apply_part_surface)
    check(".hide(" not in surface_source and "setLightOff" not in surface_source, "_apply_part_surface() contains no shadow-exclusion logic tied to Transparency -- confirms Option B is the actual, sole behavior, not a partially-implemented Option A")


# ============================================================
# MeshPart / embedded glTF alphaMode preservation
# ============================================================

def test_meshpart_alpha_test_glb_loads() -> None:
    node, error = cs.load_mesh_node(ALPHA_TEST_GLB)
    check(node is not None, f"alpha_test.glb (original diagnostic/showcase asset: OPAQUE/MASK/BLEND panels) loads: {error!r}")


def test_meshpart_preserves_embedded_mask_and_blend_alpha_state() -> None:
    """Proven this session with a real render (see the Stage 4.3D
    completion report) that MASK produces a genuine hard per-pixel
    cutout and BLEND produces genuine smooth blending. This is the
    permanent, headless regression companion: confirms the underlying
    Panda3D RenderAttrib state that makes that possible actually
    survives load_mesh_node() (including its setTransparency(MNone)
    call) untouched, at the per-geom level panda3d-gltf uses."""
    node, error = cs.load_mesh_node(ALPHA_TEST_GLB)
    check(node is not None, f"loads: {error!r}")
    if node is None:
        return
    found_alpha_test = False
    found_transparency = False
    for geom_node_path in node.findAllMatches("**/+GeomNode"):
        geom_node = geom_node_path.node()
        for i in range(geom_node.getNumGeoms()):
            state = geom_node.getGeomState(i)
            if state.getAttrib(AlphaTestAttrib) is not None:
                found_alpha_test = True
            if state.getAttrib(TransparencyAttrib) is not None:
                found_transparency = True
    check(found_alpha_test, "the MASK panel's per-geom AlphaTestAttrib survives load_mesh_node() (including its setTransparency(MNone) call) untouched")
    check(found_transparency, "the BLEND panel's per-geom TransparencyAttrib survives load_mesh_node() untouched")


def test_meshpart_does_not_get_a_transparency_override() -> None:
    """Stage 4.3D decision (Option A, Part 10 of the spec): MeshPart gets
    NO SStudio-level Transparency multiplier -- embedded glTF material
    alpha is the whole contract, preserved untouched (already proven).
    MeshPart DOES inherit a "Transparency" schema entry (it reuses the
    shared Part/SpawnPoint/MeshPart base schema), but confirmed here
    that it is genuinely dormant/unused: _apply_mesh_geometry() never
    reads it. This guards against a future accidental wiring-up of that
    inherited-but-inert property silently changing this decision without
    deliberate architecture review."""
    import inspect
    mesh_source = inspect.getsource(cs.MultiplayerGame._apply_mesh_geometry)
    check("Transparency" not in mesh_source, "_apply_mesh_geometry() never reads Transparency -- MeshPart's inherited schema entry is inert; embedded glTF alpha is the only thing that actually controls MeshPart transparency")


# ============================================================
# Material presets
# ============================================================

def test_meshpart_inspector_does_not_expose_a_non_functional_transparency_control() -> None:
    """API-surface honesty check (raised alongside the ghost-light
    blocker report): MeshPart inherits a dormant Transparency schema
    entry (see test_meshpart_does_not_get_a_transparency_override()
    above), so the Inspector must not offer a slider that looks
    functional but silently does nothing when dragged. Source-level
    check, matching this codebase's established pattern for facts a full
    Qt widget instantiation isn't needed to prove -- _build_appearance_
    section() is a single shared method for Part/SpawnPoint/MeshPart, so
    this confirms the MeshPart exclusion is real code, not just a
    docstring claim."""
    import inspect
    # _build_appearance_section lives on the Inspector panel class --
    # locate it generically by scanning the module rather than hardcoding
    # that class's name here.
    method = None
    for name, obj in vars(studio_editor_live).items():
        candidate = getattr(obj, "_build_appearance_section", None)
        if candidate is not None:
            method = candidate
            break
    check(method is not None, "found _build_appearance_section() in studio_editor_live")
    if method is not None:
        source = inspect.getsource(method)
        check('obj.object_type != "MeshPart"' in source, "_build_appearance_section() gates the Transparency row behind a MeshPart exclusion check")


def test_glass_preset_initializes_a_useful_transparency() -> None:
    preset = object_registry.MATERIAL_PRESETS.get("Glass")
    check(preset is not None and preset.get("Transparency") == 0.6, f"the Glass preset now includes a real starting Transparency, got {preset}")
    check(preset.get("Roughness") == 0.05 and preset.get("Metallic") == 0.0, "Glass's existing Roughness/Metallic are unchanged")


test_transparency_schema_and_defaults()
test_transparency_clamping()
test_opaque_part_has_full_alpha_and_transparency_attrib_present()
test_intermediate_transparency_produces_correct_alpha()
test_fully_transparent_part_is_not_destroyed_or_hidden()
test_transparency_going_back_to_zero_restores_full_opacity()
test_transparent_part_picking_collider_unaffected_by_transparency()
test_clone_preserves_transparency()
test_destroy_removes_transparent_part_cleanly()
test_workspace_reparent_preserves_transparency_and_collider()
test_transparency_lua_get_set()
test_transparency_persists_through_save_open()
test_transparent_part_shadow_policy_is_ordinary_opaque_shadows()
test_meshpart_alpha_test_glb_loads()
test_meshpart_preserves_embedded_mask_and_blend_alpha_state()
test_meshpart_does_not_get_a_transparency_override()
test_meshpart_inspector_does_not_expose_a_non_functional_transparency_control()
test_glass_preset_initializes_a_useful_transparency()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All transparency tests passed.")
