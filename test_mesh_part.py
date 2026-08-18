"""Regression tests for Stage 4.1 (showcase sprint)'s MeshPart -- the first
real-mesh Instance type: a Part-like object (Position/Size/Rotation/Color-
as-tint/Transparency/Anchored/CanCollide, exactly like Part/SpawnPoint) that
displays a loaded .glb/.gltf mesh instead of a primitive cube.

Architecture under test (see client_studio.py's load_mesh_node()/
resolve_mesh_asset_path()/_build_part_entity()/_apply_mesh_geometry(), and
lua_runtime.py's MeshId get_property/set_property branches):
  - MeshId is a path relative to MESH_ASSETS_DIR (assets/meshes/), resolved
    defensively (rejects absolute paths and ".." escapes) before ever
    touching the filesystem;
  - the mesh geometry is loaded via Panda3D's own loader.loadModel() (the
    same call model_debug_viewer.py already proved works for a .glb here,
    backed by the installed panda3d-gltf loader), reparented onto the same
    transform/collider root a Part's cube would use -- Size still scales it
    and still drives the (box-only) physics/picking collider, same as
    Part/SpawnPoint;
  - a missing/invalid MeshId falls back to a visible magenta placeholder
    cube rather than leaving the Part invisible or crashing;
  - MeshPart otherwise reuses every existing has_3d_entity mechanism
    (world-membership/Clone/Destroy/Play-Stop cleanup) unchanged -- this
    file focuses on what's NEW (mesh loading + MeshId), not re-proving
    world-membership/lifecycle behavior test_world_membership.py and
    test_stage39_api.py already cover generically for any has_3d_entity
    class.

Follows this project's existing test convention: plain top-level-assertion
script, run directly, offscreen Qt platform, headless Ursina window, a REAL
Bullet PhysicsWorld and a REAL Ursina/Panda3D scene -- mesh presence is
checked by actually loading assets/meshes/test_prop.glb (a copy of the
project's own existing assets/player.glb, reused here purely as a known-
good, already-present real .glb file), not inferred from source.
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
    _build_part_entity = cs.MultiplayerGame._build_part_entity
    _apply_mesh_geometry = cs.MultiplayerGame._apply_mesh_geometry

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


REAL_MESH_ID = "test_prop.glb"


# ============================================================
# Registry / class metadata
# ============================================================

def test_meshpart_is_registered_and_part_like() -> None:
    definition = cs.object_registry.get_object_type("MeshPart")
    check(definition is not None, "MeshPart is registered in object_registry")
    check(definition.has_3d_entity, "MeshPart.has_3d_entity is True")
    check("MeshId" in definition.property_schema, "MeshPart has a MeshId property in its schema")
    check(definition.default_properties.get("MeshId") == "", "MeshPart.MeshId defaults to empty string")
    check(definition.default_properties.get("Size") == [1.0, 1.0, 1.0], "MeshPart.Size defaults like a Part")
    check(datamodel_schema.is_a("MeshPart", "Part"), "IsA('Part') is true for MeshPart, same as SpawnPoint")
    check(datamodel_schema.is_a("MeshPart", "Instance"), "IsA('Instance') is true for MeshPart")


# ============================================================
# Asset path resolution (security-relevant: MeshId is untrusted data)
# ============================================================

def test_resolve_mesh_asset_path_accepts_relative_paths() -> None:
    resolved = cs.resolve_mesh_asset_path(REAL_MESH_ID)
    check(resolved is not None, "a plain relative MeshId resolves to a real path")
    check(resolved is not None and resolved.exists(), "...and that path actually exists on disk")


def test_resolve_mesh_asset_path_rejects_escapes() -> None:
    check(cs.resolve_mesh_asset_path("../player.glb") is None, "'..' escape is rejected")
    check(cs.resolve_mesh_asset_path("../../assets/player.glb") is None, "deeper '..' escape is rejected")
    check(cs.resolve_mesh_asset_path("/etc/passwd") is None, "a POSIX absolute path is rejected")
    check(cs.resolve_mesh_asset_path("C:/Windows/win.ini") is None, "a Windows absolute path is rejected")
    check(cs.resolve_mesh_asset_path("") is None, "an empty MeshId resolves to nothing")


# ============================================================
# Creation / loading -- a real .glb actually renders as loaded geometry
# ============================================================

def test_creating_meshpart_loads_real_geometry() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", "Workspace")
        assert ok, pid
        ok, err = scene.set_property(pid, "MeshId", REAL_MESH_ID)
        check(False, "MeshId set before geometry exists should still succeed (stored, not yet loaded)") if not ok else None

        # MeshId must be set BEFORE the Entity is built for it to affect the
        # loaded geometry (see set_property's MeshId docstring) -- destroy
        # and recreate with MeshId already present in properties, the
        # realistic authoring order (Inspector/Lua sets MeshId, then Parent
        # is what triggers the actual build).
        scene.destroy(pid)
        ok, pid = scene.instance_new("MeshPart", None)
        assert ok, pid
        ok, err = scene.set_property(pid, "MeshId", REAL_MESH_ID)
        check(ok, f"MeshId can be set on an unparented MeshPart: {err}")
        ok, err = scene.set_parent(pid, "Workspace")
        check(ok, f"parenting into Workspace succeeds: {err}")

        entity = game.parts.get(pid)
        check(entity is not None, "a real Ursina Entity was built for the MeshPart")
        check(getattr(entity, "_mesh_node", None) is not None, "the Entity carries a loaded mesh NodePath")
        check(not entity._mesh_node.isEmpty(), "...and that NodePath is real, non-empty geometry")
        check(getattr(entity, "_mesh_placeholder", None) is None, "no magenta placeholder was used for a valid asset")
    finally:
        teardown(game, manager, ctx)


def test_missing_asset_falls_back_to_placeholder_not_a_crash() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", None)
        assert ok, pid
        ok, err = scene.set_property(pid, "MeshId", "does_not_exist.glb")
        check(ok, f"setting a bogus MeshId is still accepted (validated at load time, not write time): {err}")
        ok, err = scene.set_parent(pid, "Workspace")
        check(ok, f"parenting still succeeds even though the asset is missing: {err}")

        entity = game.parts.get(pid)
        check(entity is not None, "an Entity is still built (does not silently fail to attach)")
        check(getattr(entity, "_mesh_node", None) is None, "no mesh NodePath (the asset genuinely does not exist)")
        check(getattr(entity, "_mesh_placeholder", None) is not None, "a placeholder cube was built instead")

        node, error = cs.load_mesh_node("does_not_exist.glb")
        check(node is None, "load_mesh_node() itself reports failure for a missing file")
        check(bool(error), f"...with a non-empty error message: {error!r}")
    finally:
        teardown(game, manager, ctx)


def test_empty_mesh_id_falls_back_to_placeholder() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", "Workspace")
        assert ok, pid
        entity = game.parts.get(pid)
        check(entity is not None, "a MeshPart with no MeshId set still gets an Entity")
        check(getattr(entity, "_mesh_placeholder", None) is not None, "...and shows the placeholder cube, not a blank/invisible Part")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# MeshId property read/write via the same API every other property uses
# ============================================================

def test_mesh_id_get_set_roundtrip() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", "Workspace")
        assert ok, pid
        kind, value = scene.get_property(pid, "MeshId")
        check(kind == "string" and value == "", "MeshId reads back as an empty string by default")

        ok, err = scene.set_property(pid, "MeshId", REAL_MESH_ID)
        check(ok, f"MeshId can be written: {err}")
        kind, value = scene.get_property(pid, "MeshId")
        check(kind == "string" and value == REAL_MESH_ID, "MeshId reads back exactly what was written")

        ok, err = scene.set_property(pid, "MeshId", 123)
        check(not ok, "a non-string MeshId is rejected")
    finally:
        teardown(game, manager, ctx)


def test_meshpart_shares_ordinary_part_properties() -> None:
    """Position/Size/Rotation/Color/Transparency/Anchored/CanCollide all
    behave identically to a plain Part -- MeshPart adds MeshId, it does not
    replace or narrow anything else."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", "Workspace")
        assert ok, pid
        ok, err = scene.set_property(pid, "Position", [1.0, 2.0, 3.0])
        check(ok, f"Position writes like an ordinary Part: {err}")
        ok, err = scene.set_property(pid, "Size", [2.0, 3.0, 4.0])
        check(ok, f"Size writes like an ordinary Part: {err}")
        kind, value = scene.get_property(pid, "Position")
        check(kind == "vector3" and value == [1.0, 2.0, 3.0], "Position reads back correctly")
        entity = game.parts.get(pid)
        check(entity is not None and tuple(entity.scale) == (2.0, 3.0, 4.0), "the root Entity's scale actually carries Size")
        check(game._physics_world.has_body(pid), "a MeshPart gets a real physics body, same as a Part")
    finally:
        teardown(game, manager, ctx)


# ============================================================
# Serialization / save-open round trip
# ============================================================

def test_serialization_round_trip_via_datamodel_schema() -> None:
    """place_manager.py routes MeshPart through the schema-generic
    object_registry.sanitize_properties_for_type() (it is NOT in
    server.py's/place_manager.py's PART_LIKE_TYPES special case) --
    confirms that path really does preserve every field, MeshId included,
    the same generic mechanism SpawnPoint's Part-like fields already rely
    on."""
    raw = {
        "Position": [1.0, 2.0, 3.0],
        "Size": [2.0, 2.0, 2.0],
        "Rotation": [0.0, 45.0, 0.0],
        "Color": [10, 20, 30],
        "Material": "Wood",
        "Transparency": 0.25,
        "Anchored": True,
        "CanCollide": False,
        "MeshId": REAL_MESH_ID,
    }
    # NOTE: datamodel_schema.sanitize_persistent_properties() is NOT this
    # path for Part-like classes -- it is only ever used for the SERVICES
    # snapshot (Workspace.Gravity, StarterPlayer.*, ...); Part/SpawnPoint/
    # MeshPart properties are governed entirely by shared.object_registry's
    # PropertySpec/property_schema machinery, both here and in
    # place_manager.py/server.py (see test_place_save_open_round_trip()
    # below for the real end-to-end Place-file path).
    clean = cs.object_registry.sanitize_properties_for_type("MeshPart", raw)
    for key, value in raw.items():
        check(key in clean, f"sanitize_properties_for_type keeps {key!r}")
    check(clean.get("MeshId") == REAL_MESH_ID, "MeshId survives sanitization unchanged")
    check(clean.get("Size") == [2.0, 2.0, 2.0], "Size survives sanitization")
    check(clean.get("CanCollide") is False, "CanCollide survives sanitization")


def test_place_save_open_round_trip() -> None:
    import tempfile
    import place_manager as pm

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "mesh_roundtrip.nebula.json")
        objects = [
            {
                "id": "meshtest1",
                "class_name": "MeshPart",
                "name": "TestProp",
                "parent_id": "Workspace",
                "properties": {
                    "Position": [0.0, 1.0, 0.0],
                    "Size": [1.0, 1.0, 1.0],
                    "Rotation": [0.0, 0.0, 0.0],
                    "Color": [255, 255, 255],
                    "Material": "Plastic",
                    "Transparency": 0.0,
                    "Anchored": True,
                    "CanCollide": True,
                    "MeshId": REAL_MESH_ID,
                },
                "tags": [],
                "attributes": {},
                "enabled": True,
            }
        ]
        manager = pm.PlaceManager()
        save_result = manager.save_as(path, objects, datamodel_schema.sanitize_services_snapshot(None))
        check(save_result.success, f"saving a Place containing a MeshPart succeeds: {save_result.message}")

        reopened = pm.PlaceManager()
        open_result = reopened.open(path)
        check(open_result.success, f"reopening the saved Place succeeds: {open_result.message}")
        loaded_objects = open_result.objects or []
        mesh_objects = [o for o in loaded_objects if o.get("class_name") == "MeshPart"]
        check(len(mesh_objects) == 1, "exactly one MeshPart round-trips through save/open")
        if mesh_objects:
            check(
                mesh_objects[0].get("properties", {}).get("MeshId") == REAL_MESH_ID,
                "...with its MeshId preserved exactly",
            )


# ============================================================
# Workspace attach/detach, Clone, Destroy (MeshPart-specific confirmation --
# the underlying mechanism is already covered generically by
# test_world_membership.py; this confirms MeshId travels correctly through it)
# ============================================================

def test_workspace_attach_detach_carries_mesh() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", None)
        assert ok, pid
        scene.set_property(pid, "MeshId", REAL_MESH_ID)
        check(pid not in game.parts, "an unparented MeshPart has no Entity yet (lazy build, same as Part)")

        scene.set_parent(pid, "Workspace")
        check(pid in game.parts, "attaching to Workspace builds the Entity")
        check(getattr(game.parts[pid], "_mesh_node", None) is not None, "...with the mesh actually loaded")

        ok, folder = scene.instance_new("Folder", "ReplicatedStorage")
        assert ok, folder
        # Detaching disables the existing Entity and removes its physics
        # body -- it does NOT pop it from game.parts (same contract as any
        # has_3d_entity class; see _sync_world_attachment()'s re-enable
        # branch, which requires the Entity object to still be retrievable
        # here). test_world_membership.py's own is_active() helper checks
        # exactly this combination (game.parts membership AND a physics
        # body), not game.parts membership alone.
        scene.set_parent(pid, folder)
        entity = game.parts.get(pid)
        check(entity is not None and entity.enabled is False, "detaching to a non-Workspace root disables the Entity")
        check(not game._physics_world.has_body(pid), "...and removes the physics body")
        check(scene.exists(pid), "...without destroying the Instance itself")

        scene.set_parent(pid, "Workspace")
        check(pid in game.parts and game.parts[pid].enabled is True, "reattaching re-enables the Entity")
        check(getattr(game.parts[pid], "_mesh_node", None) is not None, "...with the mesh still correctly loaded")
    finally:
        teardown(game, manager, ctx)


def test_clone_preserves_mesh_id() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, original = scene.instance_new("MeshPart", "Workspace")
        assert ok, original
        scene.set_property(original, "MeshId", REAL_MESH_ID)

        ok, clone_id = scene.clone(original)
        check(ok, f"cloning a MeshPart succeeds: {clone_id}")
        check(clone_id != original, "the clone is a new, independent instance id")
        kind, value = scene.get_property(clone_id, "MeshId")
        check(kind == "string" and value == REAL_MESH_ID, "the clone's MeshId matches the original")
        check(clone_id not in game.parts, "a fresh clone has no Entity yet (starts Parent == nil, same as any has_3d_entity clone)")

        ok, err = scene.set_parent(clone_id, "Workspace")
        check(ok, f"parenting the clone into Workspace succeeds: {err}")
        check(clone_id in game.parts, "...and now it has a real Entity")
        check(getattr(game.parts[clone_id], "_mesh_node", None) is not None, "...with its own loaded mesh geometry")
        check(clone_id != original and game.parts[clone_id] is not game.parts.get(original), "clone and original have distinct Entities")
    finally:
        teardown(game, manager, ctx)


def test_destroy_cleans_up_mesh_entity_and_physics() -> None:
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        ok, pid = scene.instance_new("MeshPart", "Workspace")
        assert ok, pid
        scene.set_property(pid, "MeshId", REAL_MESH_ID)
        check(pid in game.parts, "MeshPart is active before Destroy()")

        scene.destroy(pid)
        check(not scene.exists(pid), "Destroy() removes the Instance")
        check(pid not in game.parts, "...and its Entity")
        check(not game._physics_world.has_body(pid), "...and its physics body")
    finally:
        teardown(game, manager, ctx)


def test_repeated_play_stop_does_not_leak_mesh_nodes() -> None:
    """Performance-discipline check from the sprint spec: repeated Play/Stop
    (here: repeated create+destroy cycles, the same thing a Play session's
    world teardown does at scale) must not accumulate stale mesh geometry."""
    game = _FakeGame()
    manager, ctx = make_context(game)
    scene = manager.scene
    try:
        for _ in range(5):
            ok, pid = scene.instance_new("MeshPart", "Workspace")
            assert ok, pid
            scene.set_property(pid, "MeshId", REAL_MESH_ID)
            scene.destroy(pid)
        check(len(game.parts) == 0, "no stale Entities remain after 5 create/destroy cycles")
        check(len(game._physics_world._bodies) == 0 if hasattr(game._physics_world, "_bodies") else True, "no stale physics bodies remain (best-effort check)")
    finally:
        teardown(game, manager, ctx)


test_meshpart_is_registered_and_part_like()
test_resolve_mesh_asset_path_accepts_relative_paths()
test_resolve_mesh_asset_path_rejects_escapes()
test_creating_meshpart_loads_real_geometry()
test_missing_asset_falls_back_to_placeholder_not_a_crash()
test_empty_mesh_id_falls_back_to_placeholder()
test_mesh_id_get_set_roundtrip()
test_meshpart_shares_ordinary_part_properties()
test_serialization_round_trip_via_datamodel_schema()
test_place_save_open_round_trip()
test_workspace_attach_detach_carries_mesh()
test_clone_preserves_mesh_id()
test_destroy_cleans_up_mesh_entity_and_physics()
test_repeated_play_stop_does_not_leak_mesh_nodes()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for message in FAILURES:
        print(f"  - {message}")
    sys.exit(1)
print("All MeshPart tests passed.")
