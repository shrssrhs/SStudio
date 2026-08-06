"""
Stage 3.2: Project/Place management.

Terminology (see Stage 3.2 spec): a *Project* is a local folder; a *Place* is
one editable scene/world file inside it. One Project holds one primary Place
for this stage -- no multi-Place-per-project UI yet.

PlaceManager itself is deliberately engine-agnostic: no QWidget, no Ursina
Entity, no Bullet body, no Lua coroutine reference is ever stored on it (see
its docstring). Its one Qt dependency is QSettings for the local
recent-places store, which is a plain INI-backed key/value store, not a
widget. PlaceCreateDialog at the bottom of this file is the one genuine
QWidget in this module -- a small native "Create Place" dialog that only
calls validate_place_name() and returns a name/directory pair; it holds no
manager state itself. studio_editor_live.py/client_studio.py own all other
UI and network glue and call into this module's pure functions.

Wire/file schema note: the authoritative live world is `shared.instance.
Instance`-shaped (id/class_name/name/parent_id/properties/tags/attributes/
enabled) -- that is what the server's `world` dict and WORLD_SNAPSHOT/
REPLACE_WORLD already speak, and it requires no lossy Position/hex-color
round-tripping the way the older offline-demo SceneObject shape does. Place
files therefore store Instance-shaped objects inside the SAME `.nebula.json`
envelope (`{"format": "nebula-scene", "version": N, "objects": [...]}`)
already used by studio_editor_live.EngineBridge's offline demo path,
detected and transparently upgraded on open() so old SceneObject-shaped
scene files (identified by an "object_type" key) keep loading (see
_sceneobject_dict_to_instance_dict). EngineBridge's own save/load for the
disconnected demo path is untouched.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

import datamodel_schema
from shared import object_registry
from shared.instance import (
    is_valid_vector3,
    new_instance_id,
    sanitize_part_properties,
)
from shared.object_registry import ROOT_SERVICES, sanitize_properties_for_type

# ============================================================
# CONSTANTS
# ============================================================

SCENE_FORMAT = "nebula-scene"
# Must stay equal to studio_editor_live.SCENE_FORMAT_VERSION -- both describe
# the same `.nebula.json` envelope; only the shape of "objects" differs
# between the offline SceneObject path and this module's Instance path, and
# open() tells them apart per-file (see _looks_like_legacy_scene_object).
SCENE_FORMAT_VERSION = 3

PROJECT_METADATA_FILENAME = "project.sstudio.json"
PROJECT_METADATA_FORMAT_VERSION = 1
API_VERSION = "0.1"

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_PROJECTS_DIR_NAME = "SStudio Projects"
PRIMARY_PLACE_FILENAME = "Main.nebula.json"

MAX_RECENTS = 20
_SETTINGS_ORG = "LegitsEngine"
_SETTINGS_APP = "Pick A Door Studio"
_RECENTS_SETTINGS_KEY = "places/recent"

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}
MAX_NAME_LENGTH = 80


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class PlaceOperationResult:
    """Uniform return shape for every PlaceManager operation. `objects` is
    always the Instance-shaped list ready to hand to REPLACE_WORLD (create/
    open) or is exactly what was written (save/save_as); callers must not
    apply anything unless `success` is True.

    create_from_template()/open() deliberately do NOT update PlaceManager's
    own current_path/display_name/etc -- the actual world replacement is a
    separate, rejectable network round trip (REPLACE_WORLD), and a
    rejected create/open must leave the current Place completely
    unchanged (spec section 8/17). display_name/project_dir/template_id
    here are what the caller should pass to commit() once (and only once)
    the server has confirmed success."""

    success: bool
    message: str = ""
    path: Optional[Path] = None
    objects: Optional[list[dict[str, Any]]] = None
    display_name: Optional[str] = None
    project_dir: Optional[Path] = None
    template_id: Optional[str] = None
    # Stage 3.8: always a COMPLETE, schema-defaulted services snapshot on
    # success (create_from_template()/open() both fill in every missing
    # service/property via datamodel_schema.sanitize_services_snapshot()) --
    # never partial, so callers can hand this straight to REPLACE_WORLD
    # without any extra merging of their own.
    services: Optional[dict[str, dict[str, Any]]] = None


@dataclass
class PlaceMetadata:
    format_version: int
    project_name: str
    primary_place: str
    created_at: float
    modified_at: float
    template_id: Optional[str] = None
    api_version: str = API_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "project_name": self.project_name,
            "primary_place": self.primary_place,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "template_id": self.template_id,
            "api_version": self.api_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaceMetadata":
        return cls(
            format_version=int(data.get("format_version", PROJECT_METADATA_FORMAT_VERSION)),
            project_name=str(data.get("project_name", "")),
            primary_place=str(data.get("primary_place", PRIMARY_PLACE_FILENAME)),
            created_at=float(data.get("created_at", 0.0)),
            modified_at=float(data.get("modified_at", 0.0)),
            template_id=data.get("template_id"),
            api_version=str(data.get("api_version", API_VERSION)),
        )


@dataclass
class RecentPlaceEntry:
    path: str
    display_name: str
    last_opened: float
    template_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "display_name": self.display_name,
            "last_opened": self.last_opened,
            "template_id": self.template_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecentPlaceEntry":
        return cls(
            path=str(data["path"]),
            display_name=str(data.get("display_name") or Path(str(data["path"])).stem),
            last_opened=float(data.get("last_opened", 0.0)),
            template_id=data.get("template_id"),
        )

    def exists(self) -> bool:
        try:
            return Path(self.path).is_file()
        except OSError:
            return False


# ============================================================
# PATH / NAME VALIDATION
# ============================================================

def default_projects_root() -> Path:
    return Path.home() / "Documents" / DEFAULT_PROJECTS_DIR_NAME


def validate_place_name(name: str) -> tuple[bool, str]:
    cleaned = name.strip()
    if not cleaned:
        return False, "Name cannot be empty."
    if cleaned in (".", ".."):
        return False, "Invalid name."
    if len(cleaned) > MAX_NAME_LENGTH:
        return False, f"Name is too long (max {MAX_NAME_LENGTH} characters)."
    if _INVALID_FILENAME_CHARS.search(cleaned):
        return False, 'Name cannot contain any of: < > : " / \\ | ? *'
    if cleaned.rstrip(".").upper() in _RESERVED_WINDOWS_NAMES:
        return False, f"'{cleaned}' is a reserved name on Windows."
    if cleaned.endswith((" ", ".")):
        return False, "Name cannot end with a space or a period."
    return True, ""


def is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# ============================================================
# ATOMIC JSON WRITE (see Stage 3.2 spec section 7)
# ============================================================

def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Validate the temp file is genuinely valid JSON before it ever
        # touches the destination -- a half-written or corrupt temp file
        # must never replace a good existing Place.
        with open(tmp_path, "r", encoding="utf-8") as handle:
            json.load(handle)
        os.replace(tmp_path, str(path))  # atomic on both POSIX and Windows
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _place_envelope(objects: list[dict[str, Any]], services: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    envelope: dict[str, Any] = {"format": SCENE_FORMAT, "version": SCENE_FORMAT_VERSION, "objects": objects}
    # Stage 3.8: "services" always written from here on (format version 3)
    # -- a version-2 reader simply never looks at the extra key, and
    # open() below always produces a complete, schema-defaulted dict
    # regardless of what's actually on disk, so there's no reason to ever
    # omit it once we're writing at all.
    envelope["services"] = services if services is not None else datamodel_schema.sanitize_services_snapshot(None)
    return envelope


# ============================================================
# LEGACY (offline SceneObject-shaped) SCENE COMPATIBILITY
# ============================================================

def _looks_like_legacy_scene_object(objects: list[Any]) -> bool:
    """SceneObject-shaped dicts (the pre-3.2 offline-demo `.nebula.json`
    format) always carry "object_type"; Instance-shaped dicts (this
    module's format) always carry "class_name". Checking the first
    well-formed entry is enough to tell an old file from a new one."""
    for item in objects:
        if isinstance(item, dict):
            return "object_type" in item and "class_name" not in item
    return False


def _vec3_from_legacy(value: Any) -> list[float]:
    if isinstance(value, dict):
        return [float(value.get("x", 0.0)), float(value.get("y", 0.0)), float(value.get("z", 0.0))]
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return [float(value[0]), float(value[1]), float(value[2])]
    return [0.0, 0.0, 0.0]


def _hex_to_rgb_legacy(value: Any) -> list[int]:
    cleaned = str(value or "#ffffff").strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(ch * 2 for ch in cleaned)
    if len(cleaned) != 6:
        return [255, 255, 255]
    try:
        return [int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)]
    except ValueError:
        return [255, 255, 255]


def _sceneobject_dict_to_instance_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Best-effort upgrade of one legacy SceneObject-shaped dict (flattened
    position/rotation/size/color/... fields) into this module's
    Instance-shaped dict (everything folded into `properties`). Only used
    for opening old files; new Places never round-trip through here."""
    object_type = str(item.get("object_type", "Part"))
    properties: dict[str, Any] = dict(item.get("properties", {}))
    definition = object_registry.get_object_type(object_type)

    if definition is not None and definition.has_3d_entity:
        properties.setdefault("Position", _vec3_from_legacy(item.get("position")))
        properties.setdefault("Rotation", _vec3_from_legacy(item.get("rotation")))
        properties.setdefault("Size", _vec3_from_legacy(item.get("size")))
        properties.setdefault("Color", _hex_to_rgb_legacy(item.get("color")))
        properties.setdefault("Material", str(item.get("material", "Plastic")))
        properties.setdefault("Transparency", float(item.get("transparency", 0.0)))
        properties.setdefault("Anchored", bool(item.get("anchored", False)))
        properties.setdefault("CanCollide", bool(item.get("can_collide", True)))

    if object_type == "Model":
        properties.setdefault("PivotPosition", _vec3_from_legacy(item.get("pivot_position")))
        properties.setdefault("PivotRotation", _vec3_from_legacy(item.get("pivot_rotation")))
        properties.setdefault("PivotIsExplicit", bool(item.get("pivot_is_explicit", False)))

    attributes = dict(item.get("attributes", {}))
    attributes.pop("server_id", None)  # was only ever a live-session mirror, never authoritative

    return {
        "id": str(item.get("id") or new_instance_id()),
        "class_name": object_type,
        "name": str(item.get("name", object_type)),
        "parent_id": item.get("parent"),
        "properties": properties,
        "tags": [str(t) for t in item.get("tags", []) if isinstance(t, str)],
        "attributes": attributes,
        "enabled": bool(item.get("enabled", True)),
    }


# ============================================================
# SANITIZATION / HIERARCHY VALIDATION (Instance-shaped)
# ============================================================

def _sanitize_instance_dict(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Mirrors server.py's own create_part/update_property sanitization so a
    hand-edited or malicious Place file can never inject an out-of-schema
    property, unknown ClassName, or non-finite transform -- the exact same
    trust boundary the server already applies to network input."""
    class_name = str(raw.get("class_name", "")).strip()
    definition = object_registry.get_object_type(class_name)
    if definition is None:
        return None

    raw_properties = raw.get("properties", {})
    if not isinstance(raw_properties, dict):
        raw_properties = {}

    if class_name in ("Part", "SpawnPoint"):
        clean_properties = {**definition.default_properties, **sanitize_part_properties(raw_properties)}
    else:
        clean_properties = {**definition.default_properties, **sanitize_properties_for_type(class_name, raw_properties)}

    name = str(raw.get("name", "")).strip() or definition.display_name
    instance_id = str(raw.get("id") or new_instance_id())
    parent_id = raw.get("parent_id")
    parent_id = str(parent_id) if parent_id is not None else None
    tags = [str(t) for t in raw.get("tags", []) if isinstance(t, str)][:32]
    attributes_raw = raw.get("attributes", {})
    attributes = dict(attributes_raw) if isinstance(attributes_raw, dict) else {}

    return {
        "id": instance_id,
        "class_name": class_name,
        "name": name,
        "parent_id": parent_id,
        "properties": clean_properties,
        "tags": tags,
        "attributes": attributes,
        "enabled": bool(raw.get("enabled", True)),
    }


def _validate_hierarchy(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rejects a missing-parent or cyclic parent chain by resetting it to
    Workspace, exactly mirroring studio_editor_live._sanitize_hierarchy's
    policy for the offline path -- a corrupted Place file degrades to a
    flat-under-Workspace layout instead of an unloadable/broken tree."""
    by_id = {obj["id"]: obj for obj in objects}
    for obj in objects:
        parent_key = obj.get("parent_id") or "Workspace"
        if parent_key not in ROOT_SERVICES and parent_key not in by_id:
            obj["parent_id"] = "Workspace"
            continue
        visited = {obj["id"]}
        walker = parent_key
        while walker in by_id:
            if walker in visited:
                obj["parent_id"] = "Workspace"
                break
            visited.add(walker)
            walker = by_id[walker].get("parent_id") or "Workspace"
    return objects


def _remap_ids(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stage 3.2 spec section 16: every template instantiation gets fresh,
    independent instance ids -- creating the same template twice must
    produce two unrelated Places. Root-service parents (Workspace, ...) and
    already-missing parents are left as-is; only ids that exist IN this
    object list get remapped, so parent references stay internally
    consistent."""
    id_map = {
        str(obj["id"]): new_instance_id()
        for obj in objects
        if obj.get("id")
    }
    remapped: list[dict[str, Any]] = []
    for obj in objects:
        new_obj = dict(obj)
        old_id = str(obj.get("id", ""))
        new_obj["id"] = id_map.get(old_id, new_instance_id())
        parent_id = obj.get("parent_id")
        if parent_id is not None and str(parent_id) in id_map:
            new_obj["parent_id"] = id_map[str(parent_id)]
        remapped.append(new_obj)
    return remapped


# ============================================================
# TEMPLATE REPOSITORY
# ============================================================

class TemplateRepository:
    """Reads templates/<id>.nebula.json on demand -- the attached template-
    browser UI's TemplateSpec.payload is NOT authoritative content (it's
    empty in DEFAULT_TEMPLATES); this is where the real scene data lives,
    per Stage 3.2 spec section 14."""

    def __init__(self, templates_dir: Path = TEMPLATES_DIR) -> None:
        self.templates_dir = Path(templates_dir)

    def available_ids(self) -> list[str]:
        if not self.templates_dir.is_dir():
            return []
        # Path.stem only strips ONE suffix ("blank.nebula.json" -> "blank.
        # nebula"), not both -- the ".nebula.json" double extension needs
        # an explicit strip of the full suffix.
        return sorted(
            p.name[: -len(".nebula.json")]
            for p in self.templates_dir.glob("*.nebula.json")
        )

    def _template_path(self, template_id: str) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_]", "", template_id)
        return self.templates_dir / f"{safe_id}.nebula.json"

    def load_raw_objects(self, template_id: str) -> list[dict[str, Any]]:
        path = self._template_path(template_id)
        if not path.is_file():
            raise FileNotFoundError(f"No template file for '{template_id}' at {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        objects = raw.get("objects", [])
        if not isinstance(objects, list):
            raise ValueError(f"Template '{template_id}' has no valid 'objects' list.")
        if _looks_like_legacy_scene_object(objects):
            objects = [_sceneobject_dict_to_instance_dict(o) for o in objects if isinstance(o, dict)]
        return [o for o in objects if isinstance(o, dict)]

    def instantiate(self, template_id: str) -> list[dict[str, Any]]:
        """Returns a fresh, independent, fully-sanitized Instance-shaped
        object list -- fresh ids, validated hierarchy, every property run
        through the same sanitizer the server itself uses."""
        raw_objects = self.load_raw_objects(template_id)
        sanitized = []
        for item in raw_objects:
            clean = _sanitize_instance_dict(item)
            if clean is not None:
                sanitized.append(clean)
        sanitized = _remap_ids(sanitized)
        sanitized = _validate_hierarchy(sanitized)
        return sanitized


# ============================================================
# RECENT PLACES STORE
# ============================================================

class RecentPlacesStore:
    """QSettings is a plain local INI-backed key/value store (no widgets, no
    engine state) -- see module docstring for why this is the one Qt import
    allowed in this file."""

    def __init__(self) -> None:
        from PySide6.QtCore import QSettings

        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

    def list(self) -> list[RecentPlaceEntry]:
        raw = self._settings.value(_RECENTS_SETTINGS_KEY, [])
        if not isinstance(raw, list):
            return []
        entries: list[RecentPlaceEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(RecentPlaceEntry.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        entries.sort(key=lambda entry: entry.last_opened, reverse=True)
        return entries

    def _write(self, entries: list[RecentPlaceEntry]) -> None:
        self._settings.setValue(_RECENTS_SETTINGS_KEY, [entry.to_dict() for entry in entries])

    def add(self, path: str | Path, display_name: str, template_id: Optional[str] = None) -> None:
        canonical = str(Path(path).expanduser().resolve())
        entries = [entry for entry in self.list() if entry.path != canonical]
        entries.insert(0, RecentPlaceEntry(
            path=canonical, display_name=display_name, last_opened=time.time(), template_id=template_id,
        ))
        self._write(entries[:MAX_RECENTS])

    def remove(self, path: str | Path) -> None:
        canonical = str(Path(path).expanduser().resolve())
        self._write([entry for entry in self.list() if entry.path != canonical])


# ============================================================
# PLACE MANAGER
# ============================================================

class PlaceManager:
    """Owns current-project/current-Place state as plain data (path, display
    name, template id, dirty/saved revision) and every create/open/save
    operation as pure disk I/O + validation. Never touches Qt widgets,
    Ursina Entities, Bullet bodies, or Lua coroutines -- studio_editor_live.
    py/client_studio.py call these methods and apply the resulting
    Instance-shaped object list themselves (via REPLACE_WORLD for create/
    open, or by reading the live authoritative instances for save)."""

    def __init__(self, projects_root: Optional[Path] = None) -> None:
        self.projects_root = Path(projects_root) if projects_root is not None else default_projects_root()
        self.templates = TemplateRepository()
        self.recents = RecentPlacesStore()

        self.current_path: Optional[Path] = None
        self.current_project_dir: Optional[Path] = None
        self.display_name: str = "Untitled Place"
        self.template_id: Optional[str] = None

        # Dirty tracking per Stage 3.2 spec section 10: an authoritative
        # revision counter vs. the revision at last successful save, NOT a
        # local Undo-stack-derived flag -- a remote client's confirmed edit
        # must dirty the Place too, and CommandManager only knows about
        # locally-issued commands.
        self._authoritative_revision: int = 0
        self._saved_revision: int = 0

    # ---------------- dirty state ----------------

    @property
    def is_dirty(self) -> bool:
        return self._authoritative_revision != self._saved_revision

    def mark_authoritative_edit(self) -> None:
        """Call on every confirmed authoritative mutation (create/delete/
        rename/reparent/transform/property edit/Source save), whether it
        originated locally or from a remote client -- see spec section 10."""
        self._authoritative_revision += 1

    def _mark_saved(self) -> None:
        self._saved_revision = self._authoritative_revision

    # ---------------- create / open / save ----------------

    def create_from_template(self, template_id: str, name: str, directory: str | Path) -> PlaceOperationResult:
        ok, reason = validate_place_name(name)
        if not ok:
            return PlaceOperationResult(False, reason)

        try:
            objects = self.templates.instantiate(template_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
            return PlaceOperationResult(False, f"Could not load template '{template_id}': {error}")

        try:
            base_dir = Path(directory).expanduser().resolve()
        except OSError as error:
            return PlaceOperationResult(False, f"Invalid destination folder: {error}")

        project_dir = base_dir / name.strip()
        if project_dir.exists():
            return PlaceOperationResult(
                False, f"'{project_dir}' already exists. Choose a different name or location.",
            )

        try:
            project_dir.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            return PlaceOperationResult(False, f"Could not create project folder: {error}")

        place_path = project_dir / PRIMARY_PLACE_FILENAME
        now = time.time()
        metadata = PlaceMetadata(
            format_version=PROJECT_METADATA_FORMAT_VERSION,
            project_name=name.strip(),
            primary_place=PRIMARY_PLACE_FILENAME,
            created_at=now,
            modified_at=now,
            template_id=template_id,
        )
        # Stage 3.8: templates don't (yet) define their own service values --
        # a freshly created Place always starts from full schema defaults,
        # same as datamodel_schema.default_properties() would give any
        # brand-new instance of a class with no template.
        default_services = datamodel_schema.sanitize_services_snapshot(None)
        try:
            _atomic_write_json(place_path, _place_envelope(objects, default_services))
            _atomic_write_json(project_dir / PROJECT_METADATA_FILENAME, metadata.to_dict())
        except Exception as error:
            return PlaceOperationResult(False, f"Could not write project files: {error}")

        display_name = name.strip()
        return PlaceOperationResult(
            True, f"Created '{display_name}'.", path=place_path, objects=objects,
            display_name=display_name, project_dir=project_dir, template_id=template_id,
            services=default_services,
        )

    def open(self, path: str | Path) -> PlaceOperationResult:
        try:
            place_path = Path(path).expanduser().resolve()
        except OSError as error:
            return PlaceOperationResult(False, f"Invalid Place path: {error}")

        if not place_path.is_file():
            return PlaceOperationResult(False, f"'{place_path}' does not exist.")

        try:
            raw_text = place_path.read_text(encoding="utf-8")
        except OSError as error:
            return PlaceOperationResult(False, f"Could not read '{place_path.name}': {error}")

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as error:
            return PlaceOperationResult(False, f"'{place_path.name}' is not valid JSON: {error}")

        if not isinstance(raw, dict):
            return PlaceOperationResult(False, "Place file is not a valid scene document.")

        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version > SCENE_FORMAT_VERSION or version < 1:
            return PlaceOperationResult(False, f"Unsupported Place format version: {version!r}.")

        raw_objects = raw.get("objects")
        if not isinstance(raw_objects, list):
            return PlaceOperationResult(False, "Place file has no valid 'objects' list.")

        if _looks_like_legacy_scene_object(raw_objects):
            raw_objects = [_sceneobject_dict_to_instance_dict(o) for o in raw_objects if isinstance(o, dict)]

        sanitized: list[dict[str, Any]] = []
        for item in raw_objects:
            if not isinstance(item, dict):
                continue
            clean = _sanitize_instance_dict(item)
            if clean is not None:
                sanitized.append(clean)
        sanitized = _validate_hierarchy(sanitized)

        # Stage 3.8: version-3+ Places carry a "services" key; a version-2
        # Place (or a version-3 file with a malformed/missing one) simply
        # has no raw values to sanitize -- sanitize_services_snapshot(None)
        # still returns a COMPLETE, schema-defaulted snapshot either way,
        # which is what "missing service values use descriptor defaults"
        # and "saving an old Place upgrades it cleanly" require.
        services = datamodel_schema.sanitize_services_snapshot(raw.get("services"))

        project_dir = place_path.parent
        metadata_path = project_dir / PROJECT_METADATA_FILENAME
        display_name = place_path.stem
        template_id: Optional[str] = None
        if metadata_path.is_file():
            try:
                metadata = PlaceMetadata.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
                display_name = metadata.project_name or display_name
                template_id = metadata.template_id
            except (json.JSONDecodeError, OSError, KeyError, ValueError):
                pass

        return PlaceOperationResult(
            True, f"Loaded '{place_path.name}'.", path=place_path, objects=sanitized,
            display_name=display_name, project_dir=project_dir, template_id=template_id,
            services=services,
        )

    def commit(self, result: PlaceOperationResult) -> None:
        """Call ONLY once the caller's REPLACE_WORLD request for this
        create_from_template()/open() result has been confirmed by the
        server. Updates current_path/display_name/template_id, resets the
        dirty-revision counters (a freshly created/opened Place starts
        clean), and records it in Recents. Never call this on a result
        the server rejected -- see PlaceOperationResult's docstring."""
        if not result.success or result.path is None:
            return
        self.current_path = result.path
        self.current_project_dir = result.project_dir or result.path.parent
        self.display_name = result.display_name or result.path.stem
        self.template_id = result.template_id
        self._authoritative_revision = 0
        self._saved_revision = 0
        self.recents.add(self.current_path, self.display_name, self.template_id)

    def save(
        self, objects: list[dict[str, Any]], services: dict[str, dict[str, Any]] | None = None,
    ) -> PlaceOperationResult:
        if self.current_path is None:
            return PlaceOperationResult(False, "No current Place path -- use Save As.")
        return self.save_as(self.current_path, objects, services)

    def save_as(
        self,
        path: str | Path,
        objects: list[dict[str, Any]],
        services: dict[str, dict[str, Any]] | None = None,
    ) -> PlaceOperationResult:
        try:
            place_path = Path(path).expanduser().resolve()
        except OSError as error:
            return PlaceOperationResult(False, f"Invalid destination: {error}")

        # Stage 3.8: `services` is whatever the caller's CURRENT live
        # session state is (bridge.export_services(), mirroring
        # export_world()) -- sanitize_services_snapshot() still fills in
        # any service the caller's dict happens to omit, so Save can never
        # write a partial/invalid snapshot even if the caller only passes
        # a subset.
        clean_services = datamodel_schema.sanitize_services_snapshot(services)
        try:
            _atomic_write_json(place_path, _place_envelope(objects, clean_services))
        except Exception as error:
            return PlaceOperationResult(False, f"Save failed: {error}")

        self.current_path = place_path
        self.current_project_dir = place_path.parent
        if self.display_name in ("", "Untitled Place"):
            self.display_name = place_path.stem
        self._mark_saved()

        self.recents.add(place_path, self.display_name, self.template_id)
        return PlaceOperationResult(
            True, f"Saved '{place_path.name}'.", path=place_path, objects=objects, services=clean_services,
        )

    def reset_to_untitled(self) -> None:
        """Return to Start Page / New Place bookkeeping reset -- does not
        touch any file, just this manager's notion of "current Place"."""
        self.current_path = None
        self.current_project_dir = None
        self.display_name = "Untitled Place"
        self.template_id = None
        self._authoritative_revision = 0
        self._saved_revision = 0


# ============================================================
# CREATE-PLACE DIALOG (Stage 3.2 spec section 13)
# ============================================================

class PlaceCreateDialog(QDialog):
    """Small native dialog shown when a template card is activated: Name,
    Location, a live full-path preview, Create/Cancel. Validates via
    validate_place_name() and refuses to accept an already-existing
    destination -- no silent overwrite. Holds no PlaceManager state; the
    caller reads result_name()/result_directory() after exec() and calls
    PlaceManager.create_from_template() itself."""

    def __init__(
        self,
        template_name: str,
        default_root: Path,
        default_name: str = "MyPlace",
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Create Place — {template_name}")
        self.setMinimumWidth(440)
        self._default_root = Path(default_root)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(self._unique_default_name(default_name))
        form.addRow("Name:", self.name_edit)

        location_row = QHBoxLayout()
        self.location_edit = QLineEdit(str(self._default_root))
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse)
        location_row.addWidget(self.location_edit, 1)
        location_row.addWidget(browse_button)
        form.addRow("Location:", location_row)
        layout.addLayout(form)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #ef6b6b;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.create_button = buttons.addButton("Create", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self.create_button.clicked.connect(self._try_accept)
        layout.addWidget(buttons)

        self.name_edit.textChanged.connect(self._update_preview)
        self.location_edit.textChanged.connect(self._update_preview)
        self._update_preview()

    def _unique_default_name(self, base: str) -> str:
        candidate = base
        index = 2
        while (self._default_root / candidate).exists():
            candidate = f"{base}{index}"
            index += 1
        return candidate

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose Location", self.location_edit.text() or str(self._default_root),
        )
        if chosen:
            self.location_edit.setText(chosen)

    def _current_destination(self) -> Path:
        location = self.location_edit.text().strip() or str(self._default_root)
        name = self.name_edit.text().strip() or "…"
        return Path(location) / name

    def _update_preview(self) -> None:
        self.preview_label.setText(str(self._current_destination()))
        self.error_label.setVisible(False)

    def _try_accept(self) -> None:
        ok, reason = validate_place_name(self.name_edit.text())
        if not ok:
            self.error_label.setText(reason)
            self.error_label.setVisible(True)
            return
        destination = self._current_destination()
        if destination.exists():
            self.error_label.setText(
                f"'{destination}' already exists. Choose a different name or location."
            )
            self.error_label.setVisible(True)
            return
        self.accept()

    def result_name(self) -> str:
        return self.name_edit.text().strip()

    def result_directory(self) -> Path:
        return Path(self.location_edit.text().strip() or str(self._default_root))


# ============================================================
# RECENT PLACES DIALOG (Stage 3.2 spec section 19)
# ============================================================

class RecentPlacesDialog(QDialog):
    """Small native list dialog for the sidebar's "Recent" section. The
    attached template-browser page (sstudio_templates.py) is a template
    grid with no recents-list view of its own, and this stage intentionally
    does not redesign that page -- this is a separate, minimal native
    dialog instead, following the same pattern as PlaceCreateDialog.
    Missing files are shown greyed-out with "(missing)" rather than
    hidden or auto-removed. Removing an entry takes effect immediately
    (no separate "Apply"); opening one accepts the dialog with the chosen
    path available via chosen_path()."""

    def __init__(self, recents_store: RecentPlacesStore, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recent Places")
        self.setMinimumSize(480, 320)
        self._store = recents_store
        self._chosen_path: Optional[str] = None

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._open_selected)
        layout.addWidget(self.list_widget, 1)

        button_row = QHBoxLayout()
        open_button = QPushButton("Open")
        open_button.clicked.connect(self._open_selected)
        remove_button = QPushButton("Remove from Recents")
        remove_button.clicked.connect(self._remove_selected)
        button_row.addWidget(open_button)
        button_row.addWidget(remove_button)
        button_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        button_row.addWidget(buttons)
        layout.addLayout(button_row)

        self._reload()

    def _reload(self) -> None:
        self.list_widget.clear()
        for entry in self._store.list():
            missing = not entry.exists()
            label = f"{entry.display_name}  —  {entry.path}"
            if missing:
                label += "  (missing)"
            item = QListWidgetItem(label)
            item.setData(1, entry.path)
            if missing:
                item.setForeground(self._missing_color())
            self.list_widget.addItem(item)

    @staticmethod
    def _missing_color() -> Any:
        from PySide6.QtGui import QColor

        return QColor("#8a8f96")

    def _selected_path(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return str(item.data(1))

    def _open_selected(self, *_args: Any) -> None:
        path = self._selected_path()
        if path is None:
            return
        self._chosen_path = path
        self.accept()

    def _remove_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        self._store.remove(path)
        self._reload()

    def chosen_path(self) -> Optional[str]:
        return self._chosen_path
