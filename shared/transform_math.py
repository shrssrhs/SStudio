"""
Малый, самодостаточный модуль кватернионной математики без зависимости от
Panda3D/Ursina — используется ТОЛЬКО non-live/demo-режимом
(studio_editor_live.py), у которого нет живого 3D-движка и который не
должен импортировать ursina (это создало бы побочные эффекты окна).

Stage 2.2 hardening: этот модуль обязан давать РЕЗУЛЬТАТ, БИТ-В-БИТ
СОВПАДАЮЩИЙ (в пределах TOLERANCE_DEGREES/см. test_transform_math_parity.py)
с тем, что реально делает Ursina/Panda3D в живом клиенте — "внутренне
согласованной" конвенции недостаточно, потому что demo-режим умеет
сохранять сцену, а сохранённые Model pivot/Rotation значения должны
одинаково выглядеть при открытии в live-клиенте. Формула ниже была
получена ЭМПИРИЧЕСКИ (не из документации) путём прямого сравнения с
Entity.get_quat() внутри реального Ursina()-приложения:

  1. Ursina().rotation_setter кодирует Vec3(x, y, z) в Panda HPR как
     setHpr(Vec3(-y, -x, z)) (см. Entity.rotation_directions=(-1,-1,1) в
     ursina/entity.py) — это чисто Python-код, не зависит от coordinate-
     system и было тривиально прочитано из исходников.
  2. Однако САМА интерпретация HPR внутри Panda3D зависит от глобального
     coordinate-system, который Ursina() перенастраивает при старте на
     "y-up-left" (см. panda3d.core.ConfigVariableString('coordinate-
     system') после Ursina() — ДО Ursina() Panda по умолчанию на
     "z-up-right", что даёт СОВЕРШЕННО ДРУГУЮ формулу). Поэтому её нельзя
     было взять из общей документации Panda — она эмпирически измерена
     заново прямо в контексте живого приложения.
  3. Комбинируя оба шага, итоговая формула для мирового кватерниона по
     Ursina-совместимому (x, y, z) в градусах:

         q(x, y, z) = Ry(y) · Rx(x) · Rz(-z)

     где Rx/Ry/Rz — стандартные (правосторонние, положительный угол =
     против часовой стрелки при взгляде с положительного конца оси)
     кватернионы поворота вокруг соответствующей оси, а "·" — обычное
     произведение Гамильтона (mult(a, b) означает "сначала b, потом a").

     Проверено на 9+ комбинациях (одиночные оси, X37, Z-45, суммы трёх
     осей, отрицательные углы) через |dot(q_mine, q_ursina)| ≈ 1.0 —
     см. test_transform_math_parity.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Максимальное допустимое расхождение при сравнении с живым Panda3D —
# см. test_transform_math_parity.py. |dot(q1, q2)| >= 1 - TOLERANCE
# считается совпадением (dot=1 у идентичных поворотов, вплоть до общего
# знака кватерниона, который несущественен: q и -q — один и тот же
# поворот).
PARITY_TOLERANCE = 1e-4


@dataclass(frozen=True)
class Quat:
    w: float
    x: float
    y: float
    z: float


IDENTITY = Quat(1.0, 0.0, 0.0, 0.0)


def _axis_quat(axis: tuple[float, float, float], degrees: float) -> Quat:
    rad = math.radians(degrees)
    s = math.sin(rad * 0.5)
    c = math.cos(rad * 0.5)
    ax, ay, az = axis
    return Quat(c, s * ax, s * ay, s * az)


def from_euler_xyz_degrees(rotation: tuple[float, float, float]) -> Quat:
    """Строит unit-кватернион из Ursina-совместимого (x, y, z) в градусах.
    См. модульный docstring — формула эмпирически проверена против
    Entity.get_quat() внутри реального Ursina()-приложения, это НЕ
    самостоятельно выбранная конвенция."""
    x, y, z = rotation
    qy = _axis_quat((0.0, 1.0, 0.0), y)
    qx = _axis_quat((1.0, 0.0, 0.0), x)
    qz = _axis_quat((0.0, 0.0, 1.0), -z)
    return multiply(multiply(qy, qx), qz)


def _quat_to_matrix(q: Quat) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    w, x, y, z = q.w, q.x, q.y, q.z
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
        (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
        (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
    )


def to_euler_xyz_degrees(q: Quat) -> tuple[float, float, float]:
    """Обратное преобразование — инверсия from_euler_xyz_degrees, через
    разложение матрицы поворота в порядке Y*X*Z (см. модульный docstring).
    При x около ±90° (gimbal lock) y/z неоднозначны по отдельности —
    фиксируем z=0 и переносим весь поворот в y, что даёт корректный
    результирующий поворот (совпадающую матрицу), даже если конкретные
    (y, z) отличаются от того, что задал бы живой клиент в этой точке."""
    m = _quat_to_matrix(q)
    m00, m01, m02 = m[0]
    m10, m11, m12 = m[1]
    m20, m21, m22 = m[2]

    sin_x = max(-1.0, min(1.0, -m12))
    x = math.asin(sin_x)

    if abs(m12) < 0.9999:
        y = math.atan2(m02, m22)
        c = math.atan2(m10, m11)
        z = -c
    else:
        # Gimbal lock: cos(x) ~= 0. Фиксируем z=0 (c=0) и переносим весь
        # поворот вокруг совпавших Y/Z осей в y; sin_x уже несёт знак.
        y = math.atan2(m01 * (1.0 if sin_x >= 0 else -1.0), m00)
        z = 0.0

    return (math.degrees(x), math.degrees(y), math.degrees(z))


def multiply(a: Quat, b: Quat) -> Quat:
    """Стандартное произведение Гамильтона a*b: multiply(a, b).rotate(v) ==
    a.rotate(b.rotate(v)) -- т.е. b применяется первым, затем a."""
    return Quat(
        w=a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
        x=a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
        y=a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
        z=a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w,
    )


def conjugate(q: Quat) -> Quat:
    """Обратный кватернион для unit-кватерниона (инверсия вращения)."""
    return Quat(q.w, -q.x, -q.y, -q.z)


def rotate_vector(q: Quat, v: tuple[float, float, float]) -> tuple[float, float, float]:
    """Вращает вектор v кватернионом q (v' = q * v * q^-1, через формулу
    без промежуточного кватерниона-вектора)."""
    vx, vy, vz = v
    tx = 2.0 * (q.y * vz - q.z * vy)
    ty = 2.0 * (q.z * vx - q.x * vz)
    tz = 2.0 * (q.x * vy - q.y * vx)
    rx = vx + q.w * tx + (q.y * tz - q.z * ty)
    ry = vy + q.w * ty + (q.z * tx - q.x * tz)
    rz = vz + q.w * tz + (q.x * ty - q.y * tx)
    return (rx, ry, rz)


def compose(outer: Quat, inner: Quat) -> Quat:
    """Возвращает кватернион 'сначала inner, потом outer' -- т.е.
    compose(outer, inner).rotate(v) == outer.rotate(inner.rotate(v))."""
    return multiply(outer, inner)


def quat_dot_abs(a: Quat, b: Quat) -> float:
    """|dot(a, b)| — 1.0 означает тот же поворот (с точностью до общего
    знака кватерниона, который не имеет физического смысла: q и -q
    описывают одинаковое вращение). Используется в parity-тестах против
    живого Panda3D и в самопроверке круговых преобразований."""
    return abs(a.w * b.w + a.x * b.x + a.y * b.y + a.z * b.z)
