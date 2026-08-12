from __future__ import annotations

import builtins
import math
from typing import Optional, Union

from panda3d.core import LineSegs, NodePath, Point2, Point3, Quat
from ursina import Cone, Entity, Vec3, camera, color, mouse, window
from ursina.scene import instance as scene

from shared.instance import MIN_PART_SIZE


# ============================================================
# ОТЛАДКА
# ============================================================

# Логирует ось, стартовое значение, дельту и финальное значение при
# drag. Временный инструмент диагностики — включать только вручную.
DEBUG_GIZMO = False

# Stage 2.3 fix: логирует uniform-scale drag-baseline (drag-start Size,
# initial/current reference distance, raw delta, raw/snapped factor,
# resulting Size) — добавлено при расследовании "instant jump on
# mouse-down" бага в центральном handle. Временный флаг, по умолчанию
# False.
DEBUG_SCALE_GIZMO = False


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

# Stage 2.3: Scale-хендлы — маленькие кубики на концах осевых "шафтов"
# (та же дистанция ARROW_LENGTH, что и у стрелок Move, для визуальной
# согласованности) плюс один центральный uniform-хендл. Не переиспользуют
# ARROW_HIT_TOLERANCE/RING_HIT_TOLERANCE — это отдельные точечные хендлы,
# не целый шафт/кольцо, поэтому у них свой (меньший) радиус хит-теста.
SCALE_HANDLE_DISTANCE = ARROW_LENGTH
SCALE_HANDLE_SIZE = 0.16
SCALE_UNIFORM_HANDLE_SIZE = 0.22
SCALE_HANDLE_HIT_RADIUS = 0.32
# Порог, ниже которого стартовая дистанция "луч-до-центра" для uniform-
# хендла считается вырожденной (камера смотрит почти точно на pivot) —
# иначе коэффициент масштабирования мог бы улететь в бесконечность.
SCALE_UNIFORM_MIN_START_DISTANCE = 0.05

# Bug-report follow-up (uniform handle "instant jump on mouse-down",
# still reported present after the delta-based factor fix in 4bf1331):
# a hard pixel-space dead zone for the UNIFORM handle only. Until the
# physical mouse has moved at least this many screen pixels from its
# press-time position, _update_uniform_scale_drag returns None — no
# Size/Position write of ANY kind happens, not even a "factor==1.0,
# same value" write. This is deliberately independent of (on top of)
# the world-space delta math: even if some other factor entirely (a
# second write path, a stale process, camera movement) were still
# producing a visible jump, gating the write itself on real pixel
# movement makes that structurally impossible for the uniform handle.
# Axis handles are NOT touched.
SCALE_UNIFORM_DEAD_ZONE_PIXELS = 3.0

SCALE_HANDLE_COLOR = color.rgb32(225, 225, 225)
SCALE_HANDLE_HIGHLIGHT_COLOR = color.rgb32(255, 230, 90)

# Ключи хендлов Scale: одна из осей + знак направления ("x+","x-",...),
# или "uniform" для центрального хендла. AXIS_DIRECTION[axis] * sign даёт
# единичный вектор направления хендла В ЛОКАЛЬНОЙ (до поворота root)
# системе координат.
_SCALE_AXIS_HANDLE_KEYS = tuple(f"{axis}{sign}" for axis in AXIS_ORDER for sign in ("+", "-"))
_SCALE_UNIFORM_KEY = "uniform"


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
        self.scale_group = Entity(parent=self.root, enabled=False, eternal=True)

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

        # Scale-хендлы: маленький кубик на конце каждого +/-axis шафта,
        # плюс один центральный uniform-хендл. Позиции заданы в ЛОКАЛЬНОЙ
        # (до поворота root) системе координат — refresh_transform()
        # поворачивает root целиком на rotation цели в Scale-режиме, так
        # что хендлы визуально следуют реальным локальным осям объекта
        # (см. отчёт Stage 2.3: "не считать, что локальные оси совпадают
        # с мировыми X/Y/Z").
        self._scale_handles: dict[str, Entity] = {}
        for axis in AXIS_ORDER:
            axis_dir = AXIS_DIRECTION[axis]
            for sign_symbol, sign_value in (("+", 1.0), ("-", -1.0)):
                key = f"{axis}{sign_symbol}"
                handle = Entity(
                    parent=self.scale_group,
                    model="cube",
                    color=AXIS_COLOR[axis],
                    unlit=True,
                    scale=SCALE_HANDLE_SIZE,
                    position=axis_dir * (SCALE_HANDLE_DISTANCE * sign_value),
                    eternal=True,
                )
                self._scale_handles[key] = handle

        self._uniform_handle = Entity(
            parent=self.scale_group,
            model="cube",
            color=SCALE_HANDLE_COLOR,
            unlit=True,
            scale=SCALE_UNIFORM_HANDLE_SIZE,
            position=Vec3(0, 0, 0),
            eternal=True,
        )
        self._scale_handles[_SCALE_UNIFORM_KEY] = self._uniform_handle

        self._mode = "select"
        self._target: Optional[Entity] = None
        self._allow_axis_scale = True
        self._hovered_axis: Optional[str] = None
        self._hovered_scale_handle: Optional[str] = None
        self._dragging = False
        self._drag_axis: Optional[str] = None
        self._drag_scale_handle: Optional[str] = None
        self._drag_anchor = Vec3(0, 0, 0)
        self._drag_start_position = Vec3(0, 0, 0)
        self._drag_start_rotation = Vec3(0, 0, 0)
        self._drag_start_vector = Vec3(1, 0, 0)
        self._drag_start_size = Vec3(1, 1, 1)
        self._drag_start_quat = Quat()
        self._drag_axis_direction = Vec3(1, 0, 0)
        self._drag_start_uniform_distance = 1.0
        self._drag_start_mouse_pixel = (0.0, 0.0)
        self._scale_debug_frame_count = 0
        self._last_result: Optional[Union[tuple[str, Vec3], dict[str, Vec3]]] = None
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
        self._drag_scale_handle = None
        self._hovered_axis = None
        self._hovered_scale_handle = None

        has_target = self._target is not None
        self.move_group.enabled = has_target and mode == "move"
        self.rotate_group.enabled = has_target and mode == "rotate"
        self.scale_group.enabled = has_target and mode == "scale"
        self._update_scale_handle_visibility()
        self._reset_colors()
        self._reset_scale_colors()

    def set_target(self, entity: Optional[Entity], allow_axis_scale: bool = True) -> None:
        if entity is self._target and allow_axis_scale == self._allow_axis_scale:
            return

        self._target = entity
        self._allow_axis_scale = allow_axis_scale
        self._dragging = False
        self._drag_axis = None
        self._drag_scale_handle = None
        self._hovered_axis = None
        self._hovered_scale_handle = None

        has_target = entity is not None
        self.root.enabled = has_target
        self.move_group.enabled = has_target and self._mode == "move"
        self.rotate_group.enabled = has_target and self._mode == "rotate"
        self.scale_group.enabled = has_target and self._mode == "scale"
        self._update_scale_handle_visibility()
        self._reset_colors()
        self._reset_scale_colors()

        if has_target:
            self.refresh_transform(camera.world_position)

    def _update_scale_handle_visibility(self) -> None:
        """Model targets only ever get the uniform handle (see Stage 2.3
        report: non-uniform Model axis scaling would introduce shear for
        arbitrarily rotated descendants and is deliberately not
        implemented) — the 6 axis handles are hidden entirely rather than
        merely inert, so there is nothing to hover/click by accident."""
        for key, handle in self._scale_handles.items():
            if key == _SCALE_UNIFORM_KEY:
                handle.enabled = True
            else:
                handle.enabled = self._allow_axis_scale

    def refresh_transform(self, camera_position: Vec3) -> None:
        if self._target is None:
            return

        origin = Vec3(self._target.position)
        self.root.position = origin

        # В Scale-режиме root поворачивается на rotation цели, чтобы
        # хендлы стояли на РЕАЛЬНЫХ локальных осях объекта (важно для
        # повёрнутых Part) — Move/Rotate специально остаются в мировых
        # осях без изменений (см. отчёт Stage 2.3), поэтому поворот root
        # применяется ТОЛЬКО когда активен Scale.
        if self._mode == "scale":
            self.root.set_quat(Quat(self._target.get_quat()))
        else:
            self.root.rotation = Vec3(0, 0, 0)

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

    def _reset_scale_colors(self) -> None:
        for key, handle in self._scale_handles.items():
            if key == _SCALE_UNIFORM_KEY:
                active = key == self._hovered_scale_handle or key == self._drag_scale_handle
                handle.color = SCALE_HANDLE_HIGHLIGHT_COLOR if active else SCALE_HANDLE_COLOR
                continue
            axis = key[0]
            active = key == self._hovered_scale_handle or key == self._drag_scale_handle
            handle.color = AXIS_HIGHLIGHT_COLOR[axis] if active else AXIS_COLOR[axis]

    def _set_hovered_scale_handle(self, key: Optional[str]) -> None:
        if key == self._hovered_scale_handle:
            return
        self._hovered_scale_handle = key
        self._reset_scale_colors()

    def update_hover(self, ray_origin: Vec3, ray_direction: Vec3) -> None:
        if self._target is None or self._dragging:
            return
        if self._mode == "move":
            test = self._hit_test_arrow
        elif self._mode == "rotate":
            test = self._hit_test_ring
        elif self._mode == "scale":
            best_key, _ = self._best_scale_handle_hit(ray_origin, ray_direction)
            self._set_hovered_scale_handle(best_key)
            return
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

    @staticmethod
    def _point_to_ray_distance(point: Vec3, ray_origin: Vec3, ray_direction: Vec3) -> float:
        """Perpendicular distance from `point` to the ray, clamped to the
        ray's forward half (t >= 0) — used for both scale-handle hit
        testing (discrete point handles, not a whole shaft/ring) and the
        uniform handle's drag-distance metric."""
        to_point = point - ray_origin
        t = max(0.0, to_point.dot(ray_direction))
        closest = ray_origin + ray_direction * t
        return (point - closest).length()

    def _best_scale_handle_hit(self, ray_origin: Vec3, ray_direction: Vec3) -> tuple[Optional[str], Optional[float]]:
        best_key = None
        best_metric = None
        tolerance = SCALE_HANDLE_HIT_RADIUS * self._current_scale
        for key, handle in self._scale_handles.items():
            if not handle.enabled:
                continue
            distance = self._point_to_ray_distance(Vec3(handle.world_position), ray_origin, ray_direction)
            if distance > tolerance:
                continue
            if best_metric is None or distance < best_metric:
                best_metric, best_key = distance, key
        return best_key, best_metric

    # --------------------------------------------------------
    # DRAG
    # --------------------------------------------------------

    def begin_drag(self, ray_origin: Vec3, ray_direction: Vec3) -> bool:
        if self._target is None or self._mode not in ("move", "rotate", "scale"):
            return False

        if self._mode == "scale":
            return self._begin_scale_drag(ray_origin, ray_direction)

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

    @staticmethod
    def _current_mouse_pixel() -> tuple[float, float]:
        """Approximate physical screen-pixel mouse position, derived from
        Ursina's normalized mouse.x/mouse.y and the window's pixel size.
        Good enough for a "few pixels" dead-zone threshold — not meant to
        be exact sub-pixel precision."""
        size = window.size
        return (float(mouse.x) * size[0], float(mouse.y) * size[1])

    def _begin_scale_drag(self, ray_origin: Vec3, ray_direction: Vec3) -> bool:
        key, _ = self._best_scale_handle_hit(ray_origin, ray_direction)
        if key is None:
            return False

        self._drag_scale_handle = key
        self._last_result = None
        self._scale_debug_frame_count = 0
        self._drag_start_position = Vec3(self._target.position)
        self._drag_start_size = Vec3(self._target.scale)
        self._drag_start_quat = Quat(self._target.get_quat())
        self._drag_start_mouse_pixel = self._current_mouse_pixel()

        if key == _SCALE_UNIFORM_KEY:
            axis, sign = None, None
            # Stage 2.3 fix: keep the TRUE (unclamped) click-to-center
            # distance separate from the floored value used only to
            # normalize the delta's sensitivity. The old code clamped
            # this single number and then divided a freshly-recomputed
            # (unclamped) current_distance by the clamped one — on a
            # precise click (true distance below
            # SCALE_UNIFORM_MIN_START_DISTANCE) that mismatch alone
            # produced factor != 1.0 on the very first zero-movement
            # frame (see _update_uniform_scale_drag). Recording the true
            # distance here and computing a delta against it instead
            # makes current==start structurally guarantee delta==0 (and
            # therefore factor==1.0), regardless of how small the true
            # distance was.
            self._drag_start_uniform_distance = self._point_to_ray_distance(
                self._drag_start_position, ray_origin, ray_direction,
            )
        else:
            # Ось хендла — единичный вектор ЛОКАЛЬНОЙ оси объекта в мировом
            # пространстве на момент старта драга (не мировой X/Y/Z), это
            # то, что делает resize корректным для повёрнутых Part.
            axis = key[0]
            sign = 1.0 if key[1] == "+" else -1.0
            local_dir = Vec3(self._drag_start_quat.xform(AXIS_DIRECTION[axis]))
            self._drag_anchor = self._drag_start_position
            self._drag_axis_direction = local_dir * sign

        if DEBUG_GIZMO:
            print(f"[GIZMO] begin_drag scale handle={key} start_size={self._drag_start_size}")

        if DEBUG_SCALE_GIZMO:
            target_id = getattr(self._target, "part_id", repr(self._target))
            print(
                f"[SCALE_GIZMO] PRESS handle_id={key!r} "
                f"handle_type={'uniform' if key == _SCALE_UNIFORM_KEY else 'axis'} "
                f"axis={axis!r} sign={sign!r} "
                f"branch={'uniform-scale' if key == _SCALE_UNIFORM_KEY else 'axis-scale'} "
                f"target_id={target_id!r} "
                f"drag_start_position={self._drag_start_position} "
                f"drag_start_size={self._drag_start_size} "
                f"mouse=({mouse.x:.6f},{mouse.y:.6f}) "
                f"mouse_pixel={self._drag_start_mouse_pixel} "
                f"ray_origin={ray_origin} ray_direction={ray_direction}"
            )

        self._dragging = True
        self._hovered_scale_handle = key
        self._reset_scale_colors()
        return True

    def update_drag(
        self,
        ray_origin: Vec3,
        ray_direction: Vec3,
        move_snap_size: float,
        snap_enabled: bool,
    ) -> Optional[Union[tuple[str, Vec3], dict[str, Vec3]]]:
        if not self._dragging or self._target is None:
            return None

        if self._mode == "scale":
            if self._drag_scale_handle is None:
                return None
            result = self._update_scale_drag(ray_origin, ray_direction, move_snap_size, snap_enabled)
            if result is not None:
                self._last_result = result
            return result

        if self._drag_axis is None:
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

    def _update_scale_drag(self, ray_origin, ray_direction, move_snap_size, snap_enabled):
        self._scale_debug_frame_count += 1
        if self._drag_scale_handle == _SCALE_UNIFORM_KEY:
            return self._update_uniform_scale_drag(ray_origin, ray_direction, move_snap_size, snap_enabled)
        return self._update_axis_scale_drag(ray_origin, ray_direction, move_snap_size, snap_enabled)

    def _update_axis_scale_drag(self, ray_origin, ray_direction, move_snap_size, snap_enabled):
        """Resizes one axis, anchoring the OPPOSITE face in place (see
        Stage 2.3 report). handle_dir is the target's real local axis
        direction at drag-start (rotated into world space), not a world
        X/Y/Z axis, so this works correctly for a rotated Part."""
        handle = self._drag_scale_handle
        axis = handle[0]
        handle_dir = self._drag_axis_direction

        result = line_line_closest(self._drag_anchor, handle_dir, ray_origin, ray_direction)
        if result is not None:
            s, t = result
            if t < 0:
                result = None
        if result is None:
            plane_normal = self._fallback_plane_normal(axis, ray_direction)
            hit = ray_plane_intersect(ray_origin, ray_direction, self._drag_anchor, plane_normal)
            if hit is None:
                return None
            s = (hit - self._drag_anchor).dot(handle_dir)
        else:
            s, _t = result

        if not math.isfinite(s):
            return None

        index = _AXIS_INDEX[axis]
        new_component = self._drag_start_size[index] + s
        if snap_enabled and move_snap_size > 0:
            new_component = round(new_component / move_snap_size) * move_snap_size
        new_component = max(new_component, MIN_PART_SIZE)
        if not math.isfinite(new_component):
            return None

        applied_delta = new_component - self._drag_start_size[index]
        new_size = Vec3(self._drag_start_size)
        new_size[index] = new_component
        new_position = self._drag_start_position + handle_dir * (applied_delta / 2.0)

        self._target.scale = new_size
        self._target.position = new_position

        if DEBUG_GIZMO:
            print(f"[GIZMO] scale handle={handle} size={new_size} position={new_position}")

        return {"Position": Vec3(new_position), "Size": Vec3(new_size)}

    def _update_uniform_scale_drag(self, ray_origin, ray_direction, move_snap_size, snap_enabled):
        """Central handle: one scalar factor derived from drag-start Size,
        never independently-snapped per axis (see Stage 2.3 report — this
        is what keeps original proportions exact). Position is untouched;
        uniform scale is anchored at the Part's own center.

        Bug-report follow-up (uniform handle "instant jump on mouse-down",
        reported still present after 4bf1331's delta-based factor fix):
        a HARD PIXEL-SPACE DEAD ZONE now gates every write in this
        function. Until the physical mouse has moved
        SCALE_UNIFORM_DEAD_ZONE_PIXELS from its press-time position, this
        returns None — no Size/Position write happens at all, not even a
        "factor==1.0, unchanged value" write. This is intentionally a
        SEPARATE, independent guard on top of the delta-based factor math
        below (kept from 4bf1331): even if that math were not the true
        cause of a residual visible jump (stale process, a second write
        path, camera movement — see the bug report's own list), gating
        the write on real screen-pixel movement makes a press-only jump
        structurally impossible here, regardless of cause.

        The world-space delta math (factor = 1.0 + delta / sensitivity)
        is UNCHANGED and still computed from the immutable drag-start
        baseline captured once in _begin_scale_drag — the dead-zone check
        does not feed back into it, so crossing the threshold applies
        whatever (small, continuous) factor that immutable baseline
        already implies at that pointer position, not a fresh jump."""
        frame_no = self._scale_debug_frame_count
        verbose = DEBUG_SCALE_GIZMO and frame_no <= 5

        current_mouse_pixel = self._current_mouse_pixel()
        pixel_dx = current_mouse_pixel[0] - self._drag_start_mouse_pixel[0]
        pixel_dy = current_mouse_pixel[1] - self._drag_start_mouse_pixel[1]
        pixel_distance = math.hypot(pixel_dx, pixel_dy)

        if verbose:
            print(
                f"[SCALE_GIZMO] frame={frame_no} update_fn=_update_uniform_scale_drag "
                f"active_handle={self._drag_scale_handle!r} "
                f"mouse=({mouse.x:.6f},{mouse.y:.6f}) mouse_pixel={current_mouse_pixel} "
                f"press_mouse_pixel={self._drag_start_mouse_pixel} "
                f"pixel_distance_from_press={pixel_distance:.3f}px "
                f"snap_enabled={snap_enabled} move_snap_size={move_snap_size}"
            )

        if pixel_distance < SCALE_UNIFORM_DEAD_ZONE_PIXELS:
            if verbose:
                print(
                    f"[SCALE_GIZMO] frame={frame_no} DEAD ZONE "
                    f"(pixel_distance={pixel_distance:.3f}px < "
                    f"{SCALE_UNIFORM_DEAD_ZONE_PIXELS}px) -> NO WRITE"
                )
            return None

        size_before = Vec3(self._target.scale)
        position_before = Vec3(self._target.position)

        current_distance = self._point_to_ray_distance(self._drag_start_position, ray_origin, ray_direction)
        delta = current_distance - self._drag_start_uniform_distance
        sensitivity_reference = max(self._drag_start_uniform_distance, SCALE_UNIFORM_MIN_START_DISTANCE)
        factor = 1.0 + delta / sensitivity_reference
        if verbose:
            print(
                f"[SCALE_GIZMO] frame={frame_no} raw_metric(current_distance)={current_distance:.6f} "
                f"drag_start_metric={self._drag_start_uniform_distance:.6f} "
                f"delta={delta:.6f} raw_factor={factor:.6f}"
            )
        if not math.isfinite(factor) or factor <= 0:
            return None

        start = self._drag_start_size

        # No perceptible WORLD-SPACE movement despite crossing the pixel
        # dead zone (rare, but geometrically possible depending on camera
        # angle): Size must stay EXACTLY the drag-start Size. With snap
        # enabled, re-snapping the absolute reference dimension to the
        # grid below could otherwise manufacture a tiny but nonzero jump
        # purely because drag-start Size wasn't already grid-aligned.
        if abs(delta) < 1e-9:
            new_size = Vec3(start)
            self._target.scale = new_size
            if DEBUG_GIZMO:
                print(f"[GIZMO] scale uniform factor=1.0000 size={new_size}")
            if verbose:
                print(
                    f"[SCALE_GIZMO] frame={frame_no} snapped_factor=1.000000 "
                    f"size_before={size_before} size_after={new_size} "
                    f"position_before={position_before} position_after={Vec3(self._target.position)}"
                )
            return {"Size": Vec3(new_size)}

        reference = max(start.x, start.y, start.z)
        if reference <= 1e-9:
            return None
        scaled_reference = reference * factor
        if snap_enabled and move_snap_size > 0:
            scaled_reference = round(scaled_reference / move_snap_size) * move_snap_size
        scaled_reference = max(scaled_reference, MIN_PART_SIZE)
        effective_factor = scaled_reference / reference

        smallest = min(start.x, start.y, start.z)
        if smallest > 1e-9:
            effective_factor = max(effective_factor, MIN_PART_SIZE / smallest)
        if not math.isfinite(effective_factor) or effective_factor <= 0:
            return None

        new_size = Vec3(start.x * effective_factor, start.y * effective_factor, start.z * effective_factor)
        self._target.scale = new_size

        if DEBUG_GIZMO:
            print(f"[GIZMO] scale uniform factor={effective_factor:.4f} size={new_size}")
        if verbose:
            print(
                f"[SCALE_GIZMO] frame={frame_no} snapped_factor={effective_factor:.6f} "
                f"size_before={size_before} size_after={new_size} "
                f"position_before={position_before} position_after={Vec3(self._target.position)}"
            )

        return {"Size": Vec3(new_size)}

    def end_drag(self) -> Optional[Union[tuple[str, Vec3], dict[str, Vec3]]]:
        if not self._dragging:
            return None

        result = self._last_result
        axis = self._drag_axis
        scale_handle = self._drag_scale_handle

        self._dragging = False
        self._drag_axis = None
        self._drag_scale_handle = None
        self._last_result = None
        self._hovered_axis = None
        self._hovered_scale_handle = None
        self._reset_colors()
        self._reset_scale_colors()

        if DEBUG_GIZMO and result is not None:
            print(f"[GIZMO] end_drag axis={axis} scale_handle={scale_handle} final={result}")

        return result
