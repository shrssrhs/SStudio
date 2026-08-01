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
from typing import Any, Iterable, Optional

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
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
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

from shared import object_registry
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
            "properties",
        }
        clean = {key: value for key, value in data.items() if key in allowed}
        clean["position"] = Vec3.from_value(clean.get("position"))
        clean["rotation"] = Vec3.from_value(clean.get("rotation"))
        clean["scale"] = Vec3.from_value(clean.get("scale", {"x": 1, "y": 1, "z": 1}))
        clean["size"] = Vec3.from_value(clean.get("size", {"x": 4, "y": 4, "z": 4}))
        return cls(**clean)


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
        self.selected_id: Optional[str] = None
        self.is_playing = False
        self.is_dirty = False
        self.adapter: Any = None

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
        return next((obj for obj in self.objects if obj.id == object_id), None)

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

        old_value = self._read_property(obj, property_path)
        if not self._write_property(obj, property_path, value):
            return

        accepted = True
        if self.live_mode:
            accepted = bool(
                self._adapter_call(
                    "set_property",
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

    def play(self) -> None:
        if self.is_playing:
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

    def load_from_file(self, path: str | Path) -> None:
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw = self._migrate_scene_data(raw)
        object_data = raw.get("objects", [])
        if not isinstance(object_data, list):
            raise ValueError("Scene file has no valid 'objects' list.")
        objects = [SceneObject.from_dict(item) for item in object_data]

        if self.live_mode:
            accepted = bool(self._adapter_call("import_scene", objects, default=False))
            if not accepted:
                raise RuntimeError("The live client does not support importing a scene snapshot.")
            return

        self.replace_scene(objects, mark_dirty=False)
        self.log("info", f"Scene loaded: {source.name}")

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
        model_group.add_widget(RibbonToolButton("Mesh", "mesh", lambda: self.bridge.add_part("Mesh")))
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

class ExplorerPanel(QWidget):
    SYSTEM_PROTECTED_TYPES = {"Baseplate", "Camera", "Lighting", "Terrain"}

    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
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

        self.tree = QTreeWidget()
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

    def rebuild(self) -> None:
        self._syncing = True
        self.tree.clear()

        roots: dict[str, QTreeWidgetItem] = {}
        object_items: dict[str, QTreeWidgetItem] = {}

        for root_name in ROOT_SERVICES:
            root = QTreeWidgetItem([root_name])
            root.setData(0, Qt.ItemDataRole.UserRole, None)
            root.setIcon(0, self._icon_for_root(root_name))
            roots[root_name] = root
            self.tree.addTopLevelItem(root)

        for obj in self.bridge.objects:
            item = QTreeWidgetItem([obj.name])
            item.setData(0, Qt.ItemDataRole.UserRole, obj.id)
            item.setIcon(0, self._icon_for_type(obj.object_type, obj.color))
            if not obj.id.startswith("system:"):
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
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
            if top.text(0) in {"Workspace", "Players"}:
                top.setExpanded(True)

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
        if item is not None and item.data(0, Qt.ItemDataRole.UserRole):
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
    # ДВОЙНОЙ КЛИК (заглушка редактора кода для Script-типов)
    # --------------------------------------------------------

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        object_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not object_id:
            return
        obj = self.bridge.get_object(object_id)
        if obj is None:
            return
        definition = object_registry.get_object_type(obj.object_type)
        if definition is not None and definition.category == "Scripting":
            QMessageBox.information(
                self, obj.name, "Code editor will be implemented in the next stage."
            )

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

        if object_id:
            obj = self.bridge.get_object(object_id)
            is_system = object_id.startswith("system:")
            is_protected = obj is not None and obj.object_type in self.SYSTEM_PROTECTED_TYPES
            menu.addSeparator()

            rename_action = menu.addAction("Rename")
            rename_action.setEnabled(not is_system)
            rename_action.triggered.connect(lambda: self.tree.editItem(item))

            duplicate_action = menu.addAction("Duplicate")
            duplicate_action.setEnabled(not is_system)
            duplicate_action.triggered.connect(
                lambda: (self.bridge.select(object_id), self.bridge.duplicate_selected())
            )

            delete_action = menu.addAction("Delete")
            delete_action.setEnabled(not is_system and not is_protected)
            delete_action.triggered.connect(
                lambda: (self.bridge.select(object_id), self.bridge.delete_selected())
            )

        menu.exec(self.tree.viewport().mapToGlobal(position))


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
    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.current_object: Optional[SceneObject] = None
        self._building = False

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

        definition = object_registry.get_object_type(obj.object_type)
        icon_kind = definition.icon if definition is not None else "cube"
        icon_tint = QColor(obj.color) if (definition is None or definition.has_3d_entity) else QColor("#b9bec2")
        pix = IconFactory.make(icon_kind, 18, icon_tint).pixmap(18, 18)
        self.object_icon.setPixmap(pix)
        self.object_name.setText(obj.name)
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

    def _build_transform_section(self, obj: SceneObject) -> CollapsibleSection:
        transform = CollapsibleSection("Transform")
        position = VectorEditor(obj.position, "position")
        rotation = VectorEditor(obj.rotation, "rotation")
        scale = VectorEditor(obj.scale, "scale")
        size = VectorEditor(obj.size, "size")
        for editor in (position, rotation, scale, size):
            editor.value_changed.connect(self._set_value)

        transform.add_row("Position", position)
        transform.add_row("Rotation", rotation)
        transform.add_row("Scale", scale)
        transform.add_row("Size", size)
        return transform

    def _build_appearance_section(self, obj: SceneObject) -> CollapsibleSection:
        appearance = CollapsibleSection("Appearance")
        color = ColorField(obj.color)
        color.color_selected.connect(lambda value: self._set_value("color", value))

        material = QComboBox()
        material.addItems(["Plastic", "Metal", "Wood", "Glass", "Concrete", "Neon"])
        material.setCurrentText(obj.material)
        material.currentTextChanged.connect(lambda value: self._set_value("material", value))

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
        open_button.clicked.connect(
            lambda: QMessageBox.information(
                self, obj.name, "Code editor will be implemented in the next stage."
            )
        )
        script.add_row("", open_button)
        return script

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

    def _external_property_changed(self, object_id: str, path: str, value: Any) -> None:
        if self.current_object and self.current_object.id == object_id and not self._building:
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

class OutputPanel(QWidget):
    def __init__(self, bridge: EngineBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge

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
        clear_button.clicked.connect(lambda: self.output.clear())
        filters.addWidget(self.message_filter)
        filters.addWidget(self.context_filter)
        filters.addStretch(1)
        filters.addWidget(clear_button)
        output_layout.addLayout(filters)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(5000)
        self.output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.output.setFont(QFont("JetBrains Mono, Consolas, monospace", 9))
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
        self.bridge.scene_changed.connect(self._update_profiler)
        self._update_profiler()
        self.append_log("info", "Scene loaded successfully. (0.38s)")
        self.append_log("info", "Workspace ready.")

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
        self.console_log.appendPlainText(f"{stamp} [{level_name}] {message}")

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
            self.output.clear()
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
        self.current_file: Optional[Path] = None
        self.settings = QSettings(ORG_NAME, APP_NAME)

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
        self.bridge.selection_changed.connect(self._selection_status)
        self.bridge.play_state_changed.connect(self._play_status)

        self._restore_layout()
        if not self.bridge.live_mode:
            self.bridge.select(next((obj.id for obj in self.bridge.objects if obj.name == "GreyBlock_B"), None))
        self._update_title()

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        new_action = QAction("New Scene", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_scene)
        open_action = QAction("Open Scene…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_scene)
        save_action = QAction("Save", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_scene)
        save_as_action = QAction("Save As…", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self.save_scene_as)
        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)

        file_menu.addActions([new_action, open_action, save_action, save_as_action])
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        edit_menu = menu.addMenu("&Edit")
        duplicate_action = QAction("Duplicate", self)
        duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        duplicate_action.triggered.connect(self.bridge.duplicate_selected)
        delete_action = QAction("Delete", self)
        delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        delete_action.triggered.connect(self.bridge.delete_selected)
        edit_menu.addActions([duplicate_action, delete_action])

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
        layout.addWidget(self.viewport_frame, 1)
        self.setCentralWidget(central)

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
        self.explorer_panel = ExplorerPanel(self.bridge)
        self.inspector_panel = InspectorPanel(self.bridge)
        self.output_panel = OutputPanel(self.bridge)

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

    def _update_title(self, *_args) -> None:
        name = self.current_file.name if self.current_file else "Untitled Scene"
        dirty = " *" if self.bridge.is_dirty else ""
        self.setWindowTitle(f"{name}{dirty} — {APP_NAME}")
        self.viewport_frame.scene_title.setText(name + dirty)

    def maybe_save(self) -> bool:
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

    def new_scene(self) -> None:
        if not self.maybe_save():
            return
        self.bridge.new_scene()
        self.current_file = None
        self._update_title()

    def open_scene(self) -> None:
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open Scene", "", SCENE_FILE_FILTER)
        if not path:
            return
        try:
            self.bridge.load_from_file(path)
            self.current_file = Path(path)
            self._update_title()
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            self.bridge.log("error", f"Open failed: {exc}")

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

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.maybe_save():
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