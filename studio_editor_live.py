#!/usr/bin/env python3
"""
Studio-style editor shell for a Python game engine.

Run:
    pip install PySide6
    python studio_editor.py

Integration points:
    1. Create your engine QWidget and call:
           window.install_engine_viewport(engine_widget)

    2. Optionally attach an adapter object:
           window.bridge.set_adapter(adapter)

       Supported optional adapter methods:
           on_scene_replaced(objects)
           on_object_added(scene_object)
           on_object_deleted(object_id)
           on_object_selected(object_id)
           on_property_changed(object_id, property_path, value)
           play()
           stop()

The editor works without an engine attached and includes a mock viewport.
"""

from __future__ import annotations

import json
import math
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
    QObject,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QFont,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import place_manager
import script_editor
import datamodel_schema
from shared import object_registry, transform_math
from shared.object_registry import ObjectTypeDefinition, ROOT_SERVICES


APP_NAME = "Pick A Door Studio"
ORG_NAME = "LegitsEngine"
SCENE_FILE_FILTER = "Nebula Scene (*.nebula.json);;JSON files (*.json);;All files (*)"
BUILD_ID = "2026-08-01-live-client"
SCENE_FORMAT_VERSION = 2


# ---------------------------------------------------------------------------
# Scene model
# ---------------------------------------------------------------------------

@dataclass
class Vec3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    @classmethod
    def from_value(cls, value: Any) -> "Vec3":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                float(value.get("x", 0.0)),
                float(value.get("y", 0.0)),
                float(value.get("z", 0.0)),
            )
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return cls(float(value[0]), float(value[1]), float(value[2]))
        return cls()


@dataclass
class SceneObject:
    name: str
    object_type: str = "Part"
    parent: str = "Workspace"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    position: Vec3 = field(default_factory=Vec3)
    rotation: Vec3 = field(default_factory=Vec3)
    scale: Vec3 = field(default_factory=lambda: Vec3(1.0, 1.0, 1.0))
    size: Vec3 = field(default_factory=lambda: Vec3(4.0, 4.0, 4.0))
    color: str = "#a3a3a6"
    material: str = "Plastic"
    transparency: float = 0.0
    reflectance: float = 0.0
    anchored: bool = False
    can_collide: bool = True
    cast_shadow: bool = True
    locked: bool = False
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    # Stage 2.2: Model's persistent world-space pivot transform. Dedicated
    # typed fields (not stuffed into `properties`) specifically so the
    # existing position/rotation live-drag Inspector path (InspectorPanel.
    # _live_vector_editors / _external_property_changed's isinstance(value,
    # Vec3) fast path) works for pivot editing with zero changes to that
    # mechanism — see Stage 2.2 report for the full design. Meaningless
    # (stays at defaults) for any object_type other than "Model".
    pivot_position: Vec3 = field(default_factory=Vec3)
    pivot_rotation: Vec3 = field(default_factory=Vec3)
    pivot_is_explicit: bool = False
    # Generic bag for type-specific data that doesn't fit the Part-shaped
    # fields above (Script.Source, PointLight.Brightness, ...). Keyed the
    # same way as Instance.properties on the server side.
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SceneObject":
        allowed = {
            "name", "object_type", "parent", "id", "position", "rotation", "scale",
            "size", "color", "material", "transparency", "reflectance", "anchored",
            "can_collide", "cast_shadow", "locked", "enabled", "tags", "attributes",
            "pivot_position", "pivot_rotation", "pivot_is_explicit", "properties",
        }
        clean = {key: value for key, value in data.items() if key in allowed}
        clean["position"] = Vec3.from_value(clean.get("position"))
        clean["rotation"] = Vec3.from_value(clean.get("rotation"))
        clean["scale"] = Vec3.from_value(clean.get("scale", {"x": 1, "y": 1, "z": 1}))
        clean["size"] = Vec3.from_value(clean.get("size", {"x": 4, "y": 4, "z": 4}))
        clean["pivot_position"] = Vec3.from_value(clean.get("pivot_position"))
        clean["pivot_rotation"] = Vec3.from_value(clean.get("pivot_rotation"))
        clean["pivot_is_explicit"] = bool(clean.get("pivot_is_explicit", False))
        return cls(**clean)


def _build_service_scene_objects(
    services_state: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, "SceneObject"]:
    """Stage 3.8: one SceneObject per registered root service, id ==
    ClassName == the service's own name (e.g. "Workspace") -- kept in
    EngineBridge.services, a dict SEPARATE from EngineBridge.objects (root
    services are not ordinary scene objects; ExplorerPanel.rebuild()
    already builds their tree-root QTreeWidgetItems directly from
    shared.object_registry.ROOT_SERVICES, never from self.objects). All
    service-specific data lives in SceneObject.properties, the same
    generic bag Script.Source/PointLight.Brightness already use -- no new
    fields needed on SceneObject itself."""
    result: dict[str, SceneObject] = {}
    for descriptor in datamodel_schema.get_all_classes():
        if not descriptor.service:
            continue
        properties = datamodel_schema.default_properties(descriptor.class_name)
        if services_state is not None and descriptor.class_name in services_state:
            properties.update(services_state[descriptor.class_name])
        result[descriptor.class_name] = SceneObject(
            id=descriptor.class_name,
            name=descriptor.class_name,
            object_type=descriptor.class_name,
            parent="",
            properties=properties,
        )
    return result


def demo_scene() -> list[SceneObject]:
    # Parent-хранение по id (см. SceneObject.parent) — Models/PinkMarkers -
    # реальные Folder/Model-объекты, а не строки-пути, как раньше.
    models_folder = SceneObject(name="Models", object_type="Folder", parent="Workspace")
    grey_block_b_model = SceneObject(
        name="GreyBlock_B", object_type="Model", parent=models_folder.id,
    )
    grey_block_c_model = SceneObject(
        name="GreyBlock_C", object_type="Model", parent=models_folder.id,
    )
    pink_markers_folder = SceneObject(name="PinkMarkers", object_type="Folder", parent="Workspace")

    return [
        SceneObject(
            name="Camera",
            object_type="Camera",
            parent="Workspace",
            position=Vec3(18, 18, 24),
            can_collide=False,
            cast_shadow=False,
        ),
        SceneObject(
            name="Terrain",
            object_type="Terrain",
            parent="Workspace",
            color="#a9b5a4",
            anchored=True,
            size=Vec3(128, 1, 128),
        ),
        SceneObject(
            name="Lighting",
            object_type="Lighting",
            parent="Workspace",
            can_collide=False,
            cast_shadow=False,
        ),
        models_folder,
        SceneObject(
            name="GreyBlock_A",
            object_type="Model",
            parent=models_folder.id,
            position=Vec3(-14, 3, -4),
            size=Vec3(5, 8, 5),
            color="#b7b8bb",
            anchored=True,
        ),
        grey_block_b_model,
        SceneObject(
            name="Part",
            object_type="Part",
            parent=grey_block_b_model.id,
            position=Vec3(12, 4, -8),
            size=Vec3(4, 6, 2),
            color="#a3a3a6",
            anchored=False,
        ),
        grey_block_c_model,
        SceneObject(
            name="Part",
            object_type="Part",
            parent=grey_block_c_model.id,
            position=Vec3(0, 4, 4),
            size=Vec3(8, 5, 6),
            color="#c0c1c4",
            anchored=True,
        ),
        pink_markers_folder,
        SceneObject(
            name="PinkMarker_1",
            object_type="Part",
            parent=pink_markers_folder.id,
            position=Vec3(-6, 2, 1),
            size=Vec3(2, 2, 2),
            color="#e8a6aa",
            anchored=True,
        ),
        SceneObject(
            name="PinkMarker_2",
            object_type="Part",
            parent=pink_markers_folder.id,
            position=Vec3(-3, 5, 0),
            size=Vec3(2, 2, 2),
            color="#e8a6aa",
            anchored=True,
        ),
        SceneObject(
            name="PinkMarker_3",
            object_type="Part",
            parent=pink_markers_folder.id,
            position=Vec3(-8, 6, -3),
            size=Vec3(2, 2, 2),
            color="#e8a6aa",
            anchored=True,
        ),
        SceneObject(
            name="Baseplate",
            object_type="Baseplate",
            parent="Workspace",
            size=Vec3(128, 1, 128),
            color="#adb6aa",
            anchored=True,
        ),
    ]


# ---------------------------------------------------------------------------
# Engine bridge
# ---------------------------------------------------------------------------

class EngineBridge(QObject):
    scene_changed = Signal()
    object_added = Signal(str)
    object_deleted = Signal(str)
    selection_changed = Signal(object)
    property_changed = Signal(str, str, object)
    log_message = Signal(str, str)
    play_state_changed = Signal(bool)
    dirty_changed = Signal(bool)
    history_state_changed = Signal()
    lua_diagnostic = Signal(str, str, str, object, int)  # script_id, severity, message, line (Optional[int]), session_id
    lua_session_started = Signal(int)  # session_id -- fires once per Play, whether or not any diagnostic ever follows

    def __init__(
        self,
        objects: Optional[Iterable[SceneObject]] = None,
        *,
        live_mode: bool = False,
    ) -> None:
        super().__init__()
        self.live_mode = bool(live_mode)
        if objects is None:
            self.objects = [] if self.live_mode else demo_scene()
        else:
            self.objects = list(objects)
        # Stage 3.8: root-service selection targets (Workspace, StarterPlayer,
        # ...) -- see _build_service_scene_objects(). Populated with schema
        # defaults immediately so Workspace/StarterPlayer/etc. are already
        # selectable (non-blank Inspector) even before a live server
        # connection exists; MultiplayerStudioAdapter.sync_full_scene()
        # (client_studio.py) overwrites this with the actual synced
        # WORLD_SNAPSHOT services state once connected.
        self.services: dict[str, SceneObject] = _build_service_scene_objects()
        self.selected_id: Optional[str] = None
        self.is_playing = False
        self.is_dirty = False
        self.adapter: Any = None
        # Stage 3.1: lets StudioMainWindow interpose the "unsaved Script
        # Source" prompt in front of EVERY existing way to trigger Play
        # (Ribbon buttons x2, Tests menu/F5, the `:play` console command)
        # without rewiring each call site individually -- they all still
        # just call bridge.play().
        self._play_guard: Optional[Callable[[], bool]] = None

    def set_adapter(self, adapter: Any) -> None:
        self.adapter = adapter
        attach = getattr(adapter, "attach_bridge", None)
        if callable(attach):
            try:
                attach(self)
            except Exception as exc:
                self.log("error", f"Adapter.attach_bridge failed: {exc}")
        self.log("info", f"Engine adapter attached: {type(adapter).__name__}")

    def _adapter_call(self, method_name: str, *args: Any, default: Any = None) -> Any:
        if self.adapter is None:
            return default
        method = getattr(self.adapter, method_name, None)
        if not callable(method):
            return default
        try:
            return method(*args)
        except Exception as exc:
            self.log("error", f"Adapter.{method_name} failed: {exc}")
            return default

    def log(self, level: str, message: str) -> None:
        self.log_message.emit(level.lower(), message)

    def set_dirty(self, dirty: bool) -> None:
        if self.live_mode:
            dirty = False
        dirty = bool(dirty)
        if self.is_dirty == dirty:
            return
        self.is_dirty = dirty
        self.dirty_changed.emit(dirty)

    def get_object(self, object_id: Optional[str]) -> Optional[SceneObject]:
        if not object_id:
            return None
        found = next((obj for obj in self.objects if obj.id == object_id), None)
        if found is not None:
            return found
        # Stage 3.8: root services (Workspace, StarterPlayer, ...) live in
        # a separate dict, never in self.objects -- see
        # _build_service_scene_objects(). Checked second so an ordinary
        # object can never be shadowed by a same-named service (object ids
        # are opaque hex/uuid strings, service ids are the fixed
        # ROOT_SERVICES names, so a collision is not actually possible in
        # practice, but the ordering documents the intended precedence).
        return self.services.get(object_id)

    def sync_services(self, services_state: dict[str, dict[str, Any]]) -> None:
        """Full resync of every root service's properties from the
        authoritative server state -- called from MultiplayerStudioAdapter.
        sync_full_scene() (initial connect, WORLD_SNAPSHOT after Open
        Place/template creation). Rebuilds self.services wholesale (same
        "brand-new authoritative state" semantics as sync_scene() for
        ordinary objects) and, if a service happens to be the current
        selection, re-emits selection_changed so the Inspector refreshes
        with the new values instead of going stale."""
        self.services = _build_service_scene_objects(services_state)
        self.scene_changed.emit()
        if self.selected_id in self.services:
            self.selection_changed.emit(self.services[self.selected_id])

    def sync_service_property(self, service_name: str, properties: dict[str, Any]) -> None:
        """Granular counterpart to sync_services() -- one
        SERVICE_PROPERTY_UPDATED broadcast (including the echo of this
        client's own edit). Mirrors sync_upsert()'s role for ordinary
        Instances: updates the stored SceneObject in place and, if
        selected, notifies the Inspector via property_changed (the SAME
        signal ordinary property edits use -- InspectorPanel/ExplorerPanel
        already know how to handle it generically)."""
        obj = self.services.get(service_name)
        if obj is None:
            return
        obj.properties.update(properties)
        for key, value in properties.items():
            self.property_changed.emit(service_name, f"properties.{key}", value)

    def unique_name(self, base: str, parent: str = "Workspace") -> str:
        """Uniqueness is scoped to siblings under the same parent — same
        convention as Roblox Studio (Part, Part2, Part3, no underscore)."""
        names = {obj.name for obj in self.objects if obj.parent == parent}
        if base not in names:
            return base
        index = 2
        while f"{base}{index}" in names:
            index += 1
        return f"{base}{index}"

    def replace_scene(self, objects: Iterable[SceneObject], mark_dirty: bool = False) -> None:
        self.objects = list(objects)
        if self.selected_id and self.get_object(self.selected_id) is None:
            self.selected_id = None
        self.scene_changed.emit()
        self.selection_changed.emit(self.get_object(self.selected_id))
        self.set_dirty(mark_dirty)

    def sync_scene(self, objects: Iterable[SceneObject]) -> None:
        self.replace_scene(objects, mark_dirty=False)

    def sync_upsert(self, obj: SceneObject) -> None:
        existing = self.get_object(obj.id)
        if existing is None:
            self.objects.append(obj)
            self.object_added.emit(obj.id)
        else:
            index = self.objects.index(existing)
            self.objects[index] = obj
        self.scene_changed.emit()
        if self.selected_id == obj.id:
            self.selection_changed.emit(obj)

    def sync_transform_live(self, object_id: str, field_name: str, value: "Vec3") -> None:
        """Cheap per-frame path for an actively-dragged gizmo transform.

        Deliberately does NOT call sync_upsert()/emit scene_changed or
        selection_changed: those drive ExplorerPanel.rebuild() (tears down
        and rebuilds the entire tree) and InspectorPanel.set_object() (tears
        down and rebuilds every section, recreating every widget) — doing
        that on every rendered frame during a drag is what caused the
        reported jerky ~25-30fps movement, not network throttling or grid
        snapping. This only mutates the existing SceneObject field in place
        and emits property_changed, which ExplorerPanel already ignores for
        non-name paths and InspectorPanel now handles by updating just the
        relevant spinboxes in place (see VectorEditor.set_values_silently).
        """
        obj = self.get_object(object_id)
        if obj is None or not hasattr(obj, field_name):
            return
        setattr(obj, field_name, value)
        self.property_changed.emit(object_id, field_name, value)

    def sync_delete(self, object_id: str) -> None:
        existing = self.get_object(object_id)
        if existing is None:
            return
        self.objects = [obj for obj in self.objects if obj.id != object_id]
        self.object_deleted.emit(object_id)
        if self.selected_id == object_id:
            self.selected_id = None
            self.selection_changed.emit(None)
        self.scene_changed.emit()

    def descendant_ids(self, root_id: str) -> set[str]:
        """All ids transitively parented under root_id — used by both the
        local (non-live) set_parent() validation below and by
        ExplorerPanel's drag-and-drop preview validation, so there is one
        place that defines "descendant" instead of two."""
        children_by_parent: dict[str, list[str]] = {}
        for item in self.objects:
            children_by_parent.setdefault(item.parent or "Workspace", []).append(item.id)
        result: set[str] = set()
        frontier = [root_id]
        while frontier:
            current = frontier.pop()
            for child_id in children_by_parent.get(current, []):
                if child_id not in result:
                    result.add(child_id)
                    frontier.append(child_id)
        return result

    def set_parent(self, object_id: str, parent_key: str) -> bool:
        obj = self.get_object(object_id)
        if obj is None:
            return False

        if self.live_mode:
            accepted = bool(self._adapter_call("set_parent", object_id, parent_key, default=False))
            if not accepted:
                self.log("warning", "The live engine rejected the reparent request.")
            return accepted

        # Non-live/demo mode has no server to validate against — apply the
        # same rules locally (object_registry.is_parent_allowed + cycle/
        # self checks) so the standalone demo behaves like the live client.
        if parent_key == object_id:
            self.log("warning", "An object cannot be its own parent.")
            return False

        if parent_key in ROOT_SERVICES:
            target_type = parent_key
        else:
            target_obj = self.get_object(parent_key)
            if target_obj is None:
                self.log("warning", "Target parent does not exist.")
                return False
            target_type = target_obj.object_type
            if parent_key in self.descendant_ids(object_id):
                self.log("warning", f"Cannot parent '{obj.name}' to its own descendant.")
                return False

        if not object_registry.is_parent_allowed(obj.object_type, target_type):
            self.log("warning", f"'{obj.object_type}' cannot be parented to '{target_type}'.")
            return False

        if parent_key not in ROOT_SERVICES:
            target_definition = object_registry.get_object_type(target_type)
            if target_definition is None or not target_definition.is_container:
                self.log("warning", f"'{target_type}' cannot contain children.")
                return False

        obj.parent = parent_key
        self.scene_changed.emit()
        if self.selected_id == obj.id:
            self.selection_changed.emit(obj)
        self.set_dirty(True)
        self.log("info", f'Reparented {obj.object_type} "{obj.name}" to {parent_key}')
        return True

    _TRANSFORMABLE_TYPES = ("Part", "SpawnPoint", "Model")

    def _transformable_descendant_ids(self, root_id: str) -> list[str]:
        """Like descendant_ids() but filtered to objects that own a
        transform (Part/SpawnPoint Position+Rotation, or a nested Model's
        pivot) — recurses THROUGH non-spatial containers (Folder/Script)
        without including them. Mirrors client_studio.py's
        _collect_transformable_descendants() for the live path."""
        return [
            object_id for object_id in self.descendant_ids(root_id)
            if (obj := self.get_object(object_id)) is not None and obj.object_type in self._TRANSFORMABLE_TYPES
        ]

    def _world_bounds_points(self, descendant_id: str) -> list[tuple[float, float, float]]:
        """Mirrors client_studio.py's _world_bounds_points(): a Part/
        SpawnPoint contributes its full oriented bounding box (8 corners,
        scaled by Size, rotated by world Rotation, translated by world
        Position), not just its center, so the auto-pivot reflects real
        combined world bounds. A nested Model, or a Part-like with a
        missing/invalid Size, has no usable visual bounds and falls back
        to a single point (its world position)."""
        descendant = self.get_object(descendant_id)
        if descendant is None:
            return []

        if descendant.object_type == "Model":
            # Recurse rather than reading pivot_position directly — see
            # the rationale in transform_model()'s matching fix.
            nested_pos, _nested_quat = self._model_pivot_world(descendant)
            return [nested_pos]

        pos = descendant.position
        size = descendant.size
        half = (size.x / 2.0, size.y / 2.0, size.z / 2.0)
        if not all(h >= 0 and math.isfinite(h) for h in half):
            return [(pos.x, pos.y, pos.z)]

        rot = descendant.rotation
        quat = transform_math.from_euler_xyz_degrees((rot.x, rot.y, rot.z))
        corners: list[tuple[float, float, float]] = []
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    local = (sx * half[0], sy * half[1], sz * half[2])
                    rotated = transform_math.rotate_vector(quat, local)
                    corners.append((pos.x + rotated[0], pos.y + rotated[1], pos.z + rotated[2]))
        return corners

    def _model_pivot_world(self, model_obj: SceneObject) -> tuple[tuple[float, float, float], "transform_math.Quat"]:
        if model_obj.pivot_is_explicit:
            position = (model_obj.pivot_position.x, model_obj.pivot_position.y, model_obj.pivot_position.z)
            rotation = (model_obj.pivot_rotation.x, model_obj.pivot_rotation.y, model_obj.pivot_rotation.z)
            return position, transform_math.from_euler_xyz_degrees(rotation)

        points: list[tuple[float, float, float]] = []
        for descendant_id in self._transformable_descendant_ids(model_obj.id):
            points.extend(self._world_bounds_points(descendant_id))
        if not points:
            return (0.0, 0.0, 0.0), transform_math.IDENTITY
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        zs = [p[2] for p in points]
        center = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, (min(zs) + max(zs)) / 2.0)
        return center, transform_math.IDENTITY

    def transform_model(
        self,
        object_id: str,
        pivot_position: list[float] | None,
        pivot_rotation: list[float] | None,
    ) -> bool:
        """Model pivot edit + cascade to every transformable descendant —
        the non-live counterpart of client_studio.py's
        request_transform_model_pivot(). Same math (shared/transform_math.py
        instead of Panda3D Quat, see Stage 2.2 report for why two
        implementations exist), same relative-transform-preserving
        algorithm, so demo mode behaves like the live client."""
        obj = self.get_object(object_id)
        if obj is None or obj.object_type != "Model":
            return False

        if self.live_mode:
            accepted = bool(
                self._adapter_call("transform_model", object_id, pivot_position, pivot_rotation, default=False)
            )
            if not accepted:
                self.log("warning", "The live engine rejected the Model transform request.")
            return accepted

        old_pivot_pos, old_pivot_quat = self._model_pivot_world(obj)
        old_pivot_quat_inv = transform_math.conjugate(old_pivot_quat)

        descendant_ids = self._transformable_descendant_ids(object_id)
        relative: dict[str, tuple[tuple[float, float, float], "transform_math.Quat"]] = {}
        for descendant_id in descendant_ids:
            descendant = self.get_object(descendant_id)
            if descendant is None:
                continue
            if descendant.object_type == "Model":
                # Recurse through _model_pivot_world rather than reading
                # pivot_position directly: an untouched nested Model's true
                # position is its own lazy auto-pivot (bounds-center of its
                # own descendants), not the registry-default (0,0,0) — see
                # the matching fix in client_studio.py's
                # _begin_model_drag_capture for the full rationale.
                child_pos_tuple, child_quat = self._model_pivot_world(descendant)
                child_pos = Vec3(*child_pos_tuple)
            else:
                child_pos = descendant.position
                child_rot = descendant.rotation
                child_quat = transform_math.from_euler_xyz_degrees((child_rot.x, child_rot.y, child_rot.z))
            offset = (child_pos.x - old_pivot_pos[0], child_pos.y - old_pivot_pos[1], child_pos.z - old_pivot_pos[2])
            relative_pos = transform_math.rotate_vector(old_pivot_quat_inv, offset)
            relative_quat = transform_math.compose(old_pivot_quat_inv, child_quat)
            relative[descendant_id] = (relative_pos, relative_quat)

        new_pivot_pos = tuple(pivot_position) if pivot_position is not None else old_pivot_pos
        new_pivot_quat = (
            transform_math.from_euler_xyz_degrees(tuple(pivot_rotation))
            if pivot_rotation is not None
            else old_pivot_quat
        )

        obj.pivot_position = Vec3(*new_pivot_pos)
        obj.pivot_rotation = Vec3(*transform_math.to_euler_xyz_degrees(new_pivot_quat))
        obj.pivot_is_explicit = True

        for descendant_id, (relative_pos, relative_quat) in relative.items():
            descendant = self.get_object(descendant_id)
            if descendant is None:
                continue
            rotated_offset = transform_math.rotate_vector(new_pivot_quat, relative_pos)
            new_pos = (
                new_pivot_pos[0] + rotated_offset[0],
                new_pivot_pos[1] + rotated_offset[1],
                new_pivot_pos[2] + rotated_offset[2],
            )
            new_quat = transform_math.compose(new_pivot_quat, relative_quat)
            new_rot = transform_math.to_euler_xyz_degrees(new_quat)
            if descendant.object_type == "Model":
                descendant.pivot_position = Vec3(*new_pos)
                descendant.pivot_rotation = Vec3(*new_rot)
                descendant.pivot_is_explicit = True
            else:
                descendant.position = Vec3(*new_pos)
                descendant.rotation = Vec3(*new_rot)

        self.scene_changed.emit()
        if self.selected_id == obj.id:
            self.selection_changed.emit(obj)
        self.set_dirty(True)
        self.log("info", f'Transformed Model "{obj.name}" ({len(relative)} descendant(s)).')
        return True

    def undo(self) -> None:
        self._adapter_call("undo", default=False)

    def redo(self) -> None:
        self._adapter_call("redo", default=False)

    def history_state(self) -> dict[str, Any]:
        return self._adapter_call(
            "history_state",
            default={"can_undo": False, "can_redo": False, "undo_text": "", "redo_text": ""},
        )

    def new_scene(self) -> None:
        if self.live_mode:
            handled = bool(self._adapter_call("new_scene", default=False))
            if not handled:
                self.log("warning", "New Scene is disabled while connected to a live server.")
            return

        baseplate = SceneObject(
            name="Baseplate",
            object_type="Baseplate",
            parent="Workspace",
            size=Vec3(128, 1, 128),
            color="#adb6aa",
            anchored=True,
        )
        self.replace_scene([baseplate], mark_dirty=False)
        self.log("info", "New scene created.")

    def select(self, object_id: Optional[str], notify_adapter: bool = True) -> None:
        if object_id is not None and self.get_object(object_id) is None:
            return
        if self.selected_id == object_id:
            return
        self.selected_id = object_id
        self.selection_changed.emit(self.get_object(object_id))
        if notify_adapter:
            self._adapter_call("select_object", object_id)

    def sync_select(self, object_id: Optional[str]) -> None:
        self.select(object_id, notify_adapter=False)

    def add_part(self, object_type: str = "Part", parent: Optional[str] = None) -> Optional[SceneObject]:
        if self.live_mode:
            accepted = bool(self._adapter_call("create_part", object_type, parent, default=False))
            if not accepted:
                self.log("warning", "The live engine rejected the create request.")
            return None

        definition = object_registry.get_object_type(object_type)
        resolved_parent = parent or (definition.default_parent if definition else "Workspace")
        base_name = definition.display_name if definition else object_type
        name = self.unique_name(base_name, resolved_parent)

        default_properties = dict(definition.default_properties) if definition else {}
        kwargs: dict[str, Any] = {}

        if definition is not None and definition.has_3d_entity:
            kwargs["position"] = Vec3(0, 4, 0)
            size = default_properties.pop("Size", None)
            if size:
                kwargs["size"] = Vec3.from_value(size)
            rotation = default_properties.pop("Rotation", None)
            if rotation:
                kwargs["rotation"] = Vec3.from_value(rotation)
            color = default_properties.pop("Color", None)
            if color:
                kwargs["color"] = f"#{int(color[0]):02x}{int(color[1]):02x}{int(color[2]):02x}"
            if "Anchored" in default_properties:
                kwargs["anchored"] = bool(default_properties.pop("Anchored"))
            if "CanCollide" in default_properties:
                kwargs["can_collide"] = bool(default_properties.pop("CanCollide"))
            if "Transparency" in default_properties:
                kwargs["transparency"] = float(default_properties.pop("Transparency"))
            if "Material" in default_properties:
                kwargs["material"] = str(default_properties.pop("Material"))

        obj = SceneObject(
            name=name,
            object_type=object_type,
            parent=resolved_parent,
            properties=default_properties,
            **kwargs,
        )
        self.objects.append(obj)
        self.object_added.emit(obj.id)
        self.scene_changed.emit()
        self.select(obj.id)
        self.set_dirty(True)
        self.log("info", f"Created {object_type} \"{obj.name}\".")
        return obj

    def duplicate_selected(self) -> Optional[SceneObject]:
        source = self.get_object(self.selected_id)
        if source is None:
            return None

        if self.live_mode:
            accepted = bool(self._adapter_call("duplicate_object", source.id, default=False))
            if not accepted:
                self.log("warning", "The live engine rejected the duplicate request.")
            return None

        data = asdict(source)
        data["id"] = uuid.uuid4().hex
        data["name"] = self.unique_name(source.name, source.parent)
        data["position"]["x"] += 2.0
        data["position"]["z"] += 2.0
        duplicate = SceneObject.from_dict(data)
        self.objects.append(duplicate)
        self.scene_changed.emit()
        self.object_added.emit(duplicate.id)
        self.select(duplicate.id)
        self.set_dirty(True)
        self.log("info", f"Duplicated '{source.name}' as '{duplicate.name}'.")
        return duplicate

    def delete_selected(self) -> bool:
        obj = self.get_object(self.selected_id)
        if obj is None:
            return False
        if obj.object_type in {"Baseplate", "Camera", "Lighting", "Terrain"}:
            self.log("warning", f"'{obj.name}' is a system object and cannot be deleted.")
            return False

        if self.live_mode:
            accepted = bool(self._adapter_call("delete_object", obj.id, default=False))
            if not accepted:
                self.log("warning", "The live engine rejected the delete request.")
            return accepted

        deleted_id = obj.id
        deleted_name = obj.name
        self.objects = [item for item in self.objects if item.id != deleted_id]
        self.selected_id = None
        self.object_deleted.emit(deleted_id)
        self.scene_changed.emit()
        self.selection_changed.emit(None)
        self.set_dirty(True)
        self.log("info", f"Deleted '{deleted_name}'.")
        return True

    @staticmethod
    def _read_property(obj: SceneObject, property_path: str) -> Any:
        if property_path.startswith("properties."):
            key = property_path.split(".", 1)[1]
            return obj.properties.get(key)
        if "." in property_path:
            root, leaf = property_path.split(".", 1)
            target = getattr(obj, root, None)
            return getattr(target, leaf, None)
        return getattr(obj, property_path, None)

    @staticmethod
    def _write_property(obj: SceneObject, property_path: str, value: Any) -> bool:
        if property_path.startswith("properties."):
            key = property_path.split(".", 1)[1]
            obj.properties[key] = value
            return True
        if "." in property_path:
            root, leaf = property_path.split(".", 1)
            target = getattr(obj, root, None)
            if isinstance(target, Vec3) and hasattr(target, leaf):
                setattr(target, leaf, float(value))
                return True
            return False
        if hasattr(obj, property_path):
            setattr(obj, property_path, value)
            return True
        return False

    def set_property(self, object_id: str, property_path: str, value: Any) -> None:
        obj = self.get_object(object_id)
        if obj is None:
            return

        # Stage 3.8: root services go through datamodel_schema validation
        # BEFORE anything is written locally -- a rejected edit must leave
        # the old value unchanged, create no history entry, and not dirty
        # the Place (spec), so this has to happen ahead of _write_property()
        # rather than being caught only by the (nonexistent, for services)
        # server reject path ordinary Instance edits rely on.
        is_service = object_id in ROOT_SERVICES
        if is_service and property_path.startswith("properties."):
            key = property_path.split(".", 1)[1]
            result = datamodel_schema.validate_property_value(object_id, key, value)
            if not result.ok:
                self.log("warning", f"{object_id}.{key}: {result.error}")
                return
            value = result.value
            # Stage 3.8 fix: CameraMinZoomDistance/CameraMaxZoomDistance
            # must stay ordered -- a cross-field rule plain
            # validate_property_value() above can't see. Immediate UX-only
            # pre-check against the OTHER bound's currently-known value
            # (obj.properties, kept in sync via sync_service_property());
            # the server remains the actual authority (see
            # server.handle_update_service_property).
            if key in ("CameraMinZoomDistance", "CameraMaxZoomDistance"):
                other_key = "CameraMaxZoomDistance" if key == "CameraMinZoomDistance" else "CameraMinZoomDistance"
                other_value = obj.properties.get(other_key)
                min_value = value if key == "CameraMinZoomDistance" else other_value
                max_value = value if key == "CameraMaxZoomDistance" else other_value
                zoom_result = datamodel_schema.validate_starter_player_zoom(min_value, max_value)
                if not zoom_result.ok:
                    self.log("warning", f"{object_id}.{key}: {zoom_result.error}")
                    return

        old_value = self._read_property(obj, property_path)
        if not self._write_property(obj, property_path, value):
            return

        accepted = True
        if self.live_mode:
            adapter_method = "set_service_property" if is_service else "set_property"
            accepted = bool(
                self._adapter_call(
                    adapter_method,
                    object_id,
                    property_path,
                    value,
                    default=False,
                )
            )

        if not accepted:
            self._write_property(obj, property_path, old_value)
            self.property_changed.emit(object_id, property_path, old_value)
            self.log("warning", f"Property '{property_path}' is not supported by the live client.")
            return

        self.property_changed.emit(object_id, property_path, value)
        self.set_dirty(True)

    def set_play_guard(self, callback: Optional[Callable[[], bool]]) -> None:
        self._play_guard = callback

    def play(self) -> None:
        if self.is_playing:
            return
        if self._play_guard is not None and not self._play_guard():
            return
        accepted = self._adapter_call("play", default=True)
        if accepted is False:
            return
        self.is_playing = True
        self.play_state_changed.emit(True)
        self.log("info", "Play session started.")

    def stop(self) -> None:
        if not self.is_playing and self.live_mode:
            self._adapter_call("stop", default=True)
            return
        if not self.is_playing:
            return
        accepted = self._adapter_call("stop", default=True)
        if accepted is False:
            return
        self.is_playing = False
        self.play_state_changed.emit(False)
        self.log("info", "Play session stopped.")

    def set_transform_mode(self, mode: str) -> None:
        self._adapter_call("set_transform_mode", mode)

    def set_grid_snap(self, value: float) -> None:
        self._adapter_call("set_grid_snap", value)

    def serialize(self) -> dict[str, Any]:
        # system:* объекты — синтетические проекции движка (Camera/
        # Terrain/Lighting/...), которые каждый раз заново строятся из
        # живого состояния игры (см. MultiplayerStudioAdapter._system_objects
        # в client_studio.py). Сохранять их в файл бессмысленно — при
        # загрузке их всё равно перезапишет живой снапшот.
        persistable = [obj for obj in self.objects if not obj.id.startswith("system:")]
        return {
            "format": "nebula-scene",
            "version": SCENE_FORMAT_VERSION,
            "objects": [asdict(obj) for obj in persistable],
        }

    def save_to_file(self, path: str | Path) -> None:
        target = Path(path)
        target.write_text(
            json.dumps(self.serialize(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.set_dirty(False)
        self.log("info", f"Scene snapshot saved: {target.name}")

    @staticmethod
    def _migrate_scene_data(raw: dict[str, Any]) -> dict[str, Any]:
        """v1 scenes stored `parent` as a slash-separated path string
        ("Models/GreyBlock_B"). v2 stores `parent` as either an object id
        or a root-service name (see SceneObject.parent / Instance.parent_id).
        We can't fabricate the missing intermediate Folder objects safely,
        so v1 objects are re-parented directly under their path's root
        segment — no data is lost, only the cosmetic nesting."""
        if int(raw.get("version", 1)) >= 2:
            return raw

        migrated_objects: list[dict[str, Any]] = []
        for item in raw.get("objects", []):
            if not isinstance(item, dict):
                continue
            item = dict(item)
            parent = str(item.get("parent", "Workspace"))
            root_segment = parent.split("/", 1)[0].strip() or "Workspace"
            item["parent"] = root_segment
            migrated_objects.append(item)

        return {
            "format": raw.get("format", "nebula-scene"),
            "version": SCENE_FORMAT_VERSION,
            "objects": migrated_objects,
        }

    @staticmethod
    def _sanitize_hierarchy(objects: list[SceneObject]) -> list[SceneObject]:
        """Defends against a hand-edited or corrupted scene file: a parent_id
        that references a missing object, or a parent chain that cycles back
        on itself. Both get reset to Workspace with a logged warning rather
        than silently producing a broken/infinite-looking Explorer tree.
        Not needed for the live/network path — the server (handle_set_parent)
        already refuses to ever write a cycle into world in the first place,
        and live-mode blocks file import entirely (see load_from_file)."""
        by_id = {obj.id: obj for obj in objects}
        for obj in objects:
            parent_key = obj.parent or "Workspace"
            if parent_key not in ROOT_SERVICES and parent_key not in by_id:
                print(f"[SCENE] '{obj.name}': parent '{parent_key}' not found — moved to Workspace.")
                obj.parent = "Workspace"
                continue

            visited = {obj.id}
            walker_key = parent_key
            cyclic = False
            while walker_key in by_id:
                if walker_key in visited:
                    cyclic = True
                    break
                visited.add(walker_key)
                walker_key = by_id[walker_key].parent or "Workspace"
            if cyclic:
                print(f"[SCENE] '{obj.name}': parent chain forms a cycle — moved to Workspace.")
                obj.parent = "Workspace"
        return objects

    def load_from_file(self, path: str | Path) -> None:
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw = self._migrate_scene_data(raw)
        object_data = raw.get("objects", [])
        if not isinstance(object_data, list):
            raise ValueError("Scene file has no valid 'objects' list.")
        objects = self._sanitize_hierarchy([SceneObject.from_dict(item) for item in object_data])

        if self.live_mode:
            accepted = bool(self._adapter_call("import_scene", objects, default=False))
            if not accepted:
                raise RuntimeError("The live client does not support importing a scene snapshot.")
            return

        self.replace_scene(objects, mark_dirty=False)
        self.log("info", f"Scene loaded: {source.name}")

    def replace_world(
        self,
        objects: list[dict[str, Any]],
        on_result: Callable[[bool, str], None],
        services: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        """Stage 3.2: Create Place from template / Open Place. Instance-
        shaped (id/class_name/name/parent_id/properties/tags/attributes/
        enabled) objects, NOT SceneObject-shaped -- distinct from load_
        from_file()/save_to_file() above, which remain untouched for the
        offline (non-live) demo path. on_result fires asynchronously once
        the server's REPLACE_WORLD_RESULT arrives; this method itself
        never blocks and never mutates local state -- see PlaceManager
        for the file-side half of this operation.

        Stage 3.8: `services` (root-service persistent properties) rides
        along in the SAME atomic REPLACE_WORLD request when provided --
        see place_manager.PlaceOperationResult.services, always a
        complete schema-defaulted snapshot by the time it reaches here."""
        if not self.live_mode:
            on_result(False, "Place file operations require a live server connection.")
            return False
        return bool(self._adapter_call("replace_world", objects, on_result, services, default=False))

    def export_world(self) -> list[dict[str, Any]]:
        """Stage 3.2 Save Place: Instance-shaped snapshot of whatever is
        currently authoritative. Empty in non-live mode -- Save Place is
        not offered there; the offline demo keeps using save_to_file()."""
        if not self.live_mode:
            return []
        return list(self._adapter_call("export_world", default=[]))

    def create_starter_character(self) -> bool:
        """Stage 3.8: Explorer's "Insert StarterCharacter" action -- see
        MultiplayerStudioAdapter.create_starter_character() for the actual
        singleton pre-check and CreateObjectCommand. Requires a live
        server connection, same as every other creation path."""
        if not self.live_mode:
            self.log("warning", "StarterCharacter requires a live server connection.")
            return False
        return bool(self._adapter_call("create_starter_character", default=False))

    def export_services(self) -> dict[str, dict[str, Any]]:
        """Stage 3.8 Save Place counterpart to export_world() -- current
        persistent root-service properties. Empty in non-live mode, same
        reasoning as export_world()."""
        if not self.live_mode:
            return {}
        return dict(self._adapter_call("export_services", default={}))

# ---------------------------------------------------------------------------
# Icon factory
# ---------------------------------------------------------------------------

class IconFactory:
    @staticmethod
    def make(kind: str, size: int = 24, color: QColor | None = None) -> QIcon:
        color = color or QColor("#d4d7da")
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color, max(1.5, size / 14))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        s = float(size)
        c = QPointF(s / 2, s / 2)

        if kind == "select":
            path = QPainterPath()
            path.moveTo(s * 0.25, s * 0.16)
            path.lineTo(s * 0.72, s * 0.57)
            path.lineTo(s * 0.52, s * 0.61)
            path.lineTo(s * 0.65, s * 0.84)
            path.lineTo(s * 0.54, s * 0.9)
            path.lineTo(s * 0.41, s * 0.66)
            path.lineTo(s * 0.25, s * 0.82)
            path.closeSubpath()
            painter.setBrush(color)
            painter.drawPath(path)
        elif kind == "move":
            painter.drawLine(QPointF(c.x(), s * 0.12), QPointF(c.x(), s * 0.88))
            painter.drawLine(QPointF(s * 0.12, c.y()), QPointF(s * 0.88, c.y()))
            for end, a, b in [
                (QPointF(c.x(), s * 0.12), QPointF(c.x() - 4, s * 0.24), QPointF(c.x() + 4, s * 0.24)),
                (QPointF(c.x(), s * 0.88), QPointF(c.x() - 4, s * 0.76), QPointF(c.x() + 4, s * 0.76)),
                (QPointF(s * 0.12, c.y()), QPointF(s * 0.24, c.y() - 4), QPointF(s * 0.24, c.y() + 4)),
                (QPointF(s * 0.88, c.y()), QPointF(s * 0.76, c.y() - 4), QPointF(s * 0.76, c.y() + 4)),
            ]:
                painter.drawLine(end, a)
                painter.drawLine(end, b)
        elif kind == "rotate":
            painter.drawArc(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), 25 * 16, 300 * 16)
            painter.drawLine(QPointF(s * 0.68, s * 0.15), QPointF(s * 0.84, s * 0.22))
            painter.drawLine(QPointF(s * 0.84, s * 0.22), QPointF(s * 0.75, s * 0.36))
        elif kind == "scale":
            painter.drawRect(QRectF(s * 0.2, s * 0.42, s * 0.38, s * 0.38))
            painter.drawLine(QPointF(s * 0.5, s * 0.5), QPointF(s * 0.82, s * 0.18))
            painter.drawLine(QPointF(s * 0.64, s * 0.18), QPointF(s * 0.82, s * 0.18))
            painter.drawLine(QPointF(s * 0.82, s * 0.18), QPointF(s * 0.82, s * 0.36))
        elif kind in {"cube", "mesh"}:
            top = QPolygonF([
                QPointF(s * 0.5, s * 0.12),
                QPointF(s * 0.82, s * 0.3),
                QPointF(s * 0.5, s * 0.48),
                QPointF(s * 0.18, s * 0.3),
            ])
            left = QPolygonF([
                QPointF(s * 0.18, s * 0.3),
                QPointF(s * 0.5, s * 0.48),
                QPointF(s * 0.5, s * 0.84),
                QPointF(s * 0.18, s * 0.66),
            ])
            right = QPolygonF([
                QPointF(s * 0.5, s * 0.48),
                QPointF(s * 0.82, s * 0.3),
                QPointF(s * 0.82, s * 0.66),
                QPointF(s * 0.5, s * 0.84),
            ])
            painter.drawPolygon(top)
            painter.drawPolygon(left)
            painter.drawPolygon(right)
            if kind == "mesh":
                painter.drawLine(top[0], top[2])
                painter.drawLine(top[1], top[3])
        elif kind == "plus":
            painter.drawLine(QPointF(c.x(), s * 0.2), QPointF(c.x(), s * 0.8))
            painter.drawLine(QPointF(s * 0.2, c.y()), QPointF(s * 0.8, c.y()))
        elif kind == "ui":
            painter.drawRoundedRect(QRectF(s * 0.16, s * 0.2, s * 0.68, s * 0.6), 2, 2)
            painter.drawRect(QRectF(s * 0.25, s * 0.3, s * 0.22, s * 0.18))
            painter.drawLine(QPointF(s * 0.55, s * 0.32), QPointF(s * 0.74, s * 0.32))
            painter.drawLine(QPointF(s * 0.55, s * 0.44), QPointF(s * 0.7, s * 0.44))
            painter.drawLine(QPointF(s * 0.25, s * 0.6), QPointF(s * 0.74, s * 0.6))
        elif kind == "material":
            painter.drawEllipse(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64))
            painter.drawArc(QRectF(s * 0.27, s * 0.22, s * 0.42, s * 0.42), 20 * 16, 160 * 16)
        elif kind == "color":
            painter.setBrush(QColor("#b8b9bb"))
            painter.drawEllipse(QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6))
        elif kind == "texture":
            painter.drawRect(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64))
            painter.drawLine(QPointF(s * 0.18, s * 0.18), QPointF(s * 0.82, s * 0.82))
            painter.drawLine(QPointF(s * 0.82, s * 0.18), QPointF(s * 0.18, s * 0.82))
        elif kind == "play":
            painter.setBrush(QColor("#2aa8f2"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF([
                QPointF(s * 0.3, s * 0.18),
                QPointF(s * 0.82, s * 0.5),
                QPointF(s * 0.3, s * 0.82),
            ]))
        elif kind == "stop":
            painter.setBrush(QColor("#6b6e71"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(s * 0.25, s * 0.25, s * 0.5, s * 0.5), 2, 2)
        elif kind == "gear":
            painter.drawEllipse(QRectF(s * 0.28, s * 0.28, s * 0.44, s * 0.44))
            painter.drawEllipse(QRectF(s * 0.43, s * 0.43, s * 0.14, s * 0.14))
            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                p1 = QPointF(c.x() + math.cos(rad) * s * 0.25, c.y() + math.sin(rad) * s * 0.25)
                p2 = QPointF(c.x() + math.cos(rad) * s * 0.38, c.y() + math.sin(rad) * s * 0.38)
                painter.drawLine(p1, p2)
        elif kind == "lock":
            painter.drawRoundedRect(QRectF(s * 0.25, s * 0.43, s * 0.5, s * 0.38), 2, 2)
            painter.drawArc(QRectF(s * 0.33, s * 0.16, s * 0.34, s * 0.44), 0, 180 * 16)
        elif kind == "group":
            painter.drawRect(QRectF(s * 0.18, s * 0.2, s * 0.28, s * 0.28))
            painter.drawRect(QRectF(s * 0.54, s * 0.52, s * 0.28, s * 0.28))
            painter.drawLine(QPointF(s * 0.46, s * 0.34), QPointF(s * 0.66, s * 0.52))
        elif kind == "grid":
            for i in range(3):
                for j in range(3):
                    painter.drawRect(QRectF(s * (0.18 + j * 0.22), s * (0.18 + i * 0.22), s * 0.14, s * 0.14))
        elif kind == "search":
            painter.drawEllipse(QRectF(s * 0.18, s * 0.18, s * 0.46, s * 0.46))
            painter.drawLine(QPointF(s * 0.57, s * 0.57), QPointF(s * 0.82, s * 0.82))
        elif kind == "camera":
            painter.drawRoundedRect(QRectF(s * 0.16, s * 0.3, s * 0.68, s * 0.46), 3, 3)
            painter.drawEllipse(QRectF(s * 0.38, s * 0.38, s * 0.24, s * 0.24))
            painter.drawLine(QPointF(s * 0.28, s * 0.3), QPointF(s * 0.36, s * 0.18))
            painter.drawLine(QPointF(s * 0.36, s * 0.18), QPointF(s * 0.54, s * 0.18))
        elif kind == "folder":
            painter.setBrush(color)
            path = QPainterPath()
            path.moveTo(s * 0.16, s * 0.3)
            path.lineTo(s * 0.42, s * 0.3)
            path.lineTo(s * 0.5, s * 0.38)
            path.lineTo(s * 0.84, s * 0.38)
            path.lineTo(s * 0.84, s * 0.74)
            path.lineTo(s * 0.16, s * 0.74)
            path.closeSubpath()
            painter.drawPath(path)
        elif kind == "script":
            painter.drawRoundedRect(QRectF(s * 0.24, s * 0.12, s * 0.52, s * 0.76), 2, 2)
            painter.drawLine(QPointF(s * 0.34, s * 0.32), QPointF(s * 0.66, s * 0.32))
            painter.drawLine(QPointF(s * 0.34, s * 0.48), QPointF(s * 0.58, s * 0.48))
            painter.drawLine(QPointF(s * 0.34, s * 0.64), QPointF(s * 0.66, s * 0.64))
        elif kind == "spawn":
            painter.drawLine(QPointF(s * 0.3, s * 0.14), QPointF(s * 0.3, s * 0.86))
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([
                QPointF(s * 0.32, s * 0.18),
                QPointF(s * 0.8, s * 0.32),
                QPointF(s * 0.32, s * 0.46),
            ]))
        elif kind == "light":
            painter.drawEllipse(QRectF(s * 0.3, s * 0.12, s * 0.4, s * 0.4))
            painter.drawLine(QPointF(s * 0.4, s * 0.5), QPointF(s * 0.4, s * 0.68))
            painter.drawLine(QPointF(s * 0.6, s * 0.5), QPointF(s * 0.6, s * 0.68))
            painter.drawRect(QRectF(s * 0.38, s * 0.68, s * 0.24, s * 0.1))
        elif kind == "sound":
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([
                QPointF(s * 0.2, s * 0.4),
                QPointF(s * 0.36, s * 0.4),
                QPointF(s * 0.54, s * 0.24),
                QPointF(s * 0.54, s * 0.76),
                QPointF(s * 0.36, s * 0.6),
                QPointF(s * 0.2, s * 0.6),
            ]))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(s * 0.58, s * 0.32, s * 0.22, s * 0.36), -60 * 16, 120 * 16)
        elif kind == "particle":
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            for dx, dy, r in (
                (0.3, 0.3, 0.06), (0.6, 0.24, 0.05), (0.72, 0.5, 0.07),
                (0.4, 0.6, 0.05), (0.58, 0.72, 0.06), (0.24, 0.55, 0.045),
            ):
                painter.drawEllipse(QPointF(s * dx, s * dy), s * r, s * r)
        else:
            painter.drawEllipse(QRectF(s * 0.28, s * 0.28, s * 0.44, s * 0.44))

        painter.end()
        return QIcon(pix)


# ---------------------------------------------------------------------------
# Ribbon
# ---------------------------------------------------------------------------

class RibbonToolButton(QToolButton):
    def __init__(
        self,
        text: str,
        icon_kind: str,
        callback=None,
        checkable: bool = False,
        large: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setIcon(IconFactory.make(icon_kind, 28 if large else 19))
        self.setIconSize(QSize(28 if large else 19, 28 if large else 19))
        self.setCheckable(checkable)
        self.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon if large
            else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.setAutoRaise(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(58 if large else 45)
        self.setMinimumHeight(62 if large else 28)
        if callback is not None:
            self.clicked.connect(callback)


class RibbonGroup(QFrame):
    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("RibbonGroup")
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(6, 4, 6, 2)
        self.main_layout.setSpacing(2)

        self.content = QWidget(self)
        self.content_layout = QHBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(3)
        self.main_layout.addWidget(self.content, 1)

        label = QLabel(title, self)
        label.setObjectName("RibbonGroupTitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_layout.addWidget(label)

    def add_widget(self, widget: QWidget) -> None:
        self.content_layout.addWidget(widget)


class Ribbon(QWidget):
    tool_changed = Signal(str)
    snap_changed = Signal(float)

    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.setObjectName("Ribbon")
        self.setFixedHeight(139)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabBar(self)
        self.tabs.setObjectName("RibbonTabs")
        self.tabs.setExpanding(False)
        for text in ("Scene", "Model", "Test", "View", "Plugins"):
            self.tabs.addTab(text)
        self.tabs.setCurrentIndex(0)
        layout.addWidget(self.tabs)

        self.pages = QStackedWidget(self)
        self.pages.setObjectName("RibbonPages")
        layout.addWidget(self.pages, 1)

        self.pages.addWidget(self._build_scene_page())
        self.pages.addWidget(self._build_model_page())
        self.pages.addWidget(self._build_test_page())
        self.pages.addWidget(self._placeholder_page("View controls will be connected to the engine camera."))
        self.pages.addWidget(self._placeholder_page("Plugin actions can be registered here."))

        self.tabs.currentChanged.connect(self.pages.setCurrentIndex)
        self.bridge.play_state_changed.connect(self._sync_play_buttons)

        # Проталкиваем начальное значение снапа сразу — адаптер к этому
        # моменту уже подключён к bridge (см. порядок в main()).
        self._on_snap_text_changed(self.snap_combo.currentText())

    @staticmethod
    def _parse_snap_text(text: str) -> float:
        try:
            return float(text.split()[0])
        except (IndexError, ValueError):
            return 0.25

    def _on_snap_text_changed(self, text: str) -> None:
        value = self._parse_snap_text(text)
        self.snap_changed.emit(value)
        self.bridge.set_grid_snap(value)

    def _tool_button(self, text: str, kind: str, tool_name: str) -> RibbonToolButton:
        button = RibbonToolButton(
            text,
            kind,
            callback=lambda checked=False, name=tool_name: self.tool_changed.emit(name),
            checkable=True,
        )
        return button

    def _build_scene_page(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(7, 5, 7, 5)
        row.setSpacing(0)

        transform = RibbonGroup("Transform")
        self.select_button = self._tool_button("Select", "select", "select")
        self.move_button = self._tool_button("Move", "move", "move")
        self.rotate_button = self._tool_button("Rotate", "rotate", "rotate")
        self.scale_button = self._tool_button("Scale", "scale", "scale")
        self.select_button.setChecked(True)
        for button in (self.select_button, self.move_button, self.rotate_button, self.scale_button):
            button.clicked.connect(lambda checked, b=button: self._make_exclusive(b))
            transform.add_widget(button)
        row.addWidget(transform)

        insert = RibbonGroup("Insert")
        insert.add_widget(RibbonToolButton(
            "Add", "plus",
            lambda: open_insert_object_dialog(self.bridge, self, self.bridge.selected_id),
        ))
        insert.add_widget(RibbonToolButton("Part", "cube", lambda: self.bridge.add_part("Part")))
        insert.add_widget(RibbonToolButton("Folder", "folder", lambda: self.bridge.add_part("Folder")))
        insert.add_widget(RibbonToolButton("Script", "script", lambda: self.bridge.add_part("Script")))
        row.addWidget(insert)

        material = RibbonGroup("Material")
        material.add_widget(RibbonToolButton("Material", "material", self._cycle_material))
        material.add_widget(RibbonToolButton("Color", "color", self._choose_color))
        material.add_widget(RibbonToolButton("Texture", "texture", self._texture_stub))
        row.addWidget(material)

        tools = RibbonGroup("Tools")
        tools.add_widget(RibbonToolButton("Collision", "grid", self._toggle_collision))
        tools.add_widget(RibbonToolButton("Lock", "lock", self._toggle_lock))
        tools.add_widget(RibbonToolButton("Group", "group", self._group_stub))
        row.addWidget(tools)

        play = RibbonGroup("Play")
        self.play_button = RibbonToolButton("Play", "play", self.bridge.play)
        self.stop_button = RibbonToolButton("Stop", "stop", self.bridge.stop)
        self.stop_button.setEnabled(False)
        play.add_widget(self.play_button)
        play.add_widget(self.stop_button)
        row.addWidget(play)

        settings = RibbonGroup("Settings")
        settings.add_widget(RibbonToolButton("Game\nSettings", "gear", self._settings_stub))
        row.addWidget(settings)

        snap = RibbonGroup("Grid Snap")
        snap_column = QWidget()
        snap_layout = QVBoxLayout(snap_column)
        snap_layout.setContentsMargins(2, 4, 2, 4)
        snap_layout.setSpacing(5)
        self.snap_combo = QComboBox()
        snap_combo = self.snap_combo
        snap_combo.addItems(("0.05 studs", "0.25 studs", "0.5 studs", "1 stud", "2 studs", "4 studs"))
        snap_combo.setCurrentText("0.25 studs")
        snap_combo.setMinimumWidth(108)
        snap_combo.currentTextChanged.connect(self._on_snap_text_changed)
        snap_layout.addWidget(snap_combo)
        icon_row = QHBoxLayout()
        icon_row.addWidget(RibbonToolButton("", "grid", large=False))
        icon_row.addWidget(RibbonToolButton("", "grid", large=False))
        icon_row.addWidget(RibbonToolButton("", "lock", large=False))
        snap_layout.addLayout(icon_row)
        snap.add_widget(snap_column)
        row.addWidget(snap)

        row.addStretch(1)
        return page

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(7, 5, 7, 5)
        row.setSpacing(0)

        model_group = RibbonGroup("Model")
        model_group.add_widget(RibbonToolButton("Duplicate", "group", self.bridge.duplicate_selected))
        model_group.add_widget(RibbonToolButton("Delete", "stop", self.bridge.delete_selected))
        # Stage 4.1 fix: this button used to request "Mesh" -- not a
        # registered class (object_registry only knows "MeshPart", added
        # this stage) -- so it produced "Unknown or non-creatable object
        # type 'Mesh'" instead of ever creating anything. Insert Object's
        # own MeshPart entry always worked correctly; this toolbar
        # shortcut just never got pointed at the real class name.
        model_group.add_widget(RibbonToolButton("Mesh", "mesh", lambda: self.bridge.add_part("MeshPart")))
        row.addWidget(model_group)

        physics = RibbonGroup("Physics")
        physics.add_widget(RibbonToolButton("Anchor", "lock", self._toggle_anchor))
        physics.add_widget(RibbonToolButton("Collision", "grid", self._toggle_collision))
        row.addWidget(physics)

        row.addStretch(1)
        return page

    def _build_test_page(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(7, 5, 7, 5)
        row.setSpacing(0)

        play = RibbonGroup("Simulation")
        play.add_widget(RibbonToolButton("Play", "play", self.bridge.play))
        play.add_widget(RibbonToolButton("Stop", "stop", self.bridge.stop))
        row.addWidget(play)

        debug = RibbonGroup("Debug")
        debug.add_widget(RibbonToolButton("Console", "ui", lambda: self.bridge.log("info", "Console focused.")))
        debug.add_widget(RibbonToolButton("Profiler", "grid", lambda: self.bridge.log("info", "Profiler opened.")))
        row.addWidget(debug)
        row.addStretch(1)
        return page

    def _placeholder_page(self, text: str) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        label = QLabel(text)
        label.setObjectName("MutedLabel")
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _make_exclusive(self, active: RibbonToolButton) -> None:
        for button in (self.select_button, self.move_button, self.rotate_button, self.scale_button):
            button.setChecked(button is active)

    def _sync_play_buttons(self, playing: bool) -> None:
        self.play_button.setEnabled(not playing)
        self.stop_button.setEnabled(playing)

    def _selected(self) -> Optional[SceneObject]:
        return self.bridge.get_object(self.bridge.selected_id)

    def _toggle_collision(self) -> None:
        obj = self._selected()
        if obj:
            new_value = not obj.can_collide
            self.bridge.set_property(obj.id, "can_collide", new_value)
            self.bridge.log("info", f"CanCollide = {new_value} for '{obj.name}'.")

    def _toggle_lock(self) -> None:
        obj = self._selected()
        if obj:
            self.bridge.set_property(obj.id, "locked", not obj.locked)

    def _toggle_anchor(self) -> None:
        obj = self._selected()
        if obj:
            self.bridge.set_property(obj.id, "anchored", not obj.anchored)

    def _cycle_material(self) -> None:
        obj = self._selected()
        if not obj:
            return
        materials = ["Plastic", "Metal", "Wood", "Glass", "Concrete"]
        index = (materials.index(obj.material) + 1) % len(materials) if obj.material in materials else 0
        self.bridge.set_property(obj.id, "material", materials[index])

    def _choose_color(self) -> None:
        obj = self._selected()
        if not obj:
            return
        chosen = QColorDialog.getColor(QColor(obj.color), self, "Object color")
        if chosen.isValid():
            self.bridge.set_property(obj.id, "color", chosen.name())

    def _texture_stub(self) -> None:
        self.bridge.log("warning", "Texture import is an integration hook in this prototype.")

    def _group_stub(self) -> None:
        self.bridge.log("warning", "Multi-selection and grouping are not enabled yet.")

    def _settings_stub(self) -> None:
        QMessageBox.information(
            self,
            "Game Settings",
            "This button is ready for your engine settings widget.\n\n"
            "Connect it to the engine adapter or replace this dialog.",
        )


# ---------------------------------------------------------------------------
# Scene explorer
# ---------------------------------------------------------------------------

class ExplorerTree(QTreeWidget):
    """QTreeWidget with hand-rolled internal drag-and-drop, instead of Qt's
    built-in InternalMove mode. InternalMove would reparent the QTreeWidgetItem
    immediately on drop, before any business validation runs — for a
    server-authoritative hierarchy that's backwards: the tree must only move
    once the server confirms (see ExplorerPanel.handle_drop /
    EngineBridge.set_parent). So this only ever *reads* drag state; it never
    calls the base class's dropEvent, meaning Qt never performs its own
    reparent — ExplorerPanel decides everything.

    No sibling reordering: a drop is only accepted when the drop indicator
    position is OnItem (or over empty space, meaning "onto Workspace") — not
    AboveItem/BelowItem, which Qt uses to mean "insert as sibling here"."""

    def __init__(self, panel: "ExplorerPanel", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._panel = panel
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._panel.activate_current():
                return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop_target_item(self, event) -> tuple[Optional[QTreeWidgetItem], bool]:
        """Returns (target_item, position_is_valid). position_is_valid is
        False for AboveItem/BelowItem (sibling-reorder positions we don't
        support) when there IS an item under the cursor; dropping on empty
        space (no item at all) is always valid and means "onto Workspace"."""
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        item = self.itemAt(pos)
        if item is None:
            return None, True
        indicator = self.dropIndicatorPosition()
        return item, indicator == QAbstractItemView.DropIndicatorPosition.OnItem

    def dragMoveEvent(self, event) -> None:
        dragged_item = self.currentItem()
        target_item, position_ok = self._drop_target_item(event)
        if not position_ok:
            event.ignore()
            return
        ok, _reason = self._panel.can_reparent(dragged_item, target_item)
        if ok:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        dragged_item = self.currentItem()
        target_item, position_ok = self._drop_target_item(event)
        if not position_ok:
            event.ignore()
            return
        ok, _reason = self._panel.can_reparent(dragged_item, target_item)
        if not ok:
            event.ignore()
            return
        # Deliberately NOT calling super().dropEvent(event) — see class
        # docstring. We accept the action so Qt ends the drag cleanly, then
        # hand off to business logic; the tree item itself doesn't move
        # until/unless the server confirms.
        event.acceptProposedAction()
        self._panel.handle_drop(dragged_item, target_item)


class ExplorerPanel(QWidget):
    SYSTEM_PROTECTED_TYPES = {"Baseplate", "Camera", "Lighting", "Terrain"}

    def __init__(self, bridge: EngineBridge, script_workspace: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.script_workspace = script_workspace
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 6, 7, 6)
        layout.setSpacing(5)

        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        self.search.addAction(IconFactory.make("search", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.add_button = QToolButton()
        self.add_button.setIcon(IconFactory.make("plus", 18))
        self.add_button.setToolTip("Insert Object (Ctrl+Shift+A)")
        self.add_button.clicked.connect(self._open_insert_dialog_for_selection)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.add_button)
        layout.addLayout(search_row)

        self.tree = ExplorerTree(self)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(17)
        self.tree.setAnimated(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.tree, 1)

        self.search.textChanged.connect(self._apply_filter)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemChanged.connect(self._on_item_text_changed)
        self.bridge.scene_changed.connect(self.rebuild)
        self.bridge.selection_changed.connect(self._sync_selection)
        self.bridge.property_changed.connect(self._on_property_changed)

        self.rename_shortcut = QShortcut(QKeySequence("F2"), self.tree)
        self.rename_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.rename_shortcut.activated.connect(self._rename_current)

        self.rebuild()

    # --------------------------------------------------------
    # ПОСТРОЕНИЕ ДЕРЕВА (parent = id объекта или имя корневого сервиса)
    # --------------------------------------------------------

    def _collect_expanded_keys(self) -> set[str]:
        """Stable key per item: object id for real objects, the root's own
        name for root/service items (roots have UserRole=None, so text(0) is
        the only stable identity they have)."""
        keys: set[str] = set()

        def visit(item: QTreeWidgetItem) -> None:
            if item.isExpanded():
                key = item.data(0, Qt.ItemDataRole.UserRole) or item.text(0)
                keys.add(key)
            for i in range(item.childCount()):
                visit(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            visit(root.child(i))
        return keys

    def rebuild(self) -> None:
        expanded_keys = self._collect_expanded_keys()
        first_rebuild = not expanded_keys and self.tree.topLevelItemCount() == 0

        self._syncing = True
        self.tree.clear()

        roots: dict[str, QTreeWidgetItem] = {}
        object_items: dict[str, QTreeWidgetItem] = {}

        for root_name in ROOT_SERVICES:
            root = QTreeWidgetItem([root_name])
            # Stage 3.8: the root's own name is now its selection identity
            # (was None) -- this is what makes clicking Workspace/
            # StarterPlayer/etc. in Explorer actually select something
            # instead of silently emitting selection_changed(None). See
            # EngineBridge.get_object()'s services-dict fallback and
            # InspectorPanel.set_object()'s service-aware branch.
            root.setData(0, Qt.ItemDataRole.UserRole, root_name)
            root.setIcon(0, self._icon_for_root(root_name))
            roots[root_name] = root
            self.tree.addTopLevelItem(root)

        for obj in self.bridge.objects:
            item = QTreeWidgetItem([obj.name])
            item.setData(0, Qt.ItemDataRole.UserRole, obj.id)
            item.setIcon(0, self._icon_for_type(obj.object_type, obj.color))
            if not obj.id.startswith("system:"):
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled)
            object_items[obj.id] = item

        for obj in self.bridge.objects:
            item = object_items[obj.id]
            parent_key = obj.parent or "Workspace"
            if parent_key in roots:
                roots[parent_key].addChild(item)
            elif parent_key in object_items and parent_key != obj.id:
                object_items[parent_key].addChild(item)
            else:
                roots["Workspace"].addChild(item)

        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            key = top.text(0)
            if key in expanded_keys or (first_rebuild and key in {"Workspace", "Players"}):
                top.setExpanded(True)
        for object_id, item in object_items.items():
            if object_id in expanded_keys:
                item.setExpanded(True)

        self._syncing = False
        self._sync_selection(self.bridge.get_object(self.bridge.selected_id))
        self._apply_filter(self.search.text())

    def _icon_for_root(self, name: str) -> QIcon:
        if name == "Workspace":
            return IconFactory.make("grid", 16, QColor("#6ec5d7"))
        if name == "Players":
            return IconFactory.make("group", 16, QColor("#8bd27c"))
        return IconFactory.make("cube", 16, QColor("#d6c068"))

    def _icon_for_type(self, object_type: str, color: str) -> QIcon:
        definition = object_registry.get_object_type(object_type)
        if definition is not None:
            tint = QColor(color) if definition.has_3d_entity else QColor("#b9bec2")
            return IconFactory.make(definition.icon, 16, tint)
        if object_type == "Camera":
            return IconFactory.make("camera", 16, QColor("#b9bec2"))
        if object_type in {"Model", "Mesh"}:
            return IconFactory.make("cube", 16, QColor("#b9bec2"))
        if object_type == "Lighting":
            return IconFactory.make("material", 16, QColor("#f0d866"))
        return IconFactory.make("cube", 16, QColor(color))

    # --------------------------------------------------------
    # ВЫБОР
    # --------------------------------------------------------

    def _on_current_changed(self, current: Optional[QTreeWidgetItem], previous: Optional[QTreeWidgetItem]) -> None:
        if self._syncing:
            return
        object_id = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        self.bridge.select(object_id)

    def _sync_selection(self, obj: Optional[SceneObject]) -> None:
        self._syncing = True
        if obj is None:
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)
        else:
            iterator = self.tree.invisibleRootItem()
            item = self._find_item(iterator, obj.id)
            if item:
                self.tree.setCurrentItem(item)
                item.setSelected(True)
                self.tree.scrollToItem(item)
        self._syncing = False

    def _find_item(self, parent: QTreeWidgetItem, object_id: str) -> Optional[QTreeWidgetItem]:
        for index in range(parent.childCount()):
            child = parent.child(index)
            if child.data(0, Qt.ItemDataRole.UserRole) == object_id:
                return child
            found = self._find_item(child, object_id)
            if found:
                return found
        return None

    def _selected_object_id(self) -> Optional[str]:
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    # --------------------------------------------------------
    # ПОИСК / ФИЛЬТР
    # --------------------------------------------------------

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            child_visible = False
            for index in range(item.childCount()):
                if visit(item.child(index)):
                    child_visible = True
            self_match = query in item.text(0).lower()
            visible = not query or self_match or child_visible
            item.setHidden(not visible)
            if query and child_visible:
                item.setExpanded(True)
            return visible

        root = self.tree.invisibleRootItem()
        for index in range(root.childCount()):
            visit(root.child(index))

    def _on_property_changed(self, object_id: str, path: str, value: Any) -> None:
        if path != "name":
            return
        item = self._find_item(self.tree.invisibleRootItem(), object_id)
        if item and item.text(0) != str(value):
            self._syncing = True
            item.setText(0, str(value))
            self._syncing = False

    # --------------------------------------------------------
    # ПЕРЕИМЕНОВАНИЕ (F2 / контекстное меню / двойной клик по QLineEdit-редактору)
    # --------------------------------------------------------

    def _rename_current(self) -> None:
        item = self.tree.currentItem()
        object_id = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        # Stage 3.8: root-service items now carry their own name as
        # UserRole (was None, see rebuild()) -- explicitly excluded here
        # too, not just relying on them lacking the ItemIsEditable flag,
        # since root services are never renameable regardless of how F2
        # is triggered (spec: "root service ClassName cannot change" /
        # "renameability follows the class descriptor", and every service
        # ClassDescriptor sets renameable=False).
        if item is not None and object_id and object_id not in ROOT_SERVICES:
            self.tree.editItem(item)

    def _on_item_text_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._syncing:
            return
        object_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not object_id:
            return
        new_name = item.text(0).strip()
        obj = self.bridge.get_object(object_id)
        if not new_name:
            if obj is not None:
                self._syncing = True
                item.setText(0, obj.name)
                self._syncing = False
            return
        if obj is not None and obj.name == new_name:
            return
        self.bridge.set_property(object_id, "name", new_name)

    # --------------------------------------------------------
    # ДВОЙНОЙ КЛИК / ENTER -- открытие вкладки редактора для Script-типов
    # --------------------------------------------------------

    def _is_script_object(self, object_id: Optional[str]) -> Optional[SceneObject]:
        """Returns the SceneObject if object_id names a Script/LocalScript/
        ModuleScript instance (object_registry category "Scripting"), else
        None. Never used to decide identity -- callers still open by
        object_id, this is purely a type check."""
        if not object_id:
            return None
        obj = self.bridge.get_object(object_id)
        if obj is None:
            return None
        definition = object_registry.get_object_type(obj.object_type)
        if definition is not None and definition.category == "Scripting":
            return obj
        return None

    def open_script_tab(self, object_id: str) -> bool:
        """Opens (or focuses, if already open) the integrated editor tab
        for a Script/LocalScript/ModuleScript. Never executes the script --
        ScriptEditorWorkspace.open_script() only reads properties.Source
        and displays it; ModuleScripts are never run just by opening
        them."""
        return bool(self.script_workspace.open_script(object_id))

    def activate_current(self) -> bool:
        """Enter-key equivalent of double-click: opens the selected
        Script/LocalScript/ModuleScript's tab. Returns False (and does
        nothing) for any other selected type, so Enter falls through to
        Qt's normal tree navigation for non-Script items."""
        object_id = self._selected_object_id()
        if self._is_script_object(object_id) is None:
            return False
        return self.open_script_tab(object_id)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        object_id = item.data(0, Qt.ItemDataRole.UserRole)
        if self._is_script_object(object_id) is None:
            return
        self.open_script_tab(object_id)

    # --------------------------------------------------------
    # КОНТЕКСТНОЕ МЕНЮ
    # --------------------------------------------------------

    def _open_insert_dialog_for_selection(self) -> None:
        open_insert_object_dialog(self.bridge, self, self._selected_object_id())

    def _show_context_menu(self, position: QPoint) -> None:
        item = self.tree.itemAt(position)
        object_id = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

        menu = QMenu(self)
        insert_action = menu.addAction("Insert Object…")
        insert_action.triggered.connect(
            lambda: open_insert_object_dialog(self.bridge, self, object_id)
        )

        if object_id == "StarterPlayer":
            # Stage 3.8: StarterCharacter is a special Model ROLE, not its
            # own ClassName (see datamodel_schema.is_starter_character()),
            # so it can't just be picked from the generic Insert Object
            # type list the way StarterPlayerScripts can -- a dedicated
            # action is the only way to insert one with the exact required
            # name.
            insert_starter_character_action = menu.addAction("Insert StarterCharacter")
            insert_starter_character_action.triggered.connect(self.bridge.create_starter_character)

        if object_id:
            obj = self.bridge.get_object(object_id)
            is_system = object_id.startswith("system:")
            # Stage 3.8: root services now have a real UserRole (their own
            # name, see rebuild()) so this branch reaches them too --
            # explicitly protected here since is_system/SYSTEM_PROTECTED_TYPES
            # were never meant to cover them (spec: "root-service deletion/
            # duplication is forbidden", "renameability follows the class
            # descriptor" -- every service ClassDescriptor is renameable=False,
            # deletable=False).
            is_service = object_id in ROOT_SERVICES
            is_protected = is_service or (obj is not None and obj.object_type in self.SYSTEM_PROTECTED_TYPES)
            menu.addSeparator()

            if self._is_script_object(object_id) is not None:
                open_script_action = menu.addAction("Open Script")
                open_script_action.triggered.connect(lambda: self.open_script_tab(object_id))
                menu.addSeparator()

            rename_action = menu.addAction("Rename")
            rename_action.setEnabled(not is_system and not is_service)
            rename_action.triggered.connect(lambda: self.tree.editItem(item))

            duplicate_action = menu.addAction("Duplicate")
            duplicate_action.setEnabled(not is_system and not is_service)
            duplicate_action.triggered.connect(
                lambda: (self.bridge.select(object_id), self.bridge.duplicate_selected())
            )

            delete_action = menu.addAction("Delete")
            delete_action.setEnabled(not is_system and not is_protected)
            delete_action.triggered.connect(
                lambda: (self.bridge.select(object_id), self.bridge.delete_selected())
            )

        menu.exec(self.tree.viewport().mapToGlobal(position))

    # --------------------------------------------------------
    # DRAG AND DROP REPARENTING (Stage 2.1)
    #
    # Validation runs here (client-side, for immediate drag feedback) AND
    # independently on the server (handle_set_parent in server.py) — this
    # copy exists only for responsive UI (reject cursor / highlight while
    # hovering), it is never the final authority. A client cannot bypass the
    # rules by skipping this and sending a hand-built set_parent message:
    # the server re-validates everything from scratch.
    # --------------------------------------------------------

    def can_reparent(
        self, dragged_item: Optional[QTreeWidgetItem], target_item: Optional[QTreeWidgetItem],
    ) -> tuple[bool, str]:
        if dragged_item is None:
            return False, "No object selected."

        dragged_id = dragged_item.data(0, Qt.ItemDataRole.UserRole)
        if not dragged_id:
            return False, "System objects cannot be reparented."
        if dragged_id in ROOT_SERVICES:
            # Stage 3.8: root-service items now carry their own name as
            # UserRole (was None, which the check above used to catch) --
            # explicitly excluded here too (spec: "root services cannot be
            # reparented").
            return False, "Root services cannot be reparented."

        dragged_obj = self.bridge.get_object(dragged_id)
        if dragged_obj is None:
            return False, "Object no longer exists."

        if target_item is None:
            target_key = "Workspace"
            target_type = "Workspace"
        else:
            target_id = target_item.data(0, Qt.ItemDataRole.UserRole)
            if target_id is None:
                # A root/service item — its display text IS its stable key.
                target_key = target_item.text(0)
                target_type = target_key
            else:
                if target_id == dragged_id:
                    return False, "An object cannot be its own parent."
                target_obj = self.bridge.get_object(target_id)
                if target_obj is None:
                    return False, "Target object no longer exists."
                if target_id in self.bridge.descendant_ids(dragged_id):
                    return False, f"Cannot parent '{dragged_obj.name}' to its own descendant."
                target_key = target_id
                target_type = target_obj.object_type

        if not object_registry.is_parent_allowed(dragged_obj.object_type, target_type):
            return False, f"'{dragged_obj.object_type}' cannot be parented to '{target_type}'."

        if target_key not in ROOT_SERVICES:
            target_definition = object_registry.get_object_type(target_type)
            if target_definition is None or not target_definition.is_container:
                return False, f"'{target_type}' cannot contain children."

        if (dragged_obj.parent or "Workspace") == target_key:
            return False, "Already there."

        return True, target_key

    def handle_drop(self, dragged_item: Optional[QTreeWidgetItem], target_item: Optional[QTreeWidgetItem]) -> None:
        ok, result = self.can_reparent(dragged_item, target_item)
        if not ok:
            self.bridge.log("warning", result)
            return
        dragged_id = dragged_item.data(0, Qt.ItemDataRole.UserRole)
        self.bridge.set_parent(dragged_id, result)


# ---------------------------------------------------------------------------
# Inspector
# ---------------------------------------------------------------------------

class CollapsibleSection(QWidget):
    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(True)
        self.header.setArrowType(Qt.ArrowType.DownArrow)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setObjectName("InspectorSectionHeader")
        layout.addWidget(self.header)

        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(10, 6, 8, 8)
        self.form.setHorizontalSpacing(8)
        self.form.setVerticalSpacing(6)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addWidget(self.body)

        self.header.toggled.connect(self._toggle)

    def _toggle(self, checked: bool) -> None:
        self.body.setVisible(checked)
        self.header.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def add_row(self, label: str, widget: QWidget) -> None:
        self.form.addRow(label, widget)


class VectorEditor(QWidget):
    value_changed = Signal(str, float)

    def __init__(self, value: Vec3, prefix: str, decimals: int = 3, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.spins: dict[str, QDoubleSpinBox] = {}
        for axis, current in (("x", value.x), ("y", value.y), ("z", value.z)):
            box = QDoubleSpinBox()
            box.setDecimals(decimals)
            box.setRange(-1_000_000.0, 1_000_000.0)
            box.setSingleStep(0.25)
            box.setValue(current)
            box.setPrefix(axis.upper() + "  ")
            box.setMinimumWidth(78)
            box.valueChanged.connect(
                lambda number, path=f"{prefix}.{axis}": self.value_changed.emit(path, float(number))
            )
            self.spins[axis] = box
            layout.addWidget(box, 1)

    def set_values_silently(self, value: Vec3) -> None:
        """Updates the three spinboxes in place without emitting
        value_changed — used for high-frequency live updates (gizmo drag)
        where re-triggering the normal edit path would send the value back
        through EngineBridge.set_property() on every rendered frame."""
        for axis, component in (("x", value.x), ("y", value.y), ("z", value.z)):
            box = self.spins[axis]
            if abs(box.value() - component) < 1e-9:
                continue
            box.blockSignals(True)
            box.setValue(component)
            box.blockSignals(False)


class ColorField(QPushButton):
    color_selected = Signal(str)

    def __init__(self, color: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.color = color
        self.clicked.connect(self._choose)
        self.setMinimumHeight(28)
        self._refresh()

    def _refresh(self) -> None:
        self.setText(self.color.upper())
        self.setStyleSheet(
            f"QPushButton {{ text-align: left; padding-left: 34px; "
            f"background: qlineargradient(x1:0,y1:0,x2:0,y2:1, "
            f"stop:0 {self.color}, stop:0.01 #2d2e30, stop:1 #242527); }}"
        )

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.color), self, "Choose color")
        if chosen.isValid():
            self.color = chosen.name()
            self._refresh()
            self.color_selected.emit(self.color)


class InspectorPanel(QWidget):
    def __init__(self, bridge: EngineBridge, script_workspace: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.script_workspace = script_workspace
        self.current_object: Optional[SceneObject] = None
        self._building = False
        # Заполняется _build_transform_section() при показе объекта с
        # transform-секцией; позволяет _external_property_changed() при
        # высокочастотных live-обновлениях (drag гизмо) обновлять значения
        # существующих спинбоксов на месте, а не пересобирать Inspector —
        # см. разбор бага "дёрганое перетаскивание" в отчёте задачи.
        self._live_vector_editors: dict[str, VectorEditor] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.object_header = QWidget()
        header_layout = QHBoxLayout(self.object_header)
        header_layout.setContentsMargins(9, 7, 9, 7)
        self.object_icon = QLabel()
        self.object_icon.setFixedSize(22, 22)
        self.object_name = QLabel("No selection")
        self.object_name.setObjectName("InspectorObjectName")
        self.lock_button = QToolButton()
        self.lock_button.setIcon(IconFactory.make("lock", 16))
        self.lock_button.setCheckable(True)
        self.lock_button.clicked.connect(self._toggle_lock)
        header_layout.addWidget(self.object_icon)
        header_layout.addWidget(self.object_name, 1)
        header_layout.addWidget(self.lock_button)
        outer.addWidget(self.object_header)

        filter_wrap = QWidget()
        filter_layout = QHBoxLayout(filter_wrap)
        filter_layout.setContentsMargins(8, 4, 8, 7)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter Properties (Ctrl+Shift+P)")
        self.filter_edit.addAction(IconFactory.make("search", 16), QLineEdit.ActionPosition.LeadingPosition)
        filter_layout.addWidget(self.filter_edit)
        outer.addWidget(filter_wrap)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.contents = QWidget()
        self.contents_layout = QVBoxLayout(self.contents)
        self.contents_layout.setContentsMargins(0, 0, 0, 8)
        self.contents_layout.setSpacing(2)
        self.contents_layout.addStretch(1)
        self.scroll.setWidget(self.contents)
        outer.addWidget(self.scroll, 1)

        self.empty_label = QLabel("Select an object in Explorer or the viewport.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName("MutedLabel")
        self.contents_layout.insertWidget(0, self.empty_label)

        self.bridge.selection_changed.connect(self.set_object)
        self.bridge.property_changed.connect(self._external_property_changed)
        self.filter_edit.textChanged.connect(self._apply_filter)

    def clear_sections(self) -> None:
        self._live_vector_editors = {}
        while self.contents_layout.count() > 1:
            item = self.contents_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def set_object(self, obj: Optional[SceneObject]) -> None:
        self.current_object = obj
        self._building = True
        self.clear_sections()

        if obj is None:
            self.object_name.setText("No selection")
            self.object_icon.clear()
            self.lock_button.setChecked(False)
            self.empty_label = QLabel("Select an object in Explorer or the viewport.")
            self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.empty_label.setWordWrap(True)
            self.empty_label.setObjectName("MutedLabel")
            self.contents_layout.insertWidget(0, self.empty_label)
            self._building = False
            return

        # Stage 3.8: root services (Workspace, StarterPlayer, ...) render
        # ENTIRELY from datamodel_schema metadata -- never touching
        # shared/object_registry.py or the legacy section_builders dispatch
        # below, since services were never registered there at all (that's
        # the root cause of the "blank Properties" bug this stage fixes).
        # This is also the one Inspector code path a genuinely new class
        # (e.g. a future ProximityPrompt) could reuse with zero Inspector
        # changes -- see _build_schema_sections()'s own docstring.
        if obj.id in ROOT_SERVICES:
            self.object_icon.setPixmap(IconFactory.make("cube", 18, QColor("#d6c068")).pixmap(18, 18))
            self.object_name.setText(obj.name)
            self.lock_button.setChecked(False)
            self.lock_button.setEnabled(False)
            for section in self._build_schema_sections(obj):
                self._insert_section(section)
            self._building = False
            self._apply_filter(self.filter_edit.text())
            return

        definition = object_registry.get_object_type(obj.object_type)
        icon_kind = definition.icon if definition is not None else "cube"
        icon_tint = QColor(obj.color) if (definition is None or definition.has_3d_entity) else QColor("#b9bec2")
        pix = IconFactory.make(icon_kind, 18, icon_tint).pixmap(18, 18)
        self.object_icon.setPixmap(pix)
        self.object_name.setText(obj.name)
        self.lock_button.setEnabled(True)
        self.lock_button.setChecked(obj.locked)

        self._insert_section(self._build_general_section(obj, definition))

        # Legacy system objects (Camera/Terrain/Lighting/Baseplate/...) are
        # not in the registry — keep their historical Transform/Appearance/
        # Behavior layout so nothing regresses for them.
        sections = definition.inspector_sections if definition is not None else ("transform", "appearance", "behavior")

        section_builders = {
            "transform": self._build_transform_section,
            "appearance": self._build_appearance_section,
            "behavior": self._build_behavior_section,
            "container": self._build_container_section,
            "pivot": self._build_pivot_section,
            "mesh": self._build_mesh_section,
            "light": self._build_light_section,
            "surface": self._build_surface_section,
            "script": lambda o: self._build_script_section(o, definition),
            "placeholder": lambda o: self._build_placeholder_section(o, definition),
        }
        for section_name in sections:
            builder = section_builders.get(section_name)
            if builder is not None:
                self._insert_section(builder(obj))

        tags = CollapsibleSection("Tags")
        tags_label = QLabel(", ".join(obj.tags) if obj.tags else "No Tags")
        tags_label.setObjectName("MutedLabel")
        tags.add_row("", tags_label)
        self._insert_section(tags)

        attributes = CollapsibleSection("Attributes")
        attributes_label = QLabel(
            json.dumps(obj.attributes, ensure_ascii=False) if obj.attributes else "No Attributes"
        )
        attributes_label.setObjectName("MutedLabel")
        attributes.add_row("", attributes_label)
        self._insert_section(attributes)

        self._building = False
        self._apply_filter(self.filter_edit.text())

    def _insert_section(self, section: CollapsibleSection) -> None:
        self.contents_layout.insertWidget(self.contents_layout.count() - 1, section)

    def _parent_display_name(self, obj: SceneObject) -> str:
        parent_key = obj.parent or "Workspace"
        if parent_key in ROOT_SERVICES:
            return parent_key
        parent_obj = self.bridge.get_object(parent_key)
        return parent_obj.name if parent_obj is not None else parent_key

    def _build_general_section(self, obj: SceneObject, definition: Optional[ObjectTypeDefinition]) -> CollapsibleSection:
        general = CollapsibleSection("General")

        name_edit = QLineEdit(obj.name)
        name_edit.editingFinished.connect(
            lambda edit=name_edit: self._set_value("name", edit.text().strip() or obj.name)
        )
        general.add_row("Name", name_edit)

        type_label = QLabel(obj.object_type)
        type_label.setObjectName("MutedLabel")
        general.add_row("Type", type_label)

        parent_label = QLabel(self._parent_display_name(obj))
        parent_label.setObjectName("MutedLabel")
        general.add_row("Parent", parent_label)

        enabled = QCheckBox()
        enabled.setChecked(obj.enabled)
        enabled.toggled.connect(lambda value: self._set_value("enabled", bool(value)))
        general.add_row("Enabled", enabled)

        return general

    def _build_schema_sections(self, obj: SceneObject) -> list[CollapsibleSection]:
        """Stage 3.8: fully metadata-driven Inspector rendering -- ONE
        generic path any datamodel_schema-registered class can use, no
        per-class Inspector code. Currently wired only for root services
        (see set_object() above), but the dispatch on PropertyDescriptor.
        value_type below is written generically enough for a future
        legacy_managed=False class (a real ProximityPrompt, eventually) to
        reuse verbatim -- that's the whole point of the metadata registry
        (see datamodel_schema.py's module docstring)."""
        sections: list[CollapsibleSection] = []

        data_section = CollapsibleSection("Data")
        name_label = QLabel(obj.name)
        name_label.setObjectName("MutedLabel")
        data_section.add_row("Name", name_label)
        class_label = QLabel(obj.object_type)
        class_label.setObjectName("MutedLabel")
        data_section.add_row("ClassName", class_label)
        parent_label = QLabel("(none)")
        parent_label.setObjectName("MutedLabel")
        data_section.add_row("Parent", parent_label)
        sections.append(data_section)

        for category, props in datamodel_schema.properties_by_category(obj.object_type):
            if category in ("Data", "Debug", "Runtime"):
                continue  # Data is handled above; Debug/Runtime get their own section below
            section = CollapsibleSection(category)
            for prop in props:
                section.add_row(prop.label(), self._build_schema_property_editor(obj, prop))
            sections.append(section)

        runtime_props = [p for cat, props in datamodel_schema.properties_by_category(obj.object_type) if cat == "Runtime" for p in props]
        if runtime_props:
            runtime_section = CollapsibleSection("Runtime")
            for prop in runtime_props:
                label = QLabel("(available during Play)")
                label.setObjectName("MutedLabel")
                runtime_section.add_row(prop.label(), label)
            sections.append(runtime_section)

        debug_section = CollapsibleSection("Debug")
        id_label = QLabel(obj.id)
        id_label.setObjectName("MutedLabel")
        debug_section.add_row("Instance Id (SStudio)", id_label)
        sections.append(debug_section)

        return sections

    def _build_schema_property_editor(self, obj: SceneObject, prop: "datamodel_schema.PropertyDescriptor") -> QWidget:
        current = obj.properties.get(prop.name, prop.default)
        path = f"properties.{prop.name}"

        if not prop.editable:
            label = QLabel(str(current))
            label.setObjectName("MutedLabel")
            return label

        if prop.value_type == "bool":
            box = QCheckBox()
            box.setChecked(bool(current))
            box.toggled.connect(lambda value, p=path: self._set_value(p, bool(value)))
            return box

        if prop.value_type == "enum":
            assert prop.enum is not None
            combo = QComboBox()
            combo.addItems(list(prop.enum.values))
            if current in prop.enum.values:
                combo.setCurrentText(str(current))
            combo.currentTextChanged.connect(lambda value, p=path: self._set_value(p, value))
            return combo

        if prop.value_type in ("int", "float"):
            minimum = prop.minimum if prop.minimum is not None else -1_000_000.0
            maximum = prop.maximum if prop.maximum is not None else 1_000_000.0
            box = self._float_box(float(current), float(minimum), float(maximum), 0.1)
            box.valueChanged.connect(lambda value, p=path: self._set_value(p, float(value)))
            return box

        if prop.value_type == "color3":
            # Stage 4.3C: previously this generic path had no color3 case
            # at all (fell through to a plain QLineEdit showing e.g.
            # "[68.0, 68.0, 82.0]" as text -- editing it would send a
            # STRING to a property that validate_property_value() only
            # accepts as a 3-number list, so it silently never applied).
            # Needed for real for the new Sky/Sun colors to be authorable
            # at all; also fixes the same pre-existing gap for
            # AmbientColor/FogColor. Same hex<->[r,g,b] conversion
            # _build_surface_section() already uses for Part.EmissionColor.
            rgb = current if isinstance(current, (list, tuple)) and len(current) == 3 else [255, 255, 255]
            hex_value = "#{:02x}{:02x}{:02x}".format(
                max(0, min(255, int(rgb[0]))), max(0, min(255, int(rgb[1]))), max(0, min(255, int(rgb[2]))),
            )
            field = ColorField(hex_value)
            field.color_selected.connect(
                lambda value, p=path: self._set_value(
                    p, [QColor(str(value)).red(), QColor(str(value)).green(), QColor(str(value)).blue()],
                )
            )
            return field

        if prop.value_type == "vector3":
            # Deliberately NOT VectorEditor: that widget emits one
            # value_changed(path, float) PER AXIS (e.g. "rotation.x"), a
            # convention only the hardcoded position/rotation/size
            # dispatch in _set_value()/the bridge understands. A generic
            # schema vector3 (SunRotation) instead reads/writes the whole
            # [x,y,z] in one _set_value() call, same shape as the color3
            # case above.
            values = current if isinstance(current, (list, tuple)) and len(current) == 3 else [0.0, 0.0, 0.0]
            container = QWidget()
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(3)
            boxes: list[QDoubleSpinBox] = []
            for axis, component in zip(("X", "Y", "Z"), values):
                box = QDoubleSpinBox()
                box.setDecimals(3)
                box.setRange(-1_000_000.0, 1_000_000.0)
                box.setSingleStep(0.25)
                box.setValue(float(component))
                box.setPrefix(axis + "  ")
                box.setMinimumWidth(78)
                boxes.append(box)
                row.addWidget(box, 1)
            for box in boxes:
                box.valueChanged.connect(lambda _value, p=path, bs=boxes: self._set_value(p, [b.value() for b in bs]))
            return container

        edit = QLineEdit(str(current))
        edit.editingFinished.connect(lambda w=edit, p=path: self._set_value(p, w.text()))
        return edit

    def _build_transform_section(self, obj: SceneObject) -> CollapsibleSection:
        transform = CollapsibleSection("Transform")
        position = VectorEditor(obj.position, "position")
        rotation = VectorEditor(obj.rotation, "rotation")
        scale = VectorEditor(obj.scale, "scale")
        size = VectorEditor(obj.size, "size")
        for editor in (position, rotation, scale, size):
            editor.value_changed.connect(self._set_value)
        self._live_vector_editors = {"position": position, "rotation": rotation, "size": size}

        transform.add_row("Position", position)
        transform.add_row("Rotation", rotation)
        transform.add_row("Scale", scale)
        transform.add_row("Size", size)
        return transform

    def _build_light_section(self, obj: SceneObject) -> CollapsibleSection:
        """Stage 4.1 (local lighting foundation): minimum PointLight/
        SpotLight authoring. Position/[Rotation] reuse the same
        VectorEditor + self._set_value("position"/"rotation", ...) path
        Transform normally uses -- lights get their own section instead of
        the full Transform one since Scale/Size don't apply to them.
        Color reuses ColorField + self._set_value("color", ...) exactly
        like Appearance's own Color row (obj.color is already correctly
        populated for any has_3d_entity class, lights included -- see
        instance_to_scene_object()). Intensity/Range/[Angle] go through
        the existing generic "properties.<key>" write path, same as
        MeshId -- no new property-edit machinery anywhere in this method."""
        light = CollapsibleSection("Light")

        position = VectorEditor(obj.position, "position")
        position.value_changed.connect(self._set_value)
        light.add_row("Position", position)
        self._live_vector_editors["position"] = position

        if obj.object_type == "SpotLight":
            rotation = VectorEditor(obj.rotation, "rotation")
            rotation.value_changed.connect(self._set_value)
            light.add_row("Rotation", rotation)
            self._live_vector_editors["rotation"] = rotation

        color_field = ColorField(obj.color)
        color_field.color_selected.connect(lambda value: self._set_value("color", value))
        light.add_row("Color", color_field)

        intensity = self._float_box(float(obj.properties.get("Intensity", 1.0)), 0.0, 20.0, 0.1)
        intensity.valueChanged.connect(lambda value: self._set_value("properties.Intensity", float(value)))
        light.add_row("Intensity", intensity)

        light_range = self._float_box(float(obj.properties.get("Range", 8.0)), 0.1, 100.0, 0.5)
        light_range.valueChanged.connect(lambda value: self._set_value("properties.Range", float(value)))
        light.add_row("Range", light_range)

        if obj.object_type == "SpotLight":
            angle = self._float_box(float(obj.properties.get("Angle", 45.0)), 1.0, 179.0, 1.0)
            angle.valueChanged.connect(lambda value: self._set_value("properties.Angle", float(value)))
            light.add_row("Angle", angle)

        return light

    def _build_surface_section(self, obj: SceneObject) -> CollapsibleSection:
        """Stage 4.1 (materials & textures foundation): minimum texture/
        PBR authoring for Part (never SpawnPoint/MeshPart -- see
        shared/object_registry.py's _PART_SURFACE_SCHEMA comment). Every
        field goes through the existing generic "properties.<key>" write
        path, same as Mesh Id -- no new property-edit machinery. The
        Material dropdown that ALSO affects Roughness/Metallic lives in
        the existing Appearance section (see _on_material_preset_changed);
        this section is for the explicit, always-authoritative PBR
        controls a creator can fine-tune afterward."""
        surface = CollapsibleSection("Surface")

        texture_id_edit = QLineEdit(str(obj.properties.get("TextureId", "")))
        texture_id_edit.setPlaceholderText("e.g. concrete.jpg or walls/plaster.png")
        texture_id_edit.editingFinished.connect(
            lambda edit=texture_id_edit: self._set_value("properties.TextureId", edit.text().strip())
        )
        surface.add_row("Texture Id", texture_id_edit)

        hint = QLabel("Path relative to the project's assets/textures/ folder. Empty = flat Color.")
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        surface.add_row("", hint)

        tiles = self._float_box(float(obj.properties.get("TilesPerUnit", 1.0)), 0.01, 20.0, 0.1)
        tiles.valueChanged.connect(lambda value: self._set_value("properties.TilesPerUnit", float(value)))
        surface.add_row("Tiles Per Unit", tiles)

        roughness = self._float_box(float(obj.properties.get("Roughness", 1.0)), 0.0, 1.0, 0.05)
        roughness.valueChanged.connect(lambda value: self._set_value("properties.Roughness", float(value)))
        surface.add_row("Roughness", roughness)

        metallic = self._float_box(float(obj.properties.get("Metallic", 0.0)), 0.0, 1.0, 0.05)
        metallic.valueChanged.connect(lambda value: self._set_value("properties.Metallic", float(value)))
        surface.add_row("Metallic", metallic)

        emission_rgb = obj.properties.get("EmissionColor", [0, 0, 0])
        emission_hex = "#{:02x}{:02x}{:02x}".format(
            int(emission_rgb[0]), int(emission_rgb[1]), int(emission_rgb[2]),
        )
        emission_color = ColorField(emission_hex)
        emission_color.color_selected.connect(
            lambda value: self._set_value(
                "properties.EmissionColor",
                [QColor(str(value)).red(), QColor(str(value)).green(), QColor(str(value)).blue()],
            )
        )
        surface.add_row("Emission Color", emission_color)

        emission_strength = self._float_box(float(obj.properties.get("EmissionStrength", 0.0)), 0.0, 10.0, 0.1)
        emission_strength.valueChanged.connect(lambda value: self._set_value("properties.EmissionStrength", float(value)))
        surface.add_row("Emission Strength", emission_strength)

        return surface

    def _build_mesh_section(self, obj: SceneObject) -> CollapsibleSection:
        """Stage 4.1 follow-up: minimum MeshId authoring for MeshPart --
        Insert Object always created a correct MeshPart, but there was no
        packaged-editor way to ever give it a MeshId (only Lua or hand-
        edited Place JSON could), so a freshly inserted one only ever
        showed the missing-asset magenta placeholder. Reuses the existing
        generic "properties.<key>" write path in
        MultiplayerStudioAdapter.set_property() (client_studio.py) --
        no new property-edit machinery, same as every other field here."""
        mesh = CollapsibleSection("Mesh")
        mesh_id_edit = QLineEdit(str(obj.properties.get("MeshId", "")))
        mesh_id_edit.setPlaceholderText("e.g. crate.glb or props/crate.glb")
        mesh_id_edit.editingFinished.connect(
            lambda edit=mesh_id_edit: self._set_value("properties.MeshId", edit.text().strip())
        )
        mesh.add_row("Mesh Id", mesh_id_edit)

        hint = QLabel("Path relative to the project's assets/meshes/ folder (.glb/.gltf).")
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        mesh.add_row("", hint)
        return mesh

    def _on_material_preset_changed(self, obj: SceneObject, value: str) -> None:
        """Stage 4.1 (materials & textures foundation): makes the
        pre-existing Material dropdown genuinely useful. Always writes the
        Material string (unchanged behavior, every object type). For Part
        specifically, ALSO applies the preset's Roughness/Metallic (and,
        for "Neon", EmissionStrength) as a one-time default -- selecting a
        preset is a convenience starting point, not a standing constraint;
        nothing re-applies it later, so adjusting the Roughness/Metallic/
        Emission sliders afterward always wins (see
        shared/object_registry.MATERIAL_PRESETS's own docstring)."""
        self._set_value("material", value)
        if obj.object_type != "Part":
            return
        preset = object_registry.MATERIAL_PRESETS.get(value)
        if not preset:
            return
        for key, preset_value in preset.items():
            self._set_value(f"properties.{key}", float(preset_value))
        # No manual Inspector refresh here -- the same network round trip
        # every other property write already goes through
        # (_external_property_changed) will refresh the Surface section's
        # sliders once the server confirms, same as any other edit.

    def _build_appearance_section(self, obj: SceneObject) -> CollapsibleSection:
        appearance = CollapsibleSection("Appearance")
        color = ColorField(obj.color)
        color.color_selected.connect(lambda value: self._set_value("color", value))

        material = QComboBox()
        material.addItems(["Plastic", "Metal", "Wood", "Glass", "Concrete", "Neon"])
        material.setCurrentText(obj.material)
        material.currentTextChanged.connect(lambda value: self._on_material_preset_changed(obj, value))

        transparency = self._float_box(obj.transparency, 0.0, 1.0, 0.05)
        transparency.valueChanged.connect(lambda value: self._set_value("transparency", float(value)))
        reflectance = self._float_box(obj.reflectance, 0.0, 1.0, 0.05)
        reflectance.valueChanged.connect(lambda value: self._set_value("reflectance", float(value)))

        surface = QComboBox()
        surface.addItems(["Smooth", "Studs", "Inlet", "Universal"])
        appearance.add_row("Color", color)
        appearance.add_row("Material", material)
        appearance.add_row("Transparency", transparency)
        appearance.add_row("Reflectance", reflectance)
        appearance.add_row("Surface", surface)
        return appearance

    def _build_behavior_section(self, obj: SceneObject) -> CollapsibleSection:
        behavior = CollapsibleSection("Behavior")
        anchored = QCheckBox()
        anchored.setChecked(obj.anchored)
        anchored.toggled.connect(lambda value: self._set_value("anchored", bool(value)))
        collide = QCheckBox()
        collide.setChecked(obj.can_collide)
        collide.toggled.connect(lambda value: self._set_value("can_collide", bool(value)))
        shadow = QCheckBox()
        shadow.setChecked(obj.cast_shadow)
        shadow.toggled.connect(lambda value: self._set_value("cast_shadow", bool(value)))
        locked = QCheckBox()
        locked.setChecked(obj.locked)
        locked.toggled.connect(lambda value: self._set_value("locked", bool(value)))

        behavior.add_row("Anchored", anchored)
        behavior.add_row("Can Collide", collide)
        behavior.add_row("Cast Shadow", shadow)
        behavior.add_row("Locked", locked)
        return behavior

    def _build_container_section(self, obj: SceneObject) -> CollapsibleSection:
        container = CollapsibleSection("Container")
        children_count = sum(1 for other in self.bridge.objects if other.parent == obj.id)
        count_label = QLabel(str(children_count))
        count_label.setObjectName("MutedLabel")
        container.add_row("Children", count_label)
        return container

    def _build_pivot_section(self, obj: SceneObject) -> CollapsibleSection:
        """Stage 2.2: only meaningful for Model (see inspector_sections in
        object_registry.py — Folder also uses 'container' but not 'pivot').
        Editing these fields performs the same cascading group transform as
        dragging the gizmo — see EngineBridge.transform_model()."""
        pivot = CollapsibleSection("Pivot")

        descendant_count = len(self.bridge.descendant_ids(obj.id))
        descendant_label = QLabel(str(descendant_count))
        descendant_label.setObjectName("MutedLabel")
        pivot.add_row("Descendant Count", descendant_label)

        status_label = QLabel("Explicit" if obj.pivot_is_explicit else "Auto (from descendant bounds)")
        status_label.setObjectName("MutedLabel")
        pivot.add_row("Pivot Status", status_label)

        position = VectorEditor(obj.pivot_position, "pivot_position")
        rotation = VectorEditor(obj.pivot_rotation, "pivot_rotation")
        for editor in (position, rotation):
            editor.value_changed.connect(self._set_pivot_value)
        self._live_vector_editors.update({"pivot_position": position, "pivot_rotation": rotation})

        pivot.add_row("Pivot Position", position)
        pivot.add_row("Pivot Rotation", rotation)
        return pivot

    def _build_script_section(self, obj: SceneObject, definition: Optional[ObjectTypeDefinition]) -> CollapsibleSection:
        script = CollapsibleSection("Script")

        type_label = QLabel(obj.object_type)
        type_label.setObjectName("MutedLabel")
        script.add_row("Script Type", type_label)

        run_context = obj.properties.get("RunContext")
        if run_context:
            run_context_label = QLabel(str(run_context))
            run_context_label.setObjectName("MutedLabel")
            script.add_row("Run Context", run_context_label)

        source = str(obj.properties.get("Source", ""))
        line_count = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
        summary_label = QLabel(f"{line_count} line(s), {len(source)} characters")
        summary_label.setObjectName("MutedLabel")
        script.add_row("Source", summary_label)

        open_button = QPushButton("Open Script")
        open_button.clicked.connect(lambda: self._open_script_source_editor(obj.id, obj.name))
        script.add_row("", open_button)
        return script

    def _open_script_source_editor(self, object_id: str, display_name: str) -> None:
        """Stage 3.1: opens/focuses the integrated ScriptEditorWorkspace
        tab for this Script/LocalScript/ModuleScript -- the same tab
        Explorer double-click/Enter/"Open Script" open, keyed by the same
        stable instance_id, so there is exactly one Source-editing surface
        (see ScriptSourceDialog's removal note). Saving from that tab
        already routes through bridge.set_property("properties.Source",
        ...), the same authoritative path every other Inspector field
        uses."""
        self.script_workspace.open_script(object_id)

    def _build_placeholder_section(self, obj: SceneObject, definition: Optional[ObjectTypeDefinition]) -> CollapsibleSection:
        section = CollapsibleSection("Properties")
        note = QLabel("Editor-only placeholder — runtime behaviour is not implemented yet.")
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        section.add_row("", note)

        defaults = definition.default_properties if definition is not None else {}
        for key, default_value in defaults.items():
            current = obj.properties.get(key, default_value)
            widget = self._generic_property_widget(key, current, default_value)
            section.add_row(key, widget)
        return section

    def _generic_property_widget(self, key: str, current: Any, default_value: Any) -> QWidget:
        if isinstance(default_value, bool):
            widget = QCheckBox()
            widget.setChecked(bool(current))
            widget.toggled.connect(lambda value, k=key: self._set_value(f"properties.{k}", bool(value)))
            return widget
        if isinstance(default_value, list) and len(default_value) == 3:
            try:
                hex_value = "#{:02x}{:02x}{:02x}".format(
                    max(0, min(255, int(current[0]))),
                    max(0, min(255, int(current[1]))),
                    max(0, min(255, int(current[2]))),
                )
            except (TypeError, ValueError, IndexError):
                hex_value = "#ffffff"
            widget = ColorField(hex_value)
            widget.color_selected.connect(
                lambda value, k=key: self._set_value(
                    f"properties.{k}",
                    [int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)],
                )
            )
            return widget
        if isinstance(default_value, (int, float)):
            widget = self._float_box(float(current), -1_000_000.0, 1_000_000.0, 0.1)
            widget.valueChanged.connect(lambda value, k=key: self._set_value(f"properties.{k}", float(value)))
            return widget

        widget = QLineEdit(str(current))
        widget.editingFinished.connect(
            lambda w=widget, k=key: self._set_value(f"properties.{k}", w.text())
        )
        return widget

    def _float_box(self, value: float, minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(minimum, maximum)
        box.setSingleStep(step)
        box.setDecimals(3)
        box.setValue(value)
        return box

    def _set_value(self, path: str, value: Any) -> None:
        if self._building or self.current_object is None:
            return
        self.bridge.set_property(self.current_object.id, path, value)
        if path == "name":
            self.object_name.setText(str(value))
        elif path == "color":
            self.object_icon.setPixmap(
                IconFactory.make("cube", 18, QColor(str(value))).pixmap(18, 18)
            )
        elif path == "locked":
            self.lock_button.setChecked(bool(value))

    def _toggle_lock(self, checked: bool) -> None:
        self._set_value("locked", bool(checked))

    def _set_pivot_value(self, path: str, value: float) -> None:
        """Counterpart to _set_value() for the Pivot section's VectorEditors
        — deliberately does NOT go through bridge.set_property() (a plain
        single-object write). A pivot edit must cascade to every
        transformable descendant exactly like a gizmo drag does, so it goes
        through bridge.transform_model() instead (see Stage 2.2 report)."""
        if self._building or self.current_object is None:
            return
        obj = self.current_object
        field_name, _, axis = path.partition(".")
        current = getattr(obj, field_name, None)
        if not isinstance(current, Vec3) or axis not in ("x", "y", "z"):
            return
        updated = Vec3(current.x, current.y, current.z)
        setattr(updated, axis, value)
        position = [updated.x, updated.y, updated.z] if field_name == "pivot_position" else None
        rotation = [updated.x, updated.y, updated.z] if field_name == "pivot_rotation" else None
        self.bridge.transform_model(obj.id, position, rotation)

    def _external_property_changed(self, object_id: str, path: str, value: Any) -> None:
        if self._building or not self.current_object or self.current_object.id != object_id:
            return

        # Высокочастотный путь (drag гизмо): sync_transform_live() шлёт
        # path="position"/"rotation" целиком с Vec3-значением. Обновляем
        # только существующие спинбоксы, без пересборки Inspector — полная
        # пересборка на каждый кадр перетаскивания и была причиной
        # "дёрганого" движения (см. отчёт задачи). setattr держит
        # current_object согласованным на случай последующего _apply_filter
        # или полной пересборки по другой причине.
        editor = self._live_vector_editors.get(path)
        if editor is not None and isinstance(value, Vec3):
            setattr(self.current_object, path, value)
            editor.set_values_silently(value)
            return

        self.set_object(self.current_object)

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        for index in range(self.contents_layout.count() - 1):
            widget = self.contents_layout.itemAt(index).widget()
            if isinstance(widget, CollapsibleSection):
                widget.setVisible(not query or query in widget.header.text().lower())


# ---------------------------------------------------------------------------
# Insert Object
# ---------------------------------------------------------------------------

def resolve_parent_for_type(
    bridge: EngineBridge, type_id: str, preferred_id: Optional[str],
) -> tuple[str, bool, Optional[str]]:
    """Decides where a newly-inserted object of `type_id` should be parented.

    Returns (resolved_parent, fallback_used, warning_message). `preferred_id`
    is whatever is currently selected in Explorer (an object id, or None).
    If the preferred parent isn't allowed for this type, we silently fall
    back to the type's default_parent — "silently" only in the sense that we
    don't block the user; a warning is still returned for the caller to log.
    """
    definition = object_registry.get_object_type(type_id)
    default_parent = definition.default_parent if definition is not None else "Workspace"

    if not preferred_id:
        return default_parent, False, None

    if preferred_id in ROOT_SERVICES:
        preferred_type_id = preferred_id
        preferred_key = preferred_id
    else:
        preferred_obj = bridge.get_object(preferred_id)
        if preferred_obj is None:
            return default_parent, False, None
        preferred_type_id = preferred_obj.object_type
        preferred_key = preferred_obj.id

    if object_registry.is_parent_allowed(type_id, preferred_type_id):
        return preferred_key, False, None

    display_name = definition.display_name if definition is not None else type_id
    warning = (
        f"'{display_name}' cannot be parented to '{preferred_type_id}'. "
        f"Using the default location '{default_parent}' instead."
    )
    return default_parent, True, warning


class InsertObjectDialog(QDialog):
    """Roblox-style "Insert Object" picker, built entirely from the shared
    object_registry — adding a new type to the registry is enough for it to
    show up here, no UI code changes required."""

    CATEGORY_ORDER = ("Frequently Used", "Basic", "Scripting", "World", "GUI")
    USAGE_SETTINGS_KEY = "insert_object/usage_counts"
    MAX_FREQUENTLY_USED = 8

    def __init__(
        self,
        bridge: EngineBridge,
        preferred_parent_id: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.preferred_parent_id = preferred_parent_id
        self.chosen_type_id: Optional[str] = None

        self.setWindowTitle("Insert Object")
        self.setMinimumSize(420, 360)
        self.resize(620, 480)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search object")
        self.search_edit.addAction(IconFactory.make("search", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search_edit.installEventFilter(self)
        self.search_edit.textChanged.connect(self._rebuild_list)
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setFlow(QListWidget.Flow.LeftToRight)
        self.list_widget.setWrapping(True)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list_widget.setUniformItemSizes(False)
        self.list_widget.setSpacing(6)
        self.list_widget.setIconSize(QSize(40, 40))
        self.list_widget.setStyleSheet(
            "QListWidget { background: #1c1d1f; border: 1px solid #383a3d; }"
            "QListWidget::item { border-radius: 4px; padding: 6px; }"
            "QListWidget::item:hover { background: #2b2d2f; }"
            "QListWidget::item:selected { background: #0d5a8f; color: white; }"
        )
        self.list_widget.itemActivated.connect(self._create_and_accept)
        layout.addWidget(self.list_widget, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.insert_button = QPushButton("Insert")
        self.insert_button.clicked.connect(self._create_and_accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.insert_button)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

        self._rebuild_list("")
        self.search_edit.setFocus()

    # --------------------------------------------------------
    # СПИСОК
    # --------------------------------------------------------

    def _add_separator(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        # Огромная ширина заставляет Qt-flow-layout (IconMode без
        # setGridSize) отвести этому элементу собственную строку — так
        # заголовок категории занимает всю ширину списка вне зависимости
        # от текущего размера окна.
        item.setSizeHint(QSize(10_000, 22))
        item.setForeground(QColor("#8f9498"))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list_widget.addItem(item)

    def _add_tile(self, definition: ObjectTypeDefinition) -> None:
        item = QListWidgetItem(definition.display_name)
        item.setIcon(IconFactory.make(definition.icon, 40))
        item.setSizeHint(QSize(96, 84))
        item.setToolTip(definition.description or definition.display_name)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        item.setData(Qt.ItemDataRole.UserRole, definition.type_id)
        self.list_widget.addItem(item)

    def _rebuild_list(self, query: str = "") -> None:
        self.list_widget.clear()
        query = query.strip()

        if query:
            for definition in object_registry.search_object_types(query):
                if definition.creatable:
                    self._add_tile(definition)
            self._select_first_selectable()
            return

        by_category = object_registry.get_types_by_category()
        ordered: list[tuple[str, list[ObjectTypeDefinition]]] = []

        frequently_used = self._frequently_used_definitions()
        if frequently_used:
            ordered.append(("Frequently Used", frequently_used))

        for category_name in self.CATEGORY_ORDER[1:]:
            items = [d for d in by_category.get(category_name, []) if d.creatable]
            if items:
                ordered.append((category_name, items))

        for category_name, items in by_category.items():
            if category_name in self.CATEGORY_ORDER:
                continue
            items = [d for d in items if d.creatable]
            if items:
                ordered.append((category_name, items))

        for category_name, definitions in ordered:
            self._add_separator(category_name)
            for definition in definitions:
                self._add_tile(definition)

        self._select_first_selectable()

    def _select_first_selectable(self) -> None:
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                self.list_widget.setCurrentItem(item)
                return

    # --------------------------------------------------------
    # ЧАСТО ИСПОЛЬЗУЕМЫЕ (QSettings)
    # --------------------------------------------------------

    def _load_usage_counts(self) -> dict[str, int]:
        settings = QSettings(ORG_NAME, APP_NAME)
        raw = settings.value(self.USAGE_SETTINGS_KEY, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}

    def _record_usage(self, type_id: str) -> None:
        settings = QSettings(ORG_NAME, APP_NAME)
        counts = self._load_usage_counts()
        counts[type_id] = counts.get(type_id, 0) + 1
        settings.setValue(self.USAGE_SETTINGS_KEY, json.dumps(counts))

    def _frequently_used_definitions(self) -> list[ObjectTypeDefinition]:
        counts = self._load_usage_counts()
        if not counts:
            return []
        ranked = sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
        result = []
        for type_id, _count in ranked[: self.MAX_FREQUENTLY_USED]:
            definition = object_registry.get_object_type(type_id)
            if definition is not None and definition.creatable:
                result.append(definition)
        return result

    # --------------------------------------------------------
    # СОЗДАНИЕ / ВВОД
    # --------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search_edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self.list_widget.setFocus()
                if self.list_widget.currentItem() is None:
                    self._select_first_selectable()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._create_and_accept()
                return True
        return super().eventFilter(watched, event)

    def _create_and_accept(self, *_args: Any) -> None:
        item = self.list_widget.currentItem()
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            return
        type_id = item.data(Qt.ItemDataRole.UserRole)
        if not type_id:
            return
        self.chosen_type_id = str(type_id)
        self._record_usage(self.chosen_type_id)
        self.accept()


def open_insert_object_dialog(
    bridge: EngineBridge, parent_widget: QWidget, preferred_parent_id: Optional[str] = None,
) -> None:
    dialog = InsertObjectDialog(bridge, preferred_parent_id=preferred_parent_id, parent=parent_widget)
    if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.chosen_type_id:
        return

    resolved_parent, fallback_used, warning = resolve_parent_for_type(
        bridge, dialog.chosen_type_id, preferred_parent_id
    )
    if fallback_used and warning:
        bridge.log("warning", warning)

    bridge.add_part(dialog.chosen_type_id, parent=resolved_parent)


# ---------------------------------------------------------------------------
# Viewport
# ---------------------------------------------------------------------------

class MockViewport(QWidget):
    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.camera_yaw = -17.0
        self.camera_pitch = 20.0
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.hovered_id: Optional[str] = None
        self.hit_boxes: dict[str, QRectF] = {}
        self.dragging = False
        self.drag_start = QPoint()
        self.pan_start = QPointF()

        self.bridge.scene_changed.connect(self.update)
        self.bridge.selection_changed.connect(lambda _obj: self.update())
        self.bridge.property_changed.connect(lambda *_args: self.update())
        self.bridge.play_state_changed.connect(lambda _playing: self.update())

    def minimumSizeHint(self) -> QSize:
        return QSize(620, 420)

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        self._draw_background(painter)
        self._draw_grid(painter)
        self._draw_objects(painter)
        self._draw_orientation_cube(painter)
        self._draw_view_controls(painter)

        if self.bridge.is_playing:
            self._draw_play_overlay(painter)

    def _draw_background(self, painter: QPainter) -> None:
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor("#8ec5df"))
        gradient.setColorAt(0.44, QColor("#b8d1d7"))
        gradient.setColorAt(0.45, QColor("#b0b8a8"))
        gradient.setColorAt(1.0, QColor("#9ea899"))
        painter.fillRect(self.rect(), gradient)

    def _horizon_y(self) -> float:
        return self.height() * 0.28 + self.pan.y()

    def _draw_grid(self, painter: QPainter) -> None:
        horizon = self._horizon_y()
        bottom = self.height() + 40
        center_x = self.width() / 2 + self.pan.x()
        vanish = QPointF(center_x, horizon)

        painter.save()
        painter.setClipRect(QRectF(0, horizon, self.width(), self.height() - horizon))
        minor = QPen(QColor(86, 99, 89, 48), 1)
        major = QPen(QColor(79, 90, 82, 82), 1.2)

        line_count = 32
        for i in range(-line_count, line_count + 1):
            x_bottom = center_x + i * 34 * self.zoom
            painter.setPen(major if i % 4 == 0 else minor)
            painter.drawLine(vanish, QPointF(x_bottom, bottom))

        depth_lines = 26
        for i in range(depth_lines):
            t = i / max(1, depth_lines - 1)
            curved = t ** 2.05
            y = horizon + curved * (bottom - horizon)
            painter.setPen(major if i % 4 == 0 else minor)
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))

        painter.restore()

    def _project(self, position: Vec3) -> QPointF:
        scale = min(self.width(), self.height()) / 36.0 * self.zoom
        x = self.width() / 2 + self.pan.x() + (position.x - position.z * 0.72) * scale
        ground_y = self.height() * 0.68 + self.pan.y()
        y = ground_y + (position.x + position.z) * scale * 0.20 - position.y * scale
        return QPointF(x, y)

    def _draw_cube(
        self,
        painter: QPainter,
        center: QPointF,
        width: float,
        height: float,
        depth: float,
        color: QColor,
        selected: bool,
        hovered: bool,
    ) -> QRectF:
        w = max(8.0, width)
        h = max(8.0, height)
        d = max(5.0, depth)

        front = QRectF(center.x() - w / 2, center.y() - h, w, h)
        offset = QPointF(d * 0.45, -d * 0.32)
        top = QPolygonF([
            front.topLeft(),
            front.topRight(),
            front.topRight() + offset,
            front.topLeft() + offset,
        ])
        side = QPolygonF([
            front.topRight(),
            front.bottomRight(),
            front.bottomRight() + offset,
            front.topRight() + offset,
        ])

        edge = QColor("#7d8586")
        if selected:
            edge = QColor("#21b3ff")
        elif hovered:
            edge = QColor("#d7edf7")

        painter.setPen(QPen(edge, 2 if selected else 1))
        painter.setBrush(color.lighter(112))
        painter.drawPolygon(top)
        painter.setBrush(color.darker(110))
        painter.drawPolygon(side)
        painter.setBrush(color)
        painter.drawRect(front)

        bounds = front.united(top.boundingRect()).united(side.boundingRect())
        return bounds.adjusted(-4, -4, 4, 4)

    @staticmethod
    def _is_drawable(obj: SceneObject) -> bool:
        if obj.object_type in {"Camera", "Lighting", "Terrain", "Baseplate"}:
            return False
        definition = object_registry.get_object_type(obj.object_type)
        if definition is not None and not definition.has_3d_entity:
            return False
        return True

    def _draw_objects(self, painter: QPainter) -> None:
        self.hit_boxes.clear()
        drawable = [obj for obj in self.bridge.objects if self._is_drawable(obj)]
        drawable.sort(key=lambda obj: obj.position.z + obj.position.x)

        for obj in drawable:
            center = self._project(obj.position)
            unit = min(self.width(), self.height()) / 36.0 * self.zoom
            width = obj.size.x * unit * 0.68
            height = obj.size.y * unit * 0.7
            depth = obj.size.z * unit * 0.5
            color = QColor(obj.color)
            if obj.transparency > 0:
                color.setAlphaF(max(0.1, 1.0 - obj.transparency))

            selected = obj.id == self.bridge.selected_id
            hovered = obj.id == self.hovered_id
            bounds = self._draw_cube(
                painter, center, width, height, depth, color, selected, hovered
            )
            self.hit_boxes[obj.id] = bounds

            if selected:
                self._draw_gizmo(painter, QPointF(center.x(), center.y() - height * 0.45))

    def _draw_gizmo(self, painter: QPainter, center: QPointF) -> None:
        length = 72
        painter.save()
        painter.setPen(QPen(QColor("#e33333"), 3))
        painter.drawLine(center, center + QPointF(length, 0))
        painter.setBrush(QColor("#e33333"))
        painter.drawPolygon(QPolygonF([
            center + QPointF(length, 0),
            center + QPointF(length - 11, -6),
            center + QPointF(length - 11, 6),
        ]))

        painter.setPen(QPen(QColor("#27c43b"), 3))
        painter.drawLine(center, center + QPointF(0, -length))
        painter.setBrush(QColor("#27c43b"))
        painter.drawPolygon(QPolygonF([
            center + QPointF(0, -length),
            center + QPointF(-6, -length + 11),
            center + QPointF(6, -length + 11),
        ]))

        painter.setPen(QPen(QColor("#2c55d8"), 3))
        painter.drawLine(center, center + QPointF(-42, 18))
        painter.setBrush(QColor("#2c55d8"))
        painter.drawPolygon(QPolygonF([
            center + QPointF(-42, 18),
            center + QPointF(-29, 9),
            center + QPointF(-27, 20),
        ]))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2faeff"))
        painter.drawRoundedRect(QRectF(center.x() - 6, center.y() - 6, 12, 12), 2, 2)
        painter.restore()

    def _draw_orientation_cube(self, painter: QPainter) -> None:
        rect = QRectF(self.width() - 92, 20, 58, 58)
        painter.save()
        painter.setPen(QPen(QColor("#6e7377"), 1.2))
        painter.setBrush(QColor(213, 215, 211, 220))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setFont(QFont(QApplication.font().family(), 9, QFont.Weight.Bold))
        painter.setPen(QColor("#25a443"))
        painter.drawText(QRectF(rect.x(), rect.y() + 1, rect.width(), 18), Qt.AlignmentFlag.AlignCenter, "Y")
        painter.setPen(QColor("#d52d2d"))
        painter.drawText(QRectF(rect.right() - 20, rect.center().y() - 9, 18, 18), Qt.AlignmentFlag.AlignCenter, "X")
        painter.setPen(QColor("#2769d0"))
        painter.drawText(QRectF(rect.x() + 2, rect.bottom() - 21, 20, 18), Qt.AlignmentFlag.AlignCenter, "Z")
        painter.restore()

    def _draw_view_controls(self, painter: QPainter) -> None:
        width = 184
        rect = QRectF(self.width() - width - 14, self.height() - 48, width, 34)
        painter.save()
        painter.setPen(QPen(QColor("#65696b"), 1))
        painter.setBrush(QColor(37, 39, 40, 185))
        painter.drawRoundedRect(rect, 5, 5)
        labels = ["▦", "◉", "▣", "□", "⛶"]
        painter.setFont(QFont(QApplication.font().family(), 12))
        painter.setPen(QColor("#c9cdcf"))
        segment = width / len(labels)
        for i, label in enumerate(labels):
            painter.drawText(
                QRectF(rect.x() + i * segment, rect.y(), segment, rect.height()),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        painter.restore()

    def _draw_play_overlay(self, painter: QPainter) -> None:
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(10, 12, 14, 55))
        painter.drawRect(self.rect())
        badge = QRectF(self.width() / 2 - 70, 17, 140, 30)
        painter.setBrush(QColor(27, 31, 34, 225))
        painter.drawRoundedRect(badge, 15, 15)
        painter.setPen(QColor("#55d173"))
        painter.setFont(QFont(QApplication.font().family(), 10, QFont.Weight.DemiBold))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "●  PLAYING")
        painter.restore()

    def mousePressEvent(self, event) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self.dragging = True
            self.drag_start = event.position().toPoint()
            self.pan_start = QPointF(self.pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            point = event.position()
            selected = None
            for object_id, rect in reversed(list(self.hit_boxes.items())):
                if rect.contains(point):
                    selected = object_id
                    break
            self.bridge.select(selected)

    def mouseMoveEvent(self, event) -> None:
        if self.dragging:
            delta = event.position().toPoint() - self.drag_start
            self.pan = self.pan_start + QPointF(delta.x(), delta.y())
            self.update()
            return

        point = event.position()
        hovered = None
        for object_id, rect in reversed(list(self.hit_boxes.items())):
            if rect.contains(point):
                hovered = object_id
                break
        if hovered != self.hovered_id:
            self.hovered_id = hovered
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self.dragging = False
            self.unsetCursor()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        factor = 1.12 if delta > 0 else 1 / 1.12
        self.zoom = max(0.35, min(3.0, self.zoom * factor))
        self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_F:
            self.focus_selected()
            return
        super().keyPressEvent(event)

    def focus_selected(self) -> None:
        obj = self.bridge.get_object(self.bridge.selected_id)
        if obj is None:
            return
        projected = self._project(obj.position)
        target = QPointF(self.width() / 2, self.height() * 0.55)
        self.pan += target - projected
        self.update()


class ViewportFrame(QWidget):
    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scene_bar = QWidget()
        scene_bar.setObjectName("SceneTabBar")
        bar_layout = QHBoxLayout(scene_bar)
        bar_layout.setContentsMargins(8, 0, 8, 0)
        bar_layout.setSpacing(2)

        scene_icon = QLabel()
        scene_icon.setPixmap(IconFactory.make("grid", 15).pixmap(15, 15))
        self.scene_title = QLabel("Untitled Scene")
        close_button = QToolButton()
        close_button.setText("×")
        close_button.setAutoRaise(True)
        add_tab = QToolButton()
        add_tab.setText("+")
        add_tab.setToolTip("New scene")
        add_tab.clicked.connect(self.bridge.new_scene)

        bar_layout.addWidget(scene_icon)
        bar_layout.addWidget(self.scene_title)
        bar_layout.addWidget(close_button)
        bar_layout.addSpacing(4)
        bar_layout.addWidget(add_tab)
        bar_layout.addStretch(1)
        layout.addWidget(scene_bar)

        tool_bar = QWidget()
        tool_bar.setObjectName("ViewportToolbar")
        tool_layout = QHBoxLayout(tool_bar)
        tool_layout.setContentsMargins(8, 5, 8, 5)
        tool_layout.setSpacing(3)
        for text, kind, tip in [
            ("", "select", "Select"),
            ("", "move", "Move"),
            ("", "rotate", "Rotate"),
            ("", "scale", "Scale"),
            ("", "camera", "Camera"),
            ("", "grid", "Grid"),
        ]:
            button = QToolButton()
            button.setIcon(IconFactory.make(kind, 18))
            button.setToolTip(tip)
            button.setCheckable(tip in {"Select", "Move", "Rotate", "Scale"})
            button.setAutoRaise(False)
            tool_layout.addWidget(button)
        tool_layout.addStretch(1)
        layout.addWidget(tool_bar)

        self.stack = QStackedWidget()
        self.mock_viewport = MockViewport(bridge)
        self.stack.addWidget(self.mock_viewport)
        layout.addWidget(self.stack, 1)

        self.external_widget: Optional[QWidget] = None

    def install_external_viewport(self, widget: QWidget) -> None:
        if self.external_widget is not None:
            self.stack.removeWidget(self.external_widget)
            self.external_widget.setParent(None)
        self.external_widget = widget
        self.stack.addWidget(widget)
        self.stack.setCurrentWidget(widget)
        self.bridge.log("info", f"External viewport installed: {type(widget).__name__}")

    def use_mock_viewport(self) -> None:
        self.stack.setCurrentWidget(self.mock_viewport)

    def focus_selected(self) -> None:
        if self.stack.currentWidget() is self.mock_viewport:
            self.mock_viewport.focus_selected()


# ---------------------------------------------------------------------------
# Output / Console / Assets / Profiler
# ---------------------------------------------------------------------------

class _OutputTextEdit(QTextEdit):
    """Adds one signal Stage 3.1 needs -- which text block was
    double-clicked -- on top of an otherwise completely ordinary read-only
    QTextEdit. OutputPanel uses this to map a double-clicked line back to
    the Script instance ID/line it was logged for (see
    OutputPanel._diagnostic_blocks)."""

    block_double_clicked = Signal(int)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override
        super().mouseDoubleClickEvent(event)
        cursor = self.cursorForPosition(event.pos())
        self.block_double_clicked.emit(cursor.blockNumber())


class OutputPanel(QWidget):
    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        # Stage 3.1 Output-to-source navigation: maps the block number of
        # an appended Output line to the (script_id, line) it was logged
        # for, WITHOUT parsing the formatted log text back apart -- see
        # _on_lua_diagnostic, which correlates using the fact that
        # lua_runtime.py always calls the plain-text log path immediately
        # before the structured diagnostic path for the same event (same
        # thread, direct Qt connections -- see that method's docstring).
        self._diagnostic_blocks: dict[int, tuple[str, Optional[int]]] = {}
        self._last_log_block: Optional[int] = None
        self._navigate_callback: Optional[Callable[[str, Optional[int]], None]] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        output_page = QWidget()
        output_layout = QVBoxLayout(output_page)
        output_layout.setContentsMargins(7, 5, 7, 7)
        output_layout.setSpacing(5)

        filters = QHBoxLayout()
        self.message_filter = QComboBox()
        self.message_filter.addItems(["All Messages", "Info", "Warning", "Error"])
        self.context_filter = QComboBox()
        self.context_filter.addItems(["All Contexts", "Editor", "Engine", "Adapter"])
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_output)
        filters.addWidget(self.message_filter)
        filters.addWidget(self.context_filter)
        filters.addStretch(1)
        filters.addWidget(clear_button)
        output_layout.addLayout(filters)

        self.output = _OutputTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(5000)
        self.output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.output.setFont(QFont("JetBrains Mono, Consolas, monospace", 9))
        self.output.block_double_clicked.connect(self._on_output_block_double_clicked)
        output_layout.addWidget(self.output, 1)

        console_page = QWidget()
        console_layout = QVBoxLayout(console_page)
        console_layout.setContentsMargins(7, 6, 7, 7)
        self.console_log = QPlainTextEdit()
        self.console_log.setReadOnly(True)
        self.command_line = QLineEdit()
        self.command_line.setPlaceholderText("Enter editor command...")
        self.command_line.returnPressed.connect(self._execute_command)
        console_layout.addWidget(self.console_log, 1)
        console_layout.addWidget(self.command_line)

        assets_page = QWidget()
        assets_layout = QVBoxLayout(assets_page)
        assets_layout.setContentsMargins(7, 7, 7, 7)
        assets_search = QLineEdit()
        assets_search.setPlaceholderText("Search assets...")
        assets = QListWidget()
        assets.addItems([
            "Materials",
            "Meshes",
            "Textures",
            "Audio",
            "Scripts",
            "Prefabs",
        ])
        assets_layout.addWidget(assets_search)
        assets_layout.addWidget(assets, 1)

        profiler_page = QWidget()
        profiler_layout = QVBoxLayout(profiler_page)
        profiler_layout.setContentsMargins(10, 10, 10, 10)
        self.profiler_label = QLabel("Scheduler: 60 fps\nFrame: 16.67 ms\nDraw calls: 23\nObjects: 0")
        self.profiler_label.setObjectName("ProfilerLabel")
        profiler_layout.addWidget(self.profiler_label)
        profiler_layout.addStretch(1)

        self.tabs.addTab(output_page, "Output")
        self.tabs.addTab(console_page, "Console")
        self.tabs.addTab(assets_page, "Assets")
        self.tabs.addTab(profiler_page, "Profiler")
        layout.addWidget(self.tabs)

        self.bridge.log_message.connect(self.append_log)
        self.bridge.lua_diagnostic.connect(self._on_lua_diagnostic)
        self.bridge.scene_changed.connect(self._update_profiler)
        self._update_profiler()
        self.append_log("info", "Scene loaded successfully. (0.38s)")
        self.append_log("info", "Workspace ready.")

    def set_navigate_callback(self, callback: Callable[[str, Optional[int]], None]) -> None:
        """Optional hook so studio_editor_live.py's StudioMainWindow can
        wire double-clicked diagnostic lines to
        ScriptEditorWorkspace.navigate_to_diagnostic without this class
        importing anything from script_editor.py."""
        self._navigate_callback = callback

    def append_log(self, level: str, message: str) -> None:
        from datetime import datetime

        stamp = datetime.now().strftime("%H:%M:%S")
        level_name = level.upper()
        color_map = {
            "info": "#57a9ff",
            "warning": "#edbd54",
            "error": "#ef6b6b",
        }
        color = color_map.get(level.lower(), "#bfc4c8")
        escaped = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        html = (
            f'<span style="color:#8a9095">{stamp}</span> '
            f'<span style="color:{color}">[{level_name}]</span> '
            f'<span style="color:#c9cdd0">{escaped}</span>'
        )
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.output.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(html)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()
        self._last_log_block = cursor.blockNumber()
        self.console_log.appendPlainText(f"{stamp} [{level_name}] {message}")

    def _on_lua_diagnostic(self, script_id: str, severity: str, message: str, line: Any, session_id: int) -> None:
        """Correlates the Output line just appended by append_log() (see
        that method's `_last_log_block`) with this diagnostic's identity,
        instead of parsing the formatted log text back apart. Relies on
        lua_runtime.py always calling its plain-text log path immediately
        before notifying diagnostic listeners for the same event (both
        synchronous, same thread -- see LuaRuntimeManager._report_error/
        _report_info)."""
        if severity not in ("error", "warning") or line is None:
            return
        if self._last_log_block is not None:
            self._diagnostic_blocks[self._last_log_block] = (script_id, line)

    def _on_output_block_double_clicked(self, block_number: int) -> None:
        meta = self._diagnostic_blocks.get(block_number)
        if meta is None:
            return
        script_id, line = meta
        if self._navigate_callback is not None:
            self._navigate_callback(script_id, line)

    def _clear_output(self) -> None:
        self.output.clear()
        self._diagnostic_blocks.clear()
        self._last_log_block = None

    def _execute_command(self) -> None:
        command = self.command_line.text().strip()
        if not command:
            return
        self.console_log.appendPlainText(f"> {command}")
        self.command_line.clear()

        parts = command.split()
        head = parts[0].lower()
        if head == "play":
            self.bridge.play()
        elif head == "stop":
            self.bridge.stop()
        elif head == "add":
            object_type = parts[1] if len(parts) > 1 else "Part"
            self.bridge.add_part(object_type)
        elif head == "delete":
            self.bridge.delete_selected()
        elif head == "duplicate":
            self.bridge.duplicate_selected()
        elif head == "clear":
            self._clear_output()
            self.console_log.clear()
        elif head == "help":
            self.console_log.appendPlainText(
                "Commands: play, stop, add [Part|Mesh|UI], delete, duplicate, clear, help"
            )
        else:
            self.bridge.log("warning", f"Unknown command: {command}")

    def _update_profiler(self) -> None:
        self.profiler_label.setText(
            "Scheduler: 60 fps\n"
            "Frame: 16.67 ms\n"
            "Draw calls: 23\n"
            f"Objects: {len(self.bridge.objects)}"
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class StudioMainWindow(QMainWindow):
    def __init__(self, bridge: Optional[EngineBridge] = None) -> None:
        super().__init__()
        self.bridge = bridge or EngineBridge()
        self.shutdown_callback: Any = None
        self.current_file: Optional[Path] = None  # offline (non-live) demo path only -- see save_scene/open_scene
        self.settings = QSettings(ORG_NAME, APP_NAME)
        # Stage 3.2: PlaceManager is plain data (no Qt/engine refs on it,
        # see its own docstring) -- always constructed, but its create/
        # open/save methods are only exercised for a live bridge. Set
        # externally via set_templates_binding() once client_studio.py's
        # main() has wired install_template_browser() -- optional, so a
        # StudioMainWindow built standalone/headless (tests, the offline
        # demo) never crashes, it just has no Start Page to return to.
        self.place_manager = place_manager.PlaceManager()
        self.templates_binding: Optional[Any] = None

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1180, 720)
        self.resize(1640, 940)
        self.setDockNestingEnabled(True)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)

        self._build_menu()
        self._build_central()
        self._build_docks()
        self._build_status_bar()
        self._build_shortcuts()

        self.bridge.dirty_changed.connect(self._update_title)
        # Stage 3.2: place_manager.is_dirty has no Qt signal of its own
        # (PlaceManager is plain data, see its docstring) -- scene_changed
        # already fires synchronously right alongside every
        # mark_authoritative_edit() call (both happen in the same
        # on_instance_created/updated/deleted/model-transform methods, see
        # client_studio.py), so reusing it here keeps the title in sync
        # without a second signal plumbed all the way from there.
        self.bridge.scene_changed.connect(self._update_title)
        self.bridge.selection_changed.connect(self._selection_status)
        self.bridge.play_state_changed.connect(self._play_status)
        self.bridge.set_play_guard(self._handle_play_guard)
        # Edit-menu Undo/Redo must read "Undo Typing"/"Redo Typing" (and
        # act on the text buffer) the instant a LuaCodeEditor gains focus,
        # and read the normal scene action the instant it loses focus --
        # history_state_changed alone can't see focus changes (typing
        # inside a QPlainTextEdit never touches scene history at all).
        QApplication.instance().focusChanged.connect(lambda _old, _new: self._update_undo_redo_actions())

        self._restore_layout()
        if not self.bridge.live_mode:
            self.bridge.select(next((obj.id for obj in self.bridge.objects if obj.name == "GreyBlock_B"), None))
        self._update_title()

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        new_action = QAction("New Place", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self._place_new)
        open_action = QAction("Open Place…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._place_open)
        save_action = QAction("Save Place", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._on_save_triggered)
        save_as_action = QAction("Save Place As…", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self._place_save_as)
        save_all_action = QAction("Save All", self)
        save_all_action.setShortcut(QKeySequence("Ctrl+Alt+S"))
        save_all_action.triggered.connect(self._save_all_scripts)
        return_to_start_action = QAction("Return to Start Page", self)
        return_to_start_action.triggered.connect(self._return_to_start_page)
        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)

        file_menu.addActions([new_action, open_action, save_action, save_as_action, save_all_action])
        file_menu.addSeparator()
        file_menu.addAction(return_to_start_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        edit_menu = menu.addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self._on_undo_triggered)
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self._on_redo_triggered)
        edit_menu.addActions([self.undo_action, self.redo_action])
        # Ctrl+Shift+Z as an additional redo chord (Ctrl+Y already covers
        # the platform-standard one via QKeySequence.StandardKey.Redo) --
        # same handler, so it stays subject to the identical focus check
        # rather than being a second, divergent code path.
        self.redo_alt_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self.redo_alt_shortcut.activated.connect(self._on_redo_triggered)
        edit_menu.addSeparator()
        duplicate_action = QAction("Duplicate", self)
        duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        duplicate_action.triggered.connect(self.bridge.duplicate_selected)
        delete_action = QAction("Delete", self)
        delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        delete_action.triggered.connect(self.bridge.delete_selected)
        edit_menu.addActions([duplicate_action, delete_action])

        self.bridge.history_state_changed.connect(self._update_undo_redo_actions)
        self._update_undo_redo_actions()

        view_menu = menu.addMenu("&View")
        reset_layout = QAction("Reset Layout", self)
        reset_layout.triggered.connect(self.reset_layout)
        mock_view = QAction("Use Mock Viewport", self)
        mock_view.triggered.connect(lambda: self.viewport_frame.use_mock_viewport())
        view_menu.addActions([reset_layout, mock_view])

        plugins_menu = menu.addMenu("&Plugins")
        plugin_action = QAction("Plugin Manager", self)
        plugin_action.triggered.connect(
            lambda: QMessageBox.information(self, "Plugins", "Plugin registration hook is ready.")
        )
        plugins_menu.addAction(plugin_action)

        tests_menu = menu.addMenu("&Tests")
        play_action = QAction("Play", self)
        play_action.setShortcut(QKeySequence("F5"))
        play_action.triggered.connect(self.bridge.play)
        stop_action = QAction("Stop", self)
        stop_action.setShortcut(QKeySequence("Shift+F5"))
        stop_action.triggered.connect(self.bridge.stop)
        tests_menu.addActions([play_action, stop_action])

        window_menu = menu.addMenu("&Window")
        self.window_menu = window_menu

        help_menu = menu.addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

        right_spacer = QWidget()
        right_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.menuBar().setCornerWidget(self._corner_actions(), Qt.Corner.TopRightCorner)

    def _corner_actions(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(5, 2, 8, 2)
        layout.setSpacing(6)

        collaborate = QPushButton("◉  Collaborate")
        collaborate.setObjectName("TopActionButton")
        collaborate.clicked.connect(
            lambda: self.bridge.log("warning", "Collaboration backend is not attached.")
        )
        assistant = QPushButton("♙  AI Assistant   ●")
        assistant.setObjectName("TopActionButton")
        assistant.clicked.connect(
            lambda: self.bridge.log("warning", "AI Assistant integration hook opened.")
        )
        layout.addWidget(collaborate)
        layout.addWidget(assistant)
        return widget

    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.ribbon = Ribbon(self.bridge)
        self.ribbon.tool_changed.connect(self._on_transform_tool_changed)
        layout.addWidget(self.ribbon)

        self.viewport_frame = ViewportFrame(self.bridge)
        self.script_workspace = script_editor.ScriptEditorWorkspace(self.bridge, self.viewport_frame)
        self.script_workspace.set_icon_provider(self._script_tab_icon)
        self.script_workspace.set_on_document_opened(self._wire_script_document)
        layout.addWidget(self.script_workspace, 1)

        # Stage 3.2 crash fix: this permanent outer stack is created and
        # installed as the central widget HERE, during normal construction --
        # before embed_panda_window() ever runs (main() builds StudioMainWindow
        # first, embeds the native Panda3D viewport into `central`'s subtree
        # second). `central` is added as page 0 and never touched again.
        #
        # The previous approach called install_template_browser() AFTER
        # embed_panda_window(), which made it fall through to the QMainWindow
        # branch: takeCentralWidget() -> addWidget(central) -> setCentralWidget
        # (stack). By the time that ran, embed_panda_window() had already
        # called container.winId(), which forces Qt to realize a real native
        # HWND for `container` and every ancestor up to `central`, and had
        # raw-Win32 SetParent()'d the foreign Panda3D window under that
        # container HWND -- entirely outside Qt's own bookkeeping. Reparenting
        # `central` afterwards (moving it from being studio's direct child to
        # being a child of a brand-new QStackedWidget) made Qt destroy/recreate
        # `central`'s native window during setParent(), orphaning the
        # raw-SetParent'd Panda3D child HWND and causing a native access
        # violation (Fatal Python error: Aborted / Windows fatal exception:
        # access violation) the moment studio.show() actually mapped the
        # corrupted hierarchy.
        #
        # Now: `central` is placed in `self.central_stack` once, up front,
        # and stays there for the window's entire lifetime. embed_panda_window
        # -> install_engine_viewport() -> viewport_frame.install_external_
        # viewport() only ever touches ViewportFrame's OWN internal
        # QStackedWidget (nested inside `central`), never `central` itself or
        # any of its ancestors -- so nothing above the embedding point ever
        # gets reparented again. install_template_browser() is called with
        # host=self.central_stack (an existing QStackedWidget), which takes
        # its isinstance(target, QStackedWidget) branch: it only calls
        # stack.addWidget(page) for the brand-new (not-yet-native) template
        # page -- `central` itself is never touched.
        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(central)
        self.setCentralWidget(self.central_stack)

    def set_start_page(self, page: QWidget) -> None:
        """Adds `page` as an additional page of the permanent central stack
        built in _build_central(). Never reparents the existing editor page;
        safe to call at any time, including after native viewport embedding."""
        self.central_stack.addWidget(page)

    def _script_tab_icon(self, class_name: str) -> Any:
        icon_name = {"Script": "file-text", "LocalScript": "file-text", "ModuleScript": "package"}.get(class_name, "file-text")
        return IconFactory.make(icon_name, 14)

    def _wire_script_document(self, doc: Any) -> None:
        """Keeps the Edit menu's Undo/Redo enabled-state live as THIS
        editor's own text-undo-stack changes (typing never touches scene
        history, so history_state_changed alone would never fire for
        this)."""
        doc.editor.document().undoAvailable.connect(lambda _available: self._update_undo_redo_actions())
        doc.editor.document().redoAvailable.connect(lambda _available: self._update_undo_redo_actions())

    def _make_dock(
        self,
        title: str,
        widget: QWidget,
        area: Qt.DockWidgetArea,
        object_name: str,
        minimum_width: int = 220,
    ) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        dock.setWidget(widget)
        dock.setMinimumWidth(minimum_width)
        self.addDockWidget(area, dock)
        self.window_menu.addAction(dock.toggleViewAction())
        return dock

    def _build_docks(self) -> None:
        self.explorer_panel = ExplorerPanel(self.bridge, self.script_workspace)
        self.inspector_panel = InspectorPanel(self.bridge, self.script_workspace)
        self.output_panel = OutputPanel(self.bridge)
        self.output_panel.set_navigate_callback(self.script_workspace.navigate_to_diagnostic)

        self.explorer_dock = self._make_dock(
            "Explorer",
            self.explorer_panel,
            Qt.DockWidgetArea.LeftDockWidgetArea,
            "ExplorerDock",
            260,
        )
        self.inspector_dock = self._make_dock(
            "Inspector",
            self.inspector_panel,
            Qt.DockWidgetArea.RightDockWidgetArea,
            "InspectorDock",
            330,
        )
        self.output_dock = self._make_dock(
            "Output",
            self.output_panel,
            Qt.DockWidgetArea.BottomDockWidgetArea,
            "OutputDock",
            360,
        )

        self.explorer_dock.setMinimumHeight(320)
        self.inspector_dock.setMinimumHeight(320)
        self.output_dock.setMinimumHeight(185)
        self.resizeDocks([self.explorer_dock, self.inspector_dock], [285, 360], Qt.Orientation.Horizontal)
        self.resizeDocks([self.output_dock], [210], Qt.Orientation.Vertical)

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        status.setSizeGripEnabled(False)
        self.setStatusBar(status)

        self.ready_label = QLabel("Ready")
        self.stats_label = QLabel("Scheduler: 60/fps    |    Cores: 8    |    Memory: 842 MB    |")
        self.live_dot = QLabel("●")
        self.live_dot.setObjectName("LiveDot")

        status.addWidget(self.ready_label, 1)
        status.addPermanentWidget(self.stats_label)
        status.addPermanentWidget(self.live_dot)

    def _build_shortcuts(self) -> None:
        self.focus_shortcut = QShortcut(QKeySequence("F"), self)
        self.focus_shortcut.activated.connect(self.viewport_frame.focus_selected)

        self.property_filter_shortcut = QShortcut(QKeySequence("Ctrl+Shift+P"), self)
        self.property_filter_shortcut.activated.connect(self._focus_property_filter)

        self.console_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.console_shortcut.activated.connect(
            lambda: self.output_panel.tabs.setCurrentIndex(1)
        )

        self.insert_object_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self.insert_object_shortcut.activated.connect(
            lambda: open_insert_object_dialog(self.bridge, self, self.bridge.selected_id)
        )

    def _on_transform_tool_changed(self, tool: str) -> None:
        self.bridge.log("info", f"Active transform tool: {tool.title()}")
        self.bridge.set_transform_mode(tool)

    def _focus_property_filter(self) -> None:
        self.inspector_dock.show()
        self.inspector_dock.raise_()
        self.inspector_panel.filter_edit.setFocus()
        self.inspector_panel.filter_edit.selectAll()

    def _selection_status(self, obj: Optional[SceneObject]) -> None:
        if obj:
            self.ready_label.setText(f"Selected: {obj.parent} > {obj.name}")
        else:
            self.ready_label.setText("Ready")

    def _play_status(self, playing: bool) -> None:
        self.live_dot.setStyleSheet(f"color: {'#55d173' if playing else '#8ed752'};")
        if playing:
            self.ready_label.setText("Simulation running")
        else:
            obj = self.bridge.get_object(self.bridge.selected_id)
            self._selection_status(obj)

    def _update_undo_redo_actions(self) -> None:
        # Stage 3.1: while a LuaCodeEditor has focus, Edit menu Undo/Redo
        # reads and acts on ITS text undo stack, not the scene
        # CommandManager -- see _on_undo_triggered/_on_redo_triggered,
        # which this must stay in sync with (both check focused_editor()).
        # `script_workspace` doesn't exist yet the first time this runs --
        # _build_menu() (which wires history_state_changed to this and
        # calls it once immediately) runs before _build_central() creates
        # it -- hence the getattr guard.
        workspace = getattr(self, "script_workspace", None)
        editor = workspace.focused_editor() if workspace is not None else None
        if editor is not None:
            self.undo_action.setEnabled(editor.document().isUndoAvailable())
            self.undo_action.setText("Undo Typing")
            self.redo_action.setEnabled(editor.document().isRedoAvailable())
            self.redo_action.setText("Redo Typing")
            return

        state = self.bridge.history_state()
        can_undo = bool(state.get("can_undo"))
        can_redo = bool(state.get("can_redo"))
        undo_text = state.get("undo_text") or ""
        redo_text = state.get("redo_text") or ""
        self.undo_action.setEnabled(can_undo)
        self.undo_action.setText(f"Undo {undo_text}" if undo_text else "Undo")
        self.redo_action.setEnabled(can_redo)
        self.redo_action.setText(f"Redo {redo_text}" if redo_text else "Redo")

    def _on_undo_triggered(self) -> None:
        editor = self.script_workspace.focused_editor()
        if editor is not None:
            editor.undo()
            return
        self.bridge.undo()

    def _on_redo_triggered(self) -> None:
        editor = self.script_workspace.focused_editor()
        if editor is not None:
            editor.redo()
            return
        self.bridge.redo()

    def _on_save_triggered(self) -> None:
        # Stage 3.1 behavior preserved exactly: code focused and dirty ->
        # save Source. Stage 3.2 only changes the "otherwise" branch, from
        # the old SceneObject scene file to Save Place.
        doc = self.script_workspace.focused_document()
        if doc is not None:
            doc.save()
            return
        self._place_save()

    def _save_all_scripts(self) -> None:
        # Stage 3.2 spec section 12: Save All = every dirty Source
        # document, THEN the current Place -- in that order, so the Place
        # save captures Source exactly as it was just written.
        count = self.script_workspace.save_all()
        if count:
            self.bridge.log("info", f"Saved {count} Script(s).")
        self._place_save()

    def _handle_play_guard(self) -> bool:
        """Runs before EVERY Play (see EngineBridge.play()/set_play_guard)
        -- the single choke point every Play trigger already shares, so
        this needs no changes at any individual Ribbon button/menu
        action/console-command call site."""
        if not self.script_workspace.has_dirty_documents():
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Script Changes")
        box.setText("Some open Scripts have unsaved changes.")
        save_play_button = box.addButton("Save All and Play", QMessageBox.ButtonRole.AcceptRole)
        play_saved_button = box.addButton("Play Saved Version", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_play_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_play_button:
            self.script_workspace.save_all()
            return True
        if clicked is play_saved_button:
            self.bridge.log("info", "Playing the last saved Source; open Script tabs remain unsaved.")
            return True
        return False

    def _update_title(self, *_args) -> None:
        if self.bridge.live_mode:
            name = self.place_manager.display_name
            dirty = " *" if self.place_manager.is_dirty else ""
        else:
            name = self.current_file.name if self.current_file else "Untitled Scene"
            dirty = " *" if self.bridge.is_dirty else ""
        self.setWindowTitle(f"{name}{dirty} — {APP_NAME}")
        self.viewport_frame.scene_title.setText(name + dirty)

    def maybe_save(self) -> bool:
        """Offline (non-live) demo path only -- see _resolve_dirty_before_
        place_change() for the live Place-based equivalent."""
        if not self.bridge.is_dirty:
            return True
        result = QMessageBox.question(
            self,
            "Unsaved changes",
            "Save changes to the current scene?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if result == QMessageBox.StandardButton.Save:
            return self.save_scene()
        if result == QMessageBox.StandardButton.Cancel:
            return False
        return True

    # ---------------- Stage 3.2: Place workflow (live mode) ----------------

    def _resolve_dirty_before_place_change(self) -> bool:
        """Spec section 11: Script buffers first, then the current Place,
        before New/Open/world-replace/Return-to-Start-Page. Returns False
        only on an explicit Cancel -- caller must abort and leave
        everything exactly as it was."""
        if not self.script_workspace.prompt_save_all_before_closing():
            return False
        if not self.bridge.live_mode:
            return self.maybe_save()
        if not self.place_manager.is_dirty:
            return True
        result = QMessageBox.question(
            self,
            "Unsaved Place",
            f"'{self.place_manager.display_name}' has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if result == QMessageBox.StandardButton.Cancel:
            return False
        if result == QMessageBox.StandardButton.Save:
            return self._place_save()
        return True

    def _reject_while_playing(self, action_description: str) -> bool:
        """Spec section 22: New/Open/Save As/world replacement/Return to
        Start Page are all rejected outright while Play is active, rather
        than silently stopping it out from under the running Lua session.
        Returns True if the action was rejected (caller should abort)."""
        if not self.bridge.is_playing:
            return False
        QMessageBox.information(
            self, "Play in Progress", f"Stop the current Play session before {action_description}.",
        )
        return True

    def _place_new(self) -> None:
        if self._reject_while_playing("starting a new Place"):
            return
        if self.templates_binding is None:
            self.bridge.log("warning", "Template browser is not available.")
            return
        if not self._resolve_dirty_before_place_change():
            return
        self.templates_binding.show()

    def _return_to_start_page(self) -> None:
        if self._reject_while_playing("returning to the Start Page"):
            return
        if self.templates_binding is None:
            self.bridge.log("warning", "Template browser is not available.")
            return
        if not self._resolve_dirty_before_place_change():
            return
        self.templates_binding.show()

    def _place_open(self) -> None:
        if self._reject_while_playing("opening another Place"):
            return
        if not self.bridge.live_mode:
            self.open_scene()
            return
        if not self._resolve_dirty_before_place_change():
            return
        start_dir = str(self.place_manager.current_project_dir or self.place_manager.projects_root)
        path, _ = QFileDialog.getOpenFileName(self, "Open Place", start_dir, SCENE_FILE_FILTER)
        if not path:
            return
        self._open_place_path(Path(path))

    def open_recent_place(self, path: str | Path) -> None:
        """Entry point for a Recents list item -- same validation/replace
        flow as File > Open Place, just skipping the file dialog."""
        if self._reject_while_playing("opening another Place"):
            return
        if not self._resolve_dirty_before_place_change():
            return
        self._open_place_path(Path(path))

    def _open_place_path(self, path: Path) -> None:
        result = self.place_manager.open(path)
        if not result.success:
            QMessageBox.critical(self, "Open Place Failed", result.message)
            self.bridge.log("error", result.message)
            return
        self._replace_world_and_report(result)

    def _replace_world_and_report(self, result: place_manager.PlaceOperationResult) -> None:
        """Sends the prepared (already-validated, already-fresh-id-
        remapped-if-a-template) object list as one REPLACE_WORLD request.
        place_manager.commit() -- which is what actually updates current_
        path/display_name/resets dirty/records Recents -- runs ONLY inside
        the success branch, never speculatively (spec section 8/17: a
        rejected create/open leaves the current Place untouched).

        This is the one chokepoint every successful Place-load path funnels
        through -- template creation, File > Open Place, Recent Places, and
        the --place startup flag all call this (directly, or via
        _open_place_path()) -- which is why activate_scene_for_loaded_place()
        is called from here rather than separately by each caller: a single
        authoritative switch back to Scene, gated on the same real success
        confirmation as everything else in this method, instead of one copy
        per caller that could drift out of sync with it."""
        def _on_result(success: bool, message: str) -> None:
            if success:
                self.place_manager.commit(result)
                self.activate_scene_for_loaded_place()
                self._update_title()
                self.bridge.log("info", result.message)
            else:
                QMessageBox.critical(
                    self, "Place Operation Failed", message or "The server rejected the request.",
                )
                self.bridge.log("error", f"Place operation rejected: {message}")
        accepted = self.bridge.replace_world(result.objects or [], _on_result, result.services)
        if not accepted:
            QMessageBox.critical(self, "Not Connected", "Not connected to a live server.")

    def activate_scene_for_loaded_place(self) -> None:
        """Stage 3.7 defect fix: the single authoritative switch from the
        Templates/Home page back to the Scene workspace after a Place has
        actually finished loading.

        Root cause of the reported bug: TemplateBrowserBinding.show_editor()
        (sstudio_templates.py) is the only thing that ever moves
        central_stack's current widget back to the Scene page -- but before
        this fix it was only ever called from _on_template_activated()
        (client_studio.py, the two Templates-page create flows), never from
        _open_place_path() (File > Open Place, Recent Places, and the
        --place startup flag all funnel through it). A successful Open
        would load the Explorer/Inspector data correctly but leave
        central_stack showing whatever page (almost always Templates/Home)
        was already visible.

        Deliberately a thin wrapper rather than inlined at each call site:
        _replace_world_and_report() above is the only caller, invoked from
        its success branch alone, so a cancelled QFileDialog, a cancelled
        Recents dialog, a local place_manager.open()/create_from_template()
        failure, or a server-rejected REPLACE_WORLD all leave the current
        page untouched -- none of those paths ever reach this method. No-op
        when there's no template browser attached (headless tests, the
        offline --legacy-demo path)."""
        if self.templates_binding is not None:
            self.templates_binding.show_editor()

    def _place_save(self) -> bool:
        if not self.bridge.live_mode:
            return self.save_scene()
        if self.place_manager.current_path is None:
            return self._place_save_as()
        objects = self.bridge.export_world()
        result = self.place_manager.save(objects, self.bridge.export_services())
        if not result.success:
            QMessageBox.critical(self, "Save Failed", result.message)
            self.bridge.log("error", result.message)
            return False
        self._update_title()
        self.bridge.log("info", result.message)
        return True

    def _place_save_as(self) -> bool:
        if not self.bridge.live_mode:
            return self.save_scene_as()
        default_path = self.place_manager.current_path or (self.place_manager.projects_root / "Untitled.nebula.json")
        path, _ = QFileDialog.getSaveFileName(self, "Save Place As", str(default_path), SCENE_FILE_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".nebula.json"
        objects = self.bridge.export_world()
        result = self.place_manager.save_as(path, objects, self.bridge.export_services())
        if not result.success:
            QMessageBox.critical(self, "Save Failed", result.message)
            self.bridge.log("error", result.message)
            return False
        self._update_title()
        self.bridge.log("info", result.message)
        return True

    def save_scene(self) -> bool:
        if self.current_file is None:
            return self.save_scene_as()
        try:
            self.bridge.save_to_file(self.current_file)
            self._update_title()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            self.bridge.log("error", f"Save failed: {exc}")
            return False

    def save_scene_as(self) -> bool:
        initial = str(self.current_file) if self.current_file else "Untitled.nebula.json"
        path, _ = QFileDialog.getSaveFileName(self, "Save Scene", initial, SCENE_FILE_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".nebula.json"
        self.current_file = Path(path)
        return self.save_scene()

    def install_engine_viewport(self, widget: QWidget, adapter: Any = None) -> None:
        """
        Public integration entry point.

        Example:
            engine_widget = MyEngineViewport()
            window.install_engine_viewport(engine_widget, adapter=my_engine)
        """
        self.viewport_frame.install_external_viewport(widget)
        if adapter is not None:
            self.bridge.set_adapter(adapter)

    def reset_layout(self) -> None:
        self.explorer_dock.show()
        self.inspector_dock.show()
        self.output_dock.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.explorer_dock)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.output_dock)
        self.resizeDocks([self.explorer_dock, self.inspector_dock], [285, 360], Qt.Orientation.Horizontal)
        self.resizeDocks([self.output_dock], [210], Qt.Orientation.Vertical)
        self.bridge.log("info", "Editor layout reset.")

    def _restore_layout(self) -> None:
        geometry = self.settings.value("geometry")
        state = self.settings.value("windowState")
        if geometry:
            self.restoreGeometry(geometry)
        if state:
            self.restoreState(state)

    def set_shutdown_callback(self, callback: Any) -> None:
        self.shutdown_callback = callback

    def set_templates_binding(self, binding: Any) -> None:
        self.templates_binding = binding

    def closeEvent(self, event: QCloseEvent) -> None:
        # _resolve_dirty_before_place_change() already resolves dirty
        # Script tabs first, then the current Place (live) or scene file
        # (offline) -- exactly the sequence spec section 11 requires
        # before closing SStudio too.
        if not self._resolve_dirty_before_place_change():
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        if callable(self.shutdown_callback):
            try:
                self.shutdown_callback()
            except Exception as exc:
                self.bridge.log("error", f"Shutdown callback failed: {exc}")
        event.accept()

    def _about(self) -> None:
        QMessageBox.about(
            self,
            APP_NAME,
            "<b>Nebula Studio</b><br><br>"
            "A standalone PySide6 editor shell designed to sit around a Python game engine.<br><br>"
            "The mock viewport can be replaced with any QWidget-based engine viewport.",
        )


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

DARK_STYLE = """
QWidget {
    color: #d0d3d6;
    background: #202122;
    font-size: 12px;
}
QMainWindow {
    background: #1a1b1c;
}
QMenuBar {
    background: #171819;
    border-bottom: 1px solid #2c2d2f;
    padding: 1px 6px;
}
QMenuBar::item {
    background: transparent;
    padding: 5px 8px;
}
QMenuBar::item:selected {
    background: #303235;
    border-radius: 3px;
}
QMenu {
    background: #222326;
    border: 1px solid #3a3c3f;
    padding: 4px;
}
QMenu::item {
    padding: 7px 26px 7px 22px;
}
QMenu::item:selected {
    background: #0877b9;
}
QToolTip {
    color: #e4e7e9;
    background: #151617;
    border: 1px solid #4a4d50;
    padding: 4px;
}
#Ribbon {
    background: #252628;
    border-bottom: 1px solid #343639;
}
#RibbonTabs {
    background: #202123;
    border-bottom: 1px solid #343639;
}
#RibbonTabs::tab {
    background: transparent;
    color: #c9cccf;
    min-width: 78px;
    padding: 9px 8px 8px 8px;
}
#RibbonTabs::tab:selected {
    color: #f0f2f3;
    border-bottom: 2px solid #2daee9;
}
#RibbonTabs::tab:hover {
    background: #2c2e30;
}
#RibbonPages {
    background: #262729;
}
#RibbonGroup {
    background: transparent;
    border-right: 1px solid #393b3e;
}
#RibbonGroupTitle {
    color: #95999d;
    font-size: 10px;
}
RibbonToolButton, QToolButton {
    background: #2b2d2f;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 3px;
}
RibbonToolButton:hover, QToolButton:hover {
    background: #393c3f;
    border-color: #4a4d50;
}
RibbonToolButton:pressed, QToolButton:pressed {
    background: #1f2022;
}
RibbonToolButton:checked, QToolButton:checked {
    background: #35434b;
    border-color: #2aaae8;
}
RibbonToolButton:disabled, QToolButton:disabled {
    color: #666a6d;
    background: #28292b;
}
QPushButton {
    background: #2b2d2f;
    border: 1px solid #424447;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton:hover {
    background: #36383b;
    border-color: #55585b;
}
QPushButton:pressed {
    background: #202123;
}
#TopActionButton {
    background: #202123;
    border: 1px solid #35373a;
    padding: 4px 10px;
}
QLineEdit, QPlainTextEdit, QListWidget, QTreeWidget, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #1c1d1f;
    border: 1px solid #383a3d;
    border-radius: 3px;
    selection-background-color: #0877b9;
    selection-color: white;
}
QLineEdit {
    min-height: 25px;
    padding: 2px 5px;
}
QComboBox, QSpinBox, QDoubleSpinBox {
    min-height: 24px;
    padding: 1px 5px;
}
QComboBox::drop-down {
    border: 0;
    width: 20px;
}
QTreeWidget {
    border: 0;
    background: #202122;
    outline: 0;
}
QTreeWidget::item {
    min-height: 22px;
    border-radius: 2px;
}
QTreeWidget::item:hover {
    background: #2b2d2f;
}
QTreeWidget::item:selected {
    background: #3a3c3f;
    color: #f0f2f3;
}
QListWidget::item {
    padding: 5px;
}
QDockWidget {
    color: #d9dcdf;
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}
QDockWidget::title {
    background: #252628;
    border-bottom: 1px solid #35373a;
    padding: 7px 9px;
    text-align: left;
}
QDockWidget > QWidget {
    border: 1px solid #303235;
}
QTabWidget::pane {
    border: 1px solid #313336;
    background: #202122;
}
QTabBar::tab {
    background: #252628;
    color: #b8bbbe;
    padding: 7px 14px;
    border-right: 1px solid #323437;
}
QTabBar::tab:selected {
    background: #202122;
    color: #f0f2f3;
    border-bottom: 2px solid #2aaee9;
}
QTabBar::tab:hover {
    background: #2d2f31;
}
QScrollArea {
    border: 0;
}
QScrollBar:vertical {
    background: #1c1d1f;
    width: 11px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #474a4d;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #5a5d61;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: #1c1d1f;
    height: 11px;
}
QScrollBar::handle:horizontal {
    background: #474a4d;
    border-radius: 5px;
    min-width: 24px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
#InspectorObjectName {
    font-weight: 600;
    color: #dde0e2;
}
#InspectorSectionHeader {
    background: #2b2d2f;
    border: 0;
    border-top: 1px solid #3b3d40;
    border-bottom: 1px solid #242527;
    border-radius: 0;
    text-align: left;
    font-weight: 600;
    padding: 6px 7px;
}
#InspectorSectionHeader:hover {
    background: #333538;
}
#SceneTabBar {
    background: #222325;
    border-bottom: 1px solid #35373a;
    min-height: 29px;
}
#ViewportToolbar {
    background: #252729;
    border-bottom: 1px solid #343639;
}
#MutedLabel {
    color: #8f9498;
    padding: 12px;
}
#ProfilerLabel {
    font-family: "JetBrains Mono", "Consolas", monospace;
    color: #b9bec1;
}
QStatusBar {
    background: #1d1e20;
    border-top: 1px solid #343639;
    color: #b4b8bb;
}
QStatusBar::item {
    border: 0;
}
#LiveDot {
    color: #8ed752;
    padding-right: 7px;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid #55585b;
    border-radius: 2px;
    background: #1b1c1e;
}
QCheckBox::indicator:checked {
    background: #1386c8;
    border-color: #2baeea;
}
"""


def main() -> int:
    print(f"Starting {APP_NAME} build {BUILD_ID}")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setStyleSheet(DARK_STYLE)

    window = StudioMainWindow()
    window.show()

    # Let all dock widgets finish their first layout pass.
    QTimer.singleShot(0, lambda: window.bridge.log("info", "Editor UI initialized."))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())