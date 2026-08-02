"""
Малый, самодостаточный модуль кватернионной математики без зависимости от
Panda3D/Ursina — используется ТОЛЬКО non-live/demo-режимом
(studio_editor_live.py), у которого нет живого 3D-движка и который не
должен импортировать ursina (это создало бы побочные эффекты окна).

Живой клиент (client_studio.py) НЕ использует этот модуль — там кватернионы
берутся напрямую из настоящих Panda3D Entity/NodePath (get_quat/set_quat),
чтобы гарантированно совпадать с тем, как Ursina реально рисует rotation.
Здесь же конвенция XYZ (roll-pitch-yaw, intrinsic Z*Y*X) выбрана
самостоятельно и обязана быть только внутренне согласованной: demo-режим
ничего не рендерит в 3D, Rotation используется лишь как числа в Inspector.

Это НЕ второй "источник истины" — оба режима (live/demo) никогда не делят
одну и ту же сцену; см. отчёт Stage 2.2 за полным обоснованием.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Quat:
    w: float
    x: float
    y: float
    z: float


IDENTITY = Quat(1.0, 0.0, 0.0, 0.0)


def from_euler_xyz_degrees(rotation: tuple[float, float, float]) -> Quat:
    """Строит unit-кватернион из (roll=x, pitch=y, yaw=z) в градусах,
    intrinsic-порядок Z*Y*X (стандартная yaw-pitch-roll конвенция)."""
    roll, pitch, yaw = (math.radians(v) for v in rotation)

    cr, sr = math.cos(roll * 0.5), math.sin(roll * 0.5)
    cp, sp = math.cos(pitch * 0.5), math.sin(pitch * 0.5)
    cy, sy = math.cos(yaw * 0.5), math.sin(yaw * 0.5)

    w = cr * cp * cy + sr * sp * sy
    x = sr * cp * cy - cr * sp * sy
    y = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    return Quat(w, x, y, z)


def to_euler_xyz_degrees(q: Quat) -> tuple[float, float, float]:
    """Обратное преобразование — инверсия from_euler_xyz_degrees. При
    pitch около ±90° (gimbal lock) roll/yaw неоднозначны; используем
    устойчивую atan2-формулу, стандартную для этого случая."""
    # roll (x-axis rotation)
    sinr_cosp = 2.0 * (q.w * q.x + q.y * q.z)
    cosr_cosp = 1.0 - 2.0 * (q.x * q.x + q.y * q.y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # pitch (y-axis rotation)
    sinp = 2.0 * (q.w * q.y - q.z * q.x)
    if abs(sinp) >= 1.0:
        pitch = math.copysign(math.pi / 2.0, sinp)
    else:
        pitch = math.asin(sinp)

    # yaw (z-axis rotation)
    siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
    cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


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
    # t = 2 * cross(q.xyz, v)
    tx = 2.0 * (q.y * vz - q.z * vy)
    ty = 2.0 * (q.z * vx - q.x * vz)
    tz = 2.0 * (q.x * vy - q.y * vx)
    # v' = v + q.w * t + cross(q.xyz, t)
    rx = vx + q.w * tx + (q.y * tz - q.z * ty)
    ry = vy + q.w * ty + (q.z * tx - q.x * tz)
    rz = vz + q.w * tz + (q.x * ty - q.y * tx)
    return (rx, ry, rz)


def compose(outer: Quat, inner: Quat) -> Quat:
    """Возвращает кватернион 'сначала inner, потом outer' -- т.е.
    compose(outer, inner).rotate(v) == outer.rotate(inner.rotate(v))."""
    return multiply(outer, inner)
