"""Behavior-level tests for Stage 3.2's Project/Place workflow
(place_manager.py) and the server-side REPLACE_WORLD security/validation
logic it depends on (server.py).

Follows this project's existing test convention (test_script_editor.py,
test_physics_module.py): a plain script of top-level assertions run
directly with `python test_place_manager.py`, no pytest/unittest. Requires
the offscreen Qt platform (set QT_QPA_PLATFORM=offscreen before running, or
this sets it itself if unset) since PlaceManager's RecentPlacesStore uses
QSettings and PlaceCreateDialog/RecentPlacesDialog are real QWidgets.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import place_manager as pm

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def make_temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="sstudio_place_test_"))


def isolated_recents_store(tmp_dir: Path) -> pm.RecentPlacesStore:
    """QSettings normally writes to the real user registry/INI -- point it
    at a throwaway file so these tests never touch (or depend on) the
    developer machine's actual Recents list."""
    store = pm.RecentPlacesStore()
    ini_path = tmp_dir / "recents_test.ini"
    store._settings = QSettings(str(ini_path), QSettings.Format.IniFormat)
    return store


# ============================================================
# TemplateRepository
# ============================================================

def test_template_repository() -> None:
    repo = pm.TemplateRepository()
    ids = repo.available_ids()
    expected = {
        "blank", "baseplate", "flat_terrain", "starter_scene", "obby",
        "village", "castle", "suburban", "island", "racing", "city_block",
        "fps_arena",
    }
    check(set(ids) == expected, f"available_ids() returns exactly the 12 real template ids (got {ids})")

    blank = repo.instantiate("blank")
    check(blank == [], "Blank template instantiates to zero objects")

    baseplate = repo.instantiate("baseplate")
    check(len(baseplate) >= 1, "Baseplate template instantiates at least one object")
    part = next((o for o in baseplate if o["class_name"] == "Part"), None)
    check(part is not None, "Baseplate template contains a Part")
    if part is not None:
        check(part["properties"].get("Anchored") is True, "Baseplate Part is Anchored")
        check(part["properties"].get("CanCollide") is True, "Baseplate Part is CanCollide")

    ids_a = {o["id"] for o in repo.instantiate("baseplate")}
    ids_b = {o["id"] for o in repo.instantiate("baseplate")}
    check(ids_a.isdisjoint(ids_b), "Two instantiations of the same template produce disjoint id sets")

    try:
        repo.instantiate("does_not_exist")
        check(False, "instantiate() on an unknown template id raises")
    except FileNotFoundError:
        check(True, "instantiate() on an unknown template id raises FileNotFoundError")


def test_all_templates_load_and_validate() -> None:
    repo = pm.TemplateRepository()
    for template_id in repo.available_ids():
        objects = repo.instantiate(template_id)
        ids = [o["id"] for o in objects]
        check(len(ids) == len(set(ids)), f"'{template_id}' template has no duplicate instance ids")
        by_id = {o["id"] for o in objects}
        for obj in objects:
            parent = obj.get("parent_id")
            ok = parent is None or parent in pm.ROOT_SERVICES or parent in by_id
            check(ok, f"'{template_id}' template: object {obj['id']} has a resolvable parent")


# ============================================================
# Name / path validation (spec section 9: local path security)
# ============================================================

def test_validate_place_name() -> None:
    check(pm.validate_place_name("MyPlace")[0], "validate_place_name accepts a normal name")
    check(not pm.validate_place_name("")[0], "validate_place_name rejects an empty name")
    check(not pm.validate_place_name("   ")[0], "validate_place_name rejects a whitespace-only name")
    check(not pm.validate_place_name("..")[0], "validate_place_name rejects '..'")
    check(not pm.validate_place_name("a" * 200)[0], "validate_place_name rejects an overlong name")
    check(not pm.validate_place_name("bad/name")[0], "validate_place_name rejects a name with '/'")
    check(not pm.validate_place_name("bad:name")[0], "validate_place_name rejects a name with ':'")
    check(not pm.validate_place_name("CON")[0], "validate_place_name rejects the reserved Windows name CON")
    check(not pm.validate_place_name("lpt1")[0], "validate_place_name rejects reserved names case-insensitively")
    check(not pm.validate_place_name("trailing.")[0], "validate_place_name rejects a name ending in '.'")


def test_is_within_root() -> None:
    tmp = make_temp_dir()
    try:
        root = tmp / "root"
        root.mkdir()
        inside = root / "Project" / "Main.nebula.json"
        check(pm.is_within_root(inside, root), "is_within_root accepts a path inside root")
        outside = tmp / "elsewhere" / "Main.nebula.json"
        check(not pm.is_within_root(outside, root), "is_within_root rejects a path outside root")
        traversal = root / ".." / "elsewhere"
        check(not pm.is_within_root(traversal, root), "is_within_root rejects a '..' traversal path")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# create_from_template() / open() two-phase commit
# ============================================================

def test_create_from_template_defers_state_until_commit() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)

        result = manager.create_from_template("blank", "MyPlace", tmp)
        check(result.success, "create_from_template succeeds for a valid name/template")
        check(manager.current_path is None, "create_from_template does NOT mutate current_path before commit()")
        check(manager.display_name == "Untitled Place", "create_from_template does NOT mutate display_name before commit()")
        check(not manager.is_dirty, "create_from_template does not touch dirty state before commit()")

        place_path = tmp / "MyPlace" / pm.PRIMARY_PLACE_FILENAME
        check(place_path.is_file(), "create_from_template writes the Place file to disk")
        check((tmp / "MyPlace" / pm.PROJECT_METADATA_FILENAME).is_file(), "create_from_template writes the project metadata sidecar")

        manager.commit(result)
        check(manager.current_path == place_path, "commit() sets current_path from the deferred result")
        check(manager.display_name == "MyPlace", "commit() sets display_name from the deferred result")
        check(not manager.is_dirty, "a freshly committed create starts clean")

        recents = manager.recents.list()
        check(len(recents) == 1 and recents[0].path == str(place_path.resolve()), "commit() records the new Place in Recents")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_from_template_rejects_existing_destination() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        first = manager.create_from_template("blank", "Dup", tmp)
        check(first.success, "first create_from_template of a given name succeeds")
        second = manager.create_from_template("blank", "Dup", tmp)
        check(not second.success, "create_from_template refuses to silently overwrite an existing project folder")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_from_template_rejects_invalid_name() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        result = manager.create_from_template("blank", "", tmp)
        check(not result.success, "create_from_template rejects an empty name before touching disk")
        check(not any(tmp.iterdir()), "create_from_template with an invalid name creates no files")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_open_defers_state_until_commit_and_round_trips() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        created = manager.create_from_template("baseplate", "RoundTrip", tmp)
        manager.commit(created)
        original_objects = created.objects

        # Fresh manager instance simulates "open a Place from disk" without
        # any create_from_template() state already present.
        reopened = pm.PlaceManager(projects_root=tmp)
        reopened.recents = isolated_recents_store(tmp)
        result = reopened.open(created.path)
        check(result.success, "open() succeeds for a just-created Place file")
        check(reopened.current_path is None, "open() does NOT mutate current_path before commit()")
        check(result.objects is not None and len(result.objects) == len(original_objects), "open() round-trips the same object count")
        ids_before = {o["id"] for o in original_objects}
        ids_after = {o["id"] for o in (result.objects or [])}
        check(ids_before == ids_after, "open() preserves the exact instance ids on disk (no remap on open)")

        reopened.commit(result)
        check(reopened.current_path == created.path, "commit() applies the open() result's path")
        check(reopened.display_name == "RoundTrip", "commit() applies the open() result's display_name from project metadata")
        check(reopened.template_id == "baseplate", "commit() recovers template_id from project metadata")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_open_rejects_missing_and_malformed_files_without_mutating_state() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)

        missing = manager.open(tmp / "does_not_exist.nebula.json")
        check(not missing.success, "open() rejects a nonexistent file")
        check(manager.current_path is None, "a rejected open() leaves current_path untouched")

        malformed_path = tmp / "malformed.nebula.json"
        malformed_path.write_text("{not valid json", encoding="utf-8")
        malformed = manager.open(malformed_path)
        check(not malformed.success, "open() rejects malformed JSON")

        wrong_shape_path = tmp / "wrong_shape.nebula.json"
        wrong_shape_path.write_text(json.dumps({"format": "nebula-scene", "version": 2}), encoding="utf-8")
        wrong_shape = manager.open(wrong_shape_path)
        check(not wrong_shape.success, "open() rejects a file with no 'objects' list")

        future_version_path = tmp / "future_version.nebula.json"
        future_version_path.write_text(json.dumps({"format": "nebula-scene", "version": 999, "objects": []}), encoding="utf-8")
        future_version = manager.open(future_version_path)
        check(not future_version.success, "open() rejects an unsupported/future format version")

        check(manager.current_path is None, "current Place remains untouched after every rejected open()")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_open_upgrades_legacy_sceneobject_files() -> None:
    tmp = make_temp_dir()
    try:
        legacy_path = tmp / "legacy.nebula.json"
        legacy_doc = {
            "format": "nebula-scene",
            "version": 1,
            "objects": [
                {
                    "id": "legacy-1",
                    "object_type": "Part",
                    "name": "OldPart",
                    "parent": None,
                    "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                    "size": {"x": 4.0, "y": 1.0, "z": 4.0},
                    "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "color": "#ff0000",
                    "anchored": True,
                    "can_collide": True,
                }
            ],
        }
        legacy_path.write_text(json.dumps(legacy_doc), encoding="utf-8")

        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        result = manager.open(legacy_path)
        check(result.success, "open() accepts a legacy SceneObject-shaped file")
        objects = result.objects or []
        check(len(objects) == 1 and objects[0]["class_name"] == "Part", "legacy file upgrades object_type -> class_name")
        check(objects[0]["properties"].get("Position") == [1.0, 2.0, 3.0], "legacy position dict converts to Instance-shaped Position list")
        check(objects[0]["properties"].get("Anchored") is True, "legacy 'anchored' converts to Instance-shaped Anchored")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# Stage 3.8: "services" (root-service persistent properties) --
# format version 2 -> 3
# ============================================================

def test_open_version2_place_gets_full_schema_defaulted_services() -> None:
    """A version-2 Place file (pre-Stage-3.8, no "services" key at all)
    must still load successfully -- open() fills in a COMPLETE,
    schema-defaulted services snapshot rather than leaving it None/partial
    (spec: "missing service values use descriptor defaults")."""
    tmp = make_temp_dir()
    try:
        legacy_v2_path = tmp / "v2.nebula.json"
        legacy_v2_path.write_text(json.dumps({
            "format": "nebula-scene", "version": 2,
            "objects": [{"id": "p1", "class_name": "Part", "name": "P", "parent_id": None, "properties": {}}],
        }), encoding="utf-8")

        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        result = manager.open(legacy_v2_path)
        check(result.success, "open() accepts a version-2 Place with no 'services' key")
        check(result.services is not None, "open() never returns services=None, even for a version-2 file")
        check(result.services.get("Workspace", {}).get("Gravity") == 24.0, "a version-2 Place's Workspace.Gravity defaults to the accepted 24.0 magnitude")
        check("StarterPlayer" in result.services, "a version-2 Place still gets a complete StarterPlayer services entry")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_then_open_round_trips_services() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        created = manager.create_from_template("blank", "ServicesRoundTrip", tmp)
        manager.commit(created)

        custom_services = {
            "Workspace": {"Gravity": 5.0},
            "StarterPlayer": {"CameraMode": "LockFirstPerson", "CharacterWalkSpeed": 12.0},
        }
        saved = manager.save(created.objects or [], custom_services)
        check(saved.success, "save() with an explicit services dict succeeds")
        check(saved.services is not None and saved.services["Workspace"]["Gravity"] == 5.0, "save() returns the sanitized services it actually wrote")

        reopened = pm.PlaceManager(projects_root=tmp)
        reopened.recents = isolated_recents_store(tmp)
        result = reopened.open(created.path)
        check(result.success, "reopening a saved Place succeeds")
        check(result.services["Workspace"]["Gravity"] == 5.0, "Workspace.Gravity persists exactly through Save/Open")
        check(result.services["StarterPlayer"]["CameraMode"] == "LockFirstPerson", "StarterPlayer.CameraMode persists exactly through Save/Open")
        check(result.services["StarterPlayer"]["CharacterWalkSpeed"] == 12.0, "StarterPlayer.CharacterWalkSpeed persists exactly through Save/Open")
        # Unedited StarterPlayer properties still round-trip at their defaults.
        check(result.services["StarterPlayer"]["CharacterJumpPower"] == 8.0, "an unedited StarterPlayer property still round-trips at its default")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_open_rejects_malformed_service_values_without_failing_the_whole_load() -> None:
    tmp = make_temp_dir()
    try:
        path = tmp / "malformed_services.nebula.json"
        path.write_text(json.dumps({
            "format": "nebula-scene", "version": 3,
            "objects": [],
            "services": {
                "Workspace": {"Gravity": -999.0, "UnknownFutureProperty": "ignored"},
                "StarterPlayer": {"CameraMode": "NotARealMode"},
            },
        }), encoding="utf-8")

        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        result = manager.open(path)
        check(result.success, "a malformed 'services' value never fails the whole Place load")
        check(result.services["Workspace"]["Gravity"] == 24.0, "an invalid stored Gravity (negative) falls back to the default instead of loading garbage")
        check(result.services["StarterPlayer"]["CameraMode"] == "Classic", "an invalid stored CameraMode falls back to the default")
        check("UnknownFutureProperty" not in result.services["Workspace"], "an unknown future service property is silently dropped, not carried through")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_from_template_produces_default_services() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        created = manager.create_from_template("baseplate", "FreshServices", tmp)
        check(created.success, "create_from_template() succeeds")
        check(created.services is not None and created.services["Workspace"]["Gravity"] == 24.0, "a freshly created Place starts with full schema-default services")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# save() / save_as() / dirty state
# ============================================================

def test_save_requires_current_path() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        result = manager.save([])
        check(not result.success, "save() with no current_path fails cleanly (caller should fall back to Save As)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_as_then_save_round_trip_and_dirty_tracking() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)

        objects = [{
            "id": "abc123", "class_name": "Part", "name": "P", "parent_id": None,
            "properties": {"Position": [0, 0, 0]}, "tags": [], "attributes": {}, "enabled": True,
        }]
        dest = tmp / "SavedPlace.nebula.json"
        result = manager.save_as(dest, objects)
        check(result.success, "save_as() succeeds")
        check(manager.current_path == dest.resolve(), "save_as() updates current_path immediately (no network round trip needed)")
        check(dest.is_file(), "save_as() writes the file to disk")

        on_disk = json.loads(dest.read_text(encoding="utf-8"))
        check(on_disk["objects"] == objects, "saved file round-trips the exact object list")

        check(not manager.is_dirty, "manager is clean immediately after a successful save")
        manager.mark_authoritative_edit()
        check(manager.is_dirty, "mark_authoritative_edit() dirties the manager")
        manager.mark_authoritative_edit()
        check(manager.is_dirty, "manager stays dirty across multiple edits")

        second = manager.save(objects)
        check(second.success, "save() with an existing current_path succeeds")
        check(not manager.is_dirty, "save() clears dirty state")

        # Atomicity: a failing write must never leave a corrupt/zero-byte file.
        original_contents = dest.read_bytes()
        bogus_objects = {"not": "a list, will still serialize fine actually"}
        # Force a genuine failure by pointing at an unwritable destination
        # (a directory where the Place file should be) instead.
        blocked_dir = tmp / "blocked.nebula.json"
        blocked_dir.mkdir()
        failed = manager.save_as(blocked_dir, objects)
        check(not failed.success, "save_as() reports failure when the destination cannot be written")
        check(dest.read_bytes() == original_contents, "a failed save elsewhere leaves the previously-saved Place file untouched")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reset_to_untitled() -> None:
    tmp = make_temp_dir()
    try:
        manager = pm.PlaceManager(projects_root=tmp)
        manager.recents = isolated_recents_store(tmp)
        created = manager.create_from_template("blank", "ToReset", tmp)
        manager.commit(created)
        check(manager.current_path is not None, "sanity: manager has a current Place before reset")
        manager.reset_to_untitled()
        check(manager.current_path is None, "reset_to_untitled() clears current_path")
        check(manager.display_name == "Untitled Place", "reset_to_untitled() resets display_name")
        check((tmp / "ToReset" / pm.PRIMARY_PLACE_FILENAME).is_file(), "reset_to_untitled() does not delete the Place file on disk")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# RecentPlacesStore
# ============================================================

def test_recent_places_store() -> None:
    tmp = make_temp_dir()
    try:
        store = isolated_recents_store(tmp)
        check(store.list() == [], "a fresh RecentPlacesStore starts empty")

        p1 = tmp / "One.nebula.json"
        p2 = tmp / "Two.nebula.json"
        p1.write_text("{}", encoding="utf-8")
        p2.write_text("{}", encoding="utf-8")

        store.add(p1, "One")
        store.add(p2, "Two")
        entries = store.list()
        check([e.display_name for e in entries] == ["Two", "One"], "most-recently-added entry appears first")

        store.add(p1, "One (renamed)")
        entries = store.list()
        check(len(entries) == 2, "re-adding an existing path does not duplicate the entry")
        check(entries[0].display_name == "One (renamed)", "re-adding an existing path moves it to the front and updates its display name")

        store.remove(p2)
        entries = store.list()
        check(len(entries) == 1 and entries[0].display_name == "One (renamed)", "remove() deletes exactly the requested entry")

        missing_entry = pm.RecentPlaceEntry(path=str(tmp / "gone.nebula.json"), display_name="Gone", last_opened=0.0)
        check(not missing_entry.exists(), "RecentPlaceEntry.exists() is False for a file that isn't there")

        for i in range(pm.MAX_RECENTS + 5):
            path = tmp / f"Extra{i}.nebula.json"
            path.write_text("{}", encoding="utf-8")
            store.add(path, f"Extra{i}")
        check(len(store.list()) == pm.MAX_RECENTS, f"RecentPlacesStore caps at MAX_RECENTS ({pm.MAX_RECENTS})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recent_places_store_survives_malformed_settings() -> None:
    tmp = make_temp_dir()
    try:
        store = isolated_recents_store(tmp)
        store._settings.setValue(pm._RECENTS_SETTINGS_KEY, "not a list at all")
        check(store.list() == [], "list() tolerates a completely malformed settings value")

        store._settings.setValue(pm._RECENTS_SETTINGS_KEY, [{"path": "x"}, "not a dict", 42])
        entries = store.list()
        check(len(entries) == 1, "list() skips malformed individual entries instead of crashing")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# PlaceCreateDialog validation surface (no exec(), just the pure checks)
# ============================================================

def test_place_create_dialog_unique_default_name() -> None:
    tmp = make_temp_dir()
    try:
        (tmp / "MyPlace").mkdir()
        dialog = pm.PlaceCreateDialog("Blank", tmp, default_name="MyPlace")
        check(dialog.name_edit.text() == "MyPlace2", "PlaceCreateDialog suggests a non-colliding default name when the base name is already taken")
        dialog.deleteLater()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# Server-side REPLACE_WORLD hierarchy validation (mirrors place_manager's
# own _validate_hierarchy but is the actual security-relevant copy)
# ============================================================

def test_server_hierarchy_validation() -> None:
    import server
    from shared.instance import Instance

    valid = {
        "a": Instance(id="a", class_name="Part", name="A", parent_id=None, properties={}),
        "b": Instance(id="b", class_name="Part", name="B", parent_id="a", properties={}),
    }
    check(server._hierarchy_is_valid(valid), "server._hierarchy_is_valid accepts a normal parent chain")

    cyclic = {
        "a": Instance(id="a", class_name="Part", name="A", parent_id="b", properties={}),
        "b": Instance(id="b", class_name="Part", name="B", parent_id="a", properties={}),
    }
    check(not server._hierarchy_is_valid(cyclic), "server._hierarchy_is_valid rejects a 2-cycle")

    missing_parent = {
        "a": Instance(id="a", class_name="Part", name="A", parent_id="does-not-exist", properties={}),
    }
    check(not server._hierarchy_is_valid(missing_parent), "server._hierarchy_is_valid rejects a missing parent id")


def test_server_replace_world_object_sanitization() -> None:
    import server

    good = server._sanitize_replace_world_object({
        "id": "x1", "class_name": "Part", "name": "P", "parent_id": None,
        "properties": {"Position": [0, 0, 0]}, "enabled": True,
    })
    check(good is not None and good.class_name == "Part", "server sanitizes a well-formed replace_world object")

    check(server._sanitize_replace_world_object({"class_name": "TotallyBogusClassName"}) is None,
          "server rejects an unrecognized class_name in replace_world data")
    check(server._sanitize_replace_world_object({"id": "", "class_name": "Part"}) is None,
          "server rejects a replace_world object with an empty id")
    check(server._sanitize_replace_world_object("not a dict") is None,
          "server rejects a non-dict entry in replace_world data")


def test_server_singleton_conflict_starter_player_scripts() -> None:
    import server
    from shared.instance import Instance

    existing = {
        "sps1": Instance(id="sps1", class_name="StarterPlayerScripts", name="StarterPlayerScripts", parent_id="StarterPlayer", properties={}),
    }
    check(
        server._singleton_conflict(existing, "StarterPlayerScripts", "StarterPlayerScripts", "StarterPlayer") is not None,
        "creating a second StarterPlayerScripts is rejected",
    )
    check(
        server._singleton_conflict(existing, "StarterPlayerScripts", "StarterPlayerScripts", "StarterPlayer", exclude_id="sps1") is None,
        "renaming/reparenting the EXISTING StarterPlayerScripts (excluded from the scan) is not a conflict with itself",
    )
    check(
        server._singleton_conflict({}, "StarterPlayerScripts", "StarterPlayerScripts", "StarterPlayer") is None,
        "creating the first StarterPlayerScripts is allowed",
    )


def test_server_singleton_conflict_starter_character() -> None:
    import server
    from shared.instance import Instance

    existing = {
        "m1": Instance(id="m1", class_name="Model", name="StarterCharacter", parent_id="StarterPlayer", properties={}),
    }
    check(
        server._singleton_conflict(existing, "Model", "StarterCharacter", "StarterPlayer") is not None,
        "creating a second exact StarterCharacter under StarterPlayer is rejected",
    )
    check(
        server._singleton_conflict(existing, "Model", "SomeOtherModel", "StarterPlayer") is None,
        "creating a differently-named Model under StarterPlayer is allowed (ordinary Model, not the special role)",
    )
    check(
        server._singleton_conflict(existing, "Model", "StarterCharacter", "Workspace") is None,
        "creating a Model named StarterCharacter under Workspace is allowed (wrong parent -- not the special role at all)",
    )


def test_server_replace_world_rejects_multiple_starter_characters() -> None:
    import server
    from shared.instance import Instance

    conflict = server._replace_world_singleton_conflict({
        "a": Instance(id="a", class_name="Model", name="StarterCharacter", parent_id="StarterPlayer", properties={}),
        "b": Instance(id="b", class_name="Model", name="StarterCharacter", parent_id="StarterPlayer", properties={}),
    })
    check(conflict is not None, "REPLACE_WORLD data with two StarterCharacter Models under StarterPlayer is rejected")


def test_server_replace_world_rejects_multiple_starter_player_scripts() -> None:
    import server
    from shared.instance import Instance

    conflict = server._replace_world_singleton_conflict({
        "a": Instance(id="a", class_name="StarterPlayerScripts", name="StarterPlayerScripts", parent_id="StarterPlayer", properties={}),
        "b": Instance(id="b", class_name="StarterPlayerScripts", name="StarterPlayerScripts", parent_id="StarterPlayer", properties={}),
    })
    check(conflict is not None, "REPLACE_WORLD data with two StarterPlayerScripts is rejected")


def test_server_replace_world_rejects_non_local_connection() -> None:
    """Spec section 9: the server never accepts REPLACE_WORLD from a
    non-local connection. _is_local_connection() is the entire enforcement
    point since REPLACE_WORLD never carries a filesystem path at all -- a
    remote client physically cannot make the server touch any path."""
    import server

    server.clients.clear()

    class _FakeSocket:
        def __init__(self, host: str) -> None:
            self.remote_address = (host, 51234)

    server.clients["local_player"] = _FakeSocket("127.0.0.1")
    server.clients["remote_player"] = _FakeSocket("203.0.113.5")

    check(server._is_local_connection("local_player"), "server treats a 127.0.0.1 connection as local")
    check(not server._is_local_connection("remote_player"), "server treats a non-loopback connection as non-local")
    check(not server._is_local_connection("unknown_player"), "server treats an unknown player id as non-local (fails closed)")

    server.clients.clear()


if __name__ == "__main__":
    test_template_repository()
    test_all_templates_load_and_validate()
    test_validate_place_name()
    test_is_within_root()
    test_create_from_template_defers_state_until_commit()
    test_create_from_template_rejects_existing_destination()
    test_create_from_template_rejects_invalid_name()
    test_open_defers_state_until_commit_and_round_trips()
    test_open_rejects_missing_and_malformed_files_without_mutating_state()
    test_open_upgrades_legacy_sceneobject_files()
    test_open_version2_place_gets_full_schema_defaulted_services()
    test_save_then_open_round_trips_services()
    test_open_rejects_malformed_service_values_without_failing_the_whole_load()
    test_create_from_template_produces_default_services()
    test_save_requires_current_path()
    test_save_as_then_save_round_trip_and_dirty_tracking()
    test_reset_to_untitled()
    test_recent_places_store()
    test_recent_places_store_survives_malformed_settings()
    test_place_create_dialog_unique_default_name()
    test_server_hierarchy_validation()
    test_server_replace_world_object_sanitization()
    test_server_singleton_conflict_starter_player_scripts()
    test_server_singleton_conflict_starter_character()
    test_server_replace_world_rejects_multiple_starter_characters()
    test_server_replace_world_rejects_multiple_starter_player_scripts()
    test_server_replace_world_rejects_non_local_connection()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for message in FAILURES:
            print(f"  - {message}")
        sys.exit(1)
    print("All place_manager tests passed.")
