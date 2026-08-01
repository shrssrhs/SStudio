from __future__ import annotations

import builtins
import math
from typing import Optional

from panda3d.core import LineSegs, NodePath, Point2, Point3
from ursina import Cone, Entity, Vec3, camera, color, mouse, window
from ursina.scene import instance as scene


# ============================================================
# ОТЛАДКА
# ============================================================

# Логирует ось, стартовое значение, дельту и финальное значение при
# drag. Временный инструмент диагностики — включать только вручную.
DEBUG_GIZMO = False


# ============================================================
# ГЕОМЕТРИЯ И ВНЕШНИЙ ВИД
# ============================================================

AXIS_ORDER = ("x", "y", "z")
_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

AXIS_DIRECTION = {
    "x": Vec3(1, 0, 0),
    "y": Vec3(0, 1, 0),
    "z": Vec3(0, 0, 1),
}

# Ursina инвертирует rotation.x/rotation.y относительно "естественного"
# математического поворота вокруг мировой оси (см. rotation_directions
# в ursina/entity.py и уже существующий комментарий в client_studio.py
# про camera_pitch_pivot: "Отрицательный знак нужен для направления оси
# X камеры Ursina"). Поэтому знак посчитанного угла нужно скорректировать
# так же, иначе Rotate X/Y будет крутить в обратную сторону.
ROTATION_SIGN = {"x": -1.0, "y": -1.0, "z": 1.0}

AXIS_COLOR = {
    "x": color.rgb32(224, 62, 62),
    "y": color.rgb32(70, 200, 90),
    "z": color.rgb32(64, 122, 232),
}
AXIS_HIGHLIGHT_COLOR = {
    "x": color.rgb32(255, 170, 170),
    "y": color.rgb32(180, 255, 190),
    "z": color.rgb32(175, 205, 255),
}

# Стрелка (Move) строится указывающей по умолчанию вдоль +Z (тот же
# приём, что и у уже существующей стрелки прицела AIM_ARROW в
# client_studio.py), а затем весь pivot поворачивается на нужную ось.
_ARROW_ROTATION = {
    "z": Vec3(0, 0, 0),
    "x": Vec3(0, 90, 0),
    "y": Vec3(-90, 0, 0),
}

ARROW_LENGTH = 1.0
ARROW_SHAFT_RADIUS = 0.045
ARROW_TIP_LENGTH = 0.28
ARROW_TIP_RADIUS = 0.14
ARROW_HIT_TOLERANCE = 0.45
ARROW_HIT_MARGIN = 0.15

RING_RADIUS = 1.15
RING_SEGMENTS = 48
RING_LINE_THICKNESS = 3.0
RING_HIT_TOLERANCE = 0.40

# Кольцо (Rotate) рисуется в плоскости, перпендикулярной своей оси.
_RING_BASIS = {
    "x": (Vec3(0, 1, 0), Vec3(0, 0, 1)),
    "y": (Vec3(1, 0, 0), Vec3(0, 0, 1)),
    "z": (Vec3(1, 0, 0), Vec3(0, 1, 0)),
}

# Gizmo масштабируется каждый кадр по расстоянию до камеры, чтобы
# сохранять примерно одинаковый размер на экране независимо от зума.
GIZMO_SCREEN_SCALE = 0.16
GIZMO_MIN_SCALE = 0.05
GIZMO_MAX_SCALE = 50.0

ROTATE_SNAP_DEGREES = 5.0
DEFAULT_MOVE_SNAP = 0.25


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


# ============================================================
# МАТЕМАТИКА
# ============================================================

def line_line_closest(
    a0: Vec3,
    ad: Vec3,
    b0: Vec3,
    bd: Vec3,
    parallel_epsilon: float = 1e-4,
) -> Optional[tuple[float, float]]:
    """
    Параметры ближайшего сближения двух прямых: A(s) = a0 + ad*s,
    B(t) = b0 + bd*t. ad и bd должны быть единичными. Возвращает
    None, если прямые почти параллельны (вызывающий код должен
    использовать резервную drag-plane логику).
    """
    w0 = a0 - b0
    a = ad.dot(ad)
    b = ad.dot(bd)
    c = bd.dot(bd)
    d = ad.dot(w0)
    e = bd.dot(w0)
    denom = a * c - b * b
    if abs(denom) < parallel_epsilon:
        return None
    s = (b * e - c * d) / denom
    t = (a * e - b * d) / denom
    return s, t


def ray_plane_intersect(
    ray_origin: Vec3,
    ray_direction: Vec3,
    plane_point: Vec3,
    plane_normal: Vec3,
    epsilon: float = 1e-6,
) -> Optional[Vec3]:
    denom = ray_direction.dot(plane_normal)
    if abs(denom) < epsilon:
        return None
    t = (plane_point - ray_origin).dot(plane_normal) / denom
    if t < 0:
        return None
    return ray_origin + ray_direction * t


def mouse_world_ray() -> tuple[Optional[Vec3], Optional[Vec3]]:
    """
    Мировой ray из камеры через текущую позицию курсора (mouse.x/y).
    Использует Panda3D lens.extrude() напрямую — независимо от того,
    встроен viewport в Qt или нет, т.к. опирается только на позицию
    курсора внутри окна Panda3D (mouse.x/mouse.y), а не на относительный
    Qt-инпут камеры.
    """
    lens = camera.lens
    near_point = Point3()
    far_point = Point3()
    film_x = mouse.x * 2 / window.aspect_ratio
    film_y = mouse.y * 2

    if not lens.extrude(Point2(film_x, film_y), near_point, far_point):
        return None, None

    render_root = builtins.render
    world_near = Vec3(render_root.get_relative_point(camera, near_point))
    world_far = Vec3(render_root.get_relative_point(camera, far_point))
    direction = world_far - world_near
    length = direction.length()
    if length <= 1e-9:
        return None, None

    return world_near, direction / length


# ============================================================
# TRANSFORM GIZMO
# ============================================================

class TransformGizmo:
    """
    Move/Rotate gizmo для Studio-редактора. Создаётся один раз и
    переиспользуется — привязка к объекту меняется через set_target().
    Не использует коллайдеры Ursina, поэтому не попадает в обычный
    mouse.hovered_entity / выбор Part.
    """

    def __init__(self) -> None:
        self.root = Entity(
            parent=scene,
            name="transform_gizmo_root",
            enabled=False,
            eternal=True,
        )
        # Gizmo всегда поверх сцены: не тестируем и не пишем в depth
        # buffer, рисуем в bin с высоким sort — геометрия сцены его не
        # перекрывает.
        self.root.setDepthTest(False)
        self.root.setDepthWrite(False)
        self.root.setBin("fixed", 100)

        self.move_group = Entity(parent=self.root, enabled=False, eternal=True)
        self.rotate_group = Entity(parent=self.root, enabled=False, eternal=True)

        self._arrow_parts: dict[str, tuple[Entity, Entity]] = {}
        for axis in AXIS_ORDER:
            pivot = Entity(
                parent=self.move_group,
                rotation=_ARROW_ROTATION[axis],
                eternal=True,
            )
            shaft = Entity(
                parent=pivot,
                model="cube",
                color=AXIS_COLOR[axis],
                unlit=True,
                scale=(ARROW_SHAFT_RADIUS, ARROW_SHAFT_RADIUS, ARROW_LENGTH),
                position=(0, 0, ARROW_LENGTH / 2),
                eternal=True,
            )
            tip = Entity(
                parent=pivot,
                model=Cone(resolution=10),
                color=AXIS_COLOR[axis],
                unlit=True,
                scale=(ARROW_TIP_RADIUS, ARROW_TIP_RADIUS, ARROW_TIP_LENGTH),
                position=(0, 0, ARROW_LENGTH + ARROW_TIP_LENGTH / 2),
                rotation_x=90,
                eternal=True,
            )
            self._arrow_parts[axis] = (shaft, tip)

        self._ring_nodes: dict[str, NodePath] = {}
        for axis in AXIS_ORDER:
            self._ring_nodes[axis] = self._build_ring(axis)

        self._mode = "select"
        self._target: Optional[Entity] = None
        self._hovered_axis: Optional[str] = None
        self._dragging = False
        self._drag_axis: Optional[str] = None
        self._drag_anchor = Vec3(0, 0, 0)
        self._drag_start_position = Vec3(0, 0, 0)
        self._drag_start_rotation = Vec3(0, 0, 0)
        self._drag_start_vector = Vec3(1, 0, 0)
        self._last_result: Optional[tuple[str, Vec3]] = None
        self._current_scale = 1.0

    def _build_ring(self, axis: str) -> NodePath:
        segs = LineSegs(f"gizmo_rotate_{axis}")
        segs.setThickness(RING_LINE_THICKNESS)
        segs.setColor(AXIS_COLOR[axis])
        u, v = _RING_BASIS[axis]

        first = True
        for i in range(RING_SEGMENTS + 1):
            angle = 2.0 * math.pi * i / RING_SEGMENTS
            point = u * (RING_RADIUS * math.cos(angle)) + v * (RING_RADIUS * math.sin(angle))
            if first:
                segs.moveTo(point.x, point.y, point.z)
                first = False
            else:
                segs.drawTo(point.x, point.y, point.z)

        node_path = NodePath(segs.create())
        node_path.reparentTo(self.rotate_group)
        node_path.setLightOff()
        return node_path

    # --------------------------------------------------------
    # СОСТОЯНИЕ
    # --------------------------------------------------------

    @property
    def current_target(self) -> Optional[Entity]:
        return self._target

    @property
    def dragging(self) -> bool:
        return self._dragging

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("select", "move", "rotate", "scale"):
            mode = "select"
        self._mode = mode
        self._dragging = False
        self._drag_axis = None
        self._hovered_axis = None

        has_target = self._target is not None
        self.move_group.enabled = has_target and mode == "move"
        self.rotate_group.enabled = has_target and mode == "rotate"
        self._reset_colors()

    def set_target(self, entity: Optional[Entity]) -> None:
        if entity is self._target:
            return

        self._target = entity
        self._dragging = False
        self._drag_axis = None
        self._hovered_axis = None

        has_target = entity is not None
        self.root.enabled = has_target
        self.move_group.enabled = has_target and self._mode == "move"
        self.rotate_group.enabled = has_target and self._mode == "rotate"
        self._reset_colors()

        if has_target:
            self.refresh_transform(camera.world_position)

    def refresh_transform(self, camera_position: Vec3) -> None:
        if self._target is None:
            return

        origin = Vec3(self._target.position)
        self.root.position = origin

        distance = (Vec3(camera_position) - origin).length()
        scale = _clamp(distance * GIZMO_SCREEN_SCALE, GIZMO_MIN_SCALE, GIZMO_MAX_SCALE)
        self.root.scale = Vec3(scale, scale, scale)
        self._current_scale = scale

    # --------------------------------------------------------
    # ПОДСВЕТКА
    # --------------------------------------------------------

    def _reset_colors(self) -> None:
        for axis in AXIS_ORDER:
            active = axis == self._hovered_axis or axis == self._drag_axis
            col = AXIS_HIGHLIGHT_COLOR[axis] if active else AXIS_COLOR[axis]
            shaft, tip = self._arrow_parts[axis]
            shaft.color = col
            tip.color = col
            self._ring_nodes[axis].setColor(col, 1)

    def _set_hovered(self, axis: Optional[str]) -> None:
        if axis == self._hovered_axis:
            return
        self._hovered_axis = axis
        self._reset_colors()

    def update_hover(self, ray_origin: Vec3, ray_direction: Vec3) -> None:
        if self._target is None or self._dragging:
            return
        if self._mode == "move":
            test = self._hit_test_arrow
        elif self._mode == "rotate":
            test = self._hit_test_ring
        else:
            self._set_hovered(None)
            return

        origin = Vec3(self._target.position)
        best_axis, _ = self._best_axis_hit(test, origin, ray_origin, ray_direction)
        self._set_hovered(best_axis)

    # --------------------------------------------------------
    # HIT-TEST
    # --------------------------------------------------------

    def _best_axis_hit(self, test, origin, ray_origin, ray_direction):
        best_axis = None
        best_metric = None
        for axis in AXIS_ORDER:
            metric = test(axis, origin, ray_origin, ray_direction)
            if metric is not None and (best_metric is None or metric < best_metric):
                best_metric, best_axis = metric, axis
        return best_axis, best_metric

    def _hit_test_arrow(self, axis, origin, ray_origin, ray_direction) -> Optional[float]:
        scale = self._current_scale
        ad = AXIS_DIRECTION[axis]
        result = line_line_closest(origin, ad, ray_origin, ray_direction)
        if result is None:
            return None
        s, t = result
        if t < 0:
            return None
        min_s = -ARROW_HIT_MARGIN * scale
        max_s = (ARROW_LENGTH + ARROW_TIP_LENGTH + ARROW_HIT_MARGIN) * scale
        if s < min_s or s > max_s:
            return None
        point_on_axis = origin + ad * s
        point_on_ray = ray_origin + ray_direction * t
        perp_distance = (point_on_axis - point_on_ray).length()
        if perp_distance > ARROW_HIT_TOLERANCE * scale:
            return None
        return perp_distance

    def _hit_test_ring(self, axis, origin, ray_origin, ray_direction) -> Optional[float]:
        scale = self._current_scale
        normal = AXIS_DIRECTION[axis]
        hit = ray_plane_intersect(ray_origin, ray_direction, origin, normal)
        if hit is None:
            return None
        distance_from_center = (hit - origin).length()
        radius = RING_RADIUS * scale
        delta = abs(distance_from_center - radius)
        if delta > RING_HIT_TOLERANCE * scale:
            return None
        return delta

    def _fallback_plane_normal(self, axis: str, ray_direction: Vec3) -> Vec3:
        candidates = [a for a in AXIS_ORDER if a != axis]
        best = candidates[0]
        best_dot = abs(AXIS_DIRECTION[best].dot(ray_direction))
        for candidate in candidates[1:]:
            d = abs(AXIS_DIRECTION[candidate].dot(ray_direction))
            if d > best_dot:
                best_dot = d
                best = candidate
        return AXIS_DIRECTION[best]

    # --------------------------------------------------------
    # DRAG
    # --------------------------------------------------------

    def begin_drag(self, ray_origin: Vec3, ray_direction: Vec3) -> bool:
        if self._target is None or self._mode not in ("move", "rotate"):
            return False

        origin = Vec3(self._target.position)
        if self._mode == "move":
            axis, _ = self._best_axis_hit(self._hit_test_arrow, origin, ray_origin, ray_direction)
        else:
            axis, _ = self._best_axis_hit(self._hit_test_ring, origin, ray_origin, ray_direction)

        if axis is None:
            return False

        self._drag_axis = axis
        self._drag_anchor = origin
        self._last_result = None

        if self._mode == "move":
            self._drag_start_position = Vec3(self._target.position)
            if DEBUG_GIZMO:
                print(f"[GIZMO] begin_drag move axis={axis} start={self._drag_start_position}")
        else:
            normal = AXIS_DIRECTION[axis]
            hit = ray_plane_intersect(ray_origin, ray_direction, origin, normal)
            if hit is None:
                return False
            start_vector = hit - origin
            if start_vector.length() <= 1e-6:
                return False
            self._drag_start_vector = start_vector.normalized()
            self._drag_start_rotation = Vec3(self._target.rotation)
            if DEBUG_GIZMO:
                print(f"[GIZMO] begin_drag rotate axis={axis} start={self._drag_start_rotation}")

        self._dragging = True
        self._hovered_axis = axis
        self._reset_colors()
        return True

    def update_drag(
        self,
        ray_origin: Vec3,
        ray_direction: Vec3,
        move_snap_size: float,
        snap_enabled: bool,
    ) -> Optional[tuple[str, Vec3]]:
        if not self._dragging or self._target is None or self._drag_axis is None:
            return None

        axis = self._drag_axis

        if self._mode == "move":
            result = self._update_move_drag(axis, ray_origin, ray_direction, move_snap_size, snap_enabled)
        elif self._mode == "rotate":
            result = self._update_rotate_drag(axis, ray_origin, ray_direction, snap_enabled)
        else:
            result = None

        if result is not None:
            self._last_result = result
        return result

    def _update_move_drag(self, axis, ray_origin, ray_direction, move_snap_size, snap_enabled):
        ad = AXIS_DIRECTION[axis]
        result = line_line_closest(self._drag_anchor, ad, ray_origin, ray_direction)

        if result is not None:
            s, t = result
            if t < 0:
                result = None

        if result is None:
            plane_normal = self._fallback_plane_normal(axis, ray_direction)
            hit = ray_plane_intersect(ray_origin, ray_direction, self._drag_anchor, plane_normal)
            if hit is None:
                return None
            s = (hit - self._drag_anchor).dot(ad)
        else:
            s, _t = result

        if snap_enabled and move_snap_size > 0:
            s = round(s / move_snap_size) * move_snap_size

        new_position = self._drag_start_position + ad * s
        self._target.position = new_position

        if DEBUG_GIZMO:
            print(f"[GIZMO] move axis={axis} delta={s:.4f} value={new_position}")

        return "Position", Vec3(new_position)

    def _update_rotate_drag(self, axis, ray_origin, ray_direction, snap_enabled):
        normal = AXIS_DIRECTION[axis]
        hit = ray_plane_intersect(ray_origin, ray_direction, self._drag_anchor, normal)
        if hit is None:
            return None

        current_vector = hit - self._drag_anchor
        if current_vector.length() <= 1e-6:
            return None
        current_vector = current_vector.normalized()

        cross = self._drag_start_vector.cross(current_vector)
        signed_sin = cross.dot(normal)
        cos_angle = self._drag_start_vector.dot(current_vector)
        angle_degrees = math.degrees(math.atan2(signed_sin, cos_angle))
        angle_degrees *= ROTATION_SIGN[axis]

        if snap_enabled:
            angle_degrees = round(angle_degrees / ROTATE_SNAP_DEGREES) * ROTATE_SNAP_DEGREES

        index = _AXIS_INDEX[axis]
        new_rotation = Vec3(self._drag_start_rotation)
        new_rotation[index] = self._drag_start_rotation[index] + angle_degrees
        self._target.rotation = new_rotation

        if DEBUG_GIZMO:
            print(f"[GIZMO] rotate axis={axis} delta={angle_degrees:.2f} value={new_rotation[index]:.2f}")

        return "Rotation", Vec3(new_rotation)

    def end_drag(self) -> Optional[tuple[str, Vec3]]:
        if not self._dragging:
            return None

        result = self._last_result
        axis = self._drag_axis

        self._dragging = False
        self._drag_axis = None
        self._last_result = None
        self._hovered_axis = None
        self._reset_colors()

        if DEBUG_GIZMO and result is not None:
            kind, vector = result
            print(f"[GIZMO] end_drag axis={axis} final {kind}={vector}")

        return result
