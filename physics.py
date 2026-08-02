"""
Stage 2.4: минимальный Play Mode runtime physics для Anchored/CanCollide.

Архитектура (см. Stage 2.4 report за полным обоснованием):

- Физика существует ТОЛЬКО во время Play — создаётся в PhysicsWorld.start(),
  полностью уничтожается в PhysicsWorld.stop(). Ничего не переживает Stop.
- Три категории тела на каждый Part-подобный Entity, в зависимости от
  Anchored/CanCollide на момент входа в Play:
    Anchored=True                -> STATIC Bullet-тело (mass=0), если
                                     CanCollide=True; иначе вообще без тела
                                     (не двигается, нечему сталкиваться).
    Anchored=False, CanCollide=True  -> DYNAMIC Bullet rigid body: настоящая
                                     гравитация + столкновения через Bullet.
    Anchored=False, CanCollide=False -> "ghost": НЕ добавляется в BulletWorld
                                     вообще (ни как static, ни как dynamic) —
                                     падает по простой ручной интеграции
                                     (v.y += gravity*dt; y += v.y*dt), поэтому
                                     структурно не может столкнуться ни с чем,
                                     а не просто "выключена коллизия" через
                                     Bullet-маски.
- Редакторский pick (mouse.hovered_entity, см. client_studio.py) остаётся
  ПОЛНОСТЬЮ независим от этого модуля — Entity.collider управляется отдельно
  в client_studio.py и не связан с наличием/отсутствием физического тела
  здесь. См. "Do not confuse editor picking with gameplay collision" в
  задании Stage 2.4.
- Sync — только локальный: World -> Entity.position/rotation каждый кадр
  Play, НИКОГДА обратно в record.properties и НИКОГДА в сеть (см. §8
  задания — физика в этой стадии умышленно local-only, не реплицируется).
"""

from __future__ import annotations

from typing import Any, Optional

from panda3d.bullet import BulletBoxShape, BulletRigidBodyNode, BulletWorld
from panda3d.core import NodePath, Quat
from panda3d.core import Vec3 as PVec3
from ursina import Entity, Vec3

from shared.instance import MIN_PART_SIZE

# Логирует: инициализацию бэкенда, создание/удаление тел (id, Anchored,
# CanCollide, тип тела, Position/Rotation/Size), число тел на старте/стопе
# Play, результаты restore. НЕ печатает ничего каждый физический кадр, если
# явно не включено ниже отдельным принтом (см. PhysicsWorld.step). По
# умолчанию False — временный диагностический флаг.
DEBUG_PHYSICS = False

# Подобрано под текущий масштаб мира (обычный Part по умолчанию — куб со
# стороной ~4 юнита) так, чтобы падение с разумной редакторской высоты
# занимало доли секунды, а не выглядело как невесомость и не "телепортировало"
# тело за один substep.
GRAVITY_Y = -24.0
MAX_SUBSTEPS = 10
FIXED_SUBSTEP = 1.0 / 120.0

# Постоянная линейная/угловая демпфирование — стандартная практика для
# устойчивого "оседания" Bullet-тел без дрожания, не имитация трения (то
# даёт сам Bullet по умолчанию) и не restitution/bounce (умышленно не
# трогаем, см. §6 задания — bounce опционален и не требуется).
LINEAR_DAMPING = 0.1
ANGULAR_DAMPING = 0.4

_MIN_HALF_EXTENT = MIN_PART_SIZE / 2.0


class _RotationScratch:
    """Тот же приём, что и MultiplayerGame._euler_xyz_to_quat/_quat_to_euler_xyz
    (Stage 2.2): конвертация Euler XYZ <-> quaternion ВСЕГДА через реальный
    Panda3D NodePath, а не через самодельную формулу — гарантирует то же
    сочетание осей/знаков, что и остальной рендер (см. Stage 2.2 report).
    Отдельный от MultiplayerGame экземпляр, чтобы physics.py не зависел от
    client_studio.py (модуль используется ИЗ client_studio.py, не наоборот)."""

    def __init__(self) -> None:
        # Same construction as MultiplayerGame._transform_scratch: no
        # `model=` kwarg, so nothing is ever actually rendered.
        self._scratch = Entity(eternal=True)

    def euler_to_quat(self, rotation_xyz: Any) -> Quat:
        self._scratch.rotation = Vec3(
            float(rotation_xyz[0]), float(rotation_xyz[1]), float(rotation_xyz[2]),
        )
        return Quat(self._scratch.get_quat())

    def quat_to_euler(self, quat: Quat) -> list[float]:
        self._scratch.set_quat(quat)
        r = self._scratch.rotation
        return [float(r.x), float(r.y), float(r.z)]


class _PhysicsBody:
    __slots__ = ("instance_id", "entity", "kind", "node", "np", "velocity_y")

    def __init__(self, instance_id: str, entity: Entity, kind: str, node: Optional[BulletRigidBodyNode], np: Optional[NodePath]) -> None:
        self.instance_id = instance_id
        self.entity = entity
        # "static" | "dynamic" | "ghost" — see module docstring.
        self.kind = kind
        self.node = node
        self.np = np
        self.velocity_y = 0.0


class PhysicsWorld:
    """One instance lives for exactly one Play session (see
    MultiplayerGame.set_studio_playing). Never reused across sessions —
    a fresh PhysicsWorld is created on every Play, discarded on every
    Stop, so repeated Play/Stop cannot leak bodies (see Stage 2.4 report,
    §12 test)."""

    def __init__(self) -> None:
        self.bullet_world = BulletWorld()
        self.bullet_world.setGravity(PVec3(0, GRAVITY_Y, 0))
        self._rotation = _RotationScratch()
        self._bodies: dict[str, _PhysicsBody] = {}
        if DEBUG_PHYSICS:
            print(f"[PHYSICS] backend initialized: Bullet, gravity=(0,{GRAVITY_Y},0)")

    def add_part(
        self,
        instance_id: str,
        entity: Entity,
        position: list[float],
        rotation: list[float],
        size: list[float],
        anchored: bool,
        can_collide: bool,
    ) -> None:
        half_extent = PVec3(
            max(float(size[0]) / 2.0, _MIN_HALF_EXTENT),
            max(float(size[1]) / 2.0, _MIN_HALF_EXTENT),
            max(float(size[2]) / 2.0, _MIN_HALF_EXTENT),
        )
        pos = PVec3(float(position[0]), float(position[1]), float(position[2]))
        quat = self._rotation.euler_to_quat(rotation)

        if anchored:
            if not can_collide:
                # Anchored + non-collidable: never moves (Anchored) and
                # nothing can collide with it (CanCollide=False) -- no
                # physics presence needed at all.
                body = _PhysicsBody(instance_id, entity, "none", None, None)
                self._bodies[instance_id] = body
                if DEBUG_PHYSICS:
                    print(
                        f"[PHYSICS] body id={instance_id} kind=none anchored=True "
                        f"can_collide=False pos={position} rot={rotation} size={size}"
                    )
                return

            node = BulletRigidBodyNode(f"static:{instance_id}")
            node.addShape(BulletBoxShape(half_extent))
            node.setMass(0.0)
            node.setStatic(True)
            np = NodePath(node)
            np.setPos(pos)
            np.setQuat(quat)
            self.bullet_world.attachRigidBody(node)
            body = _PhysicsBody(instance_id, entity, "static", node, np)
            self._bodies[instance_id] = body
            if DEBUG_PHYSICS:
                print(
                    f"[PHYSICS] body id={instance_id} kind=static anchored=True "
                    f"can_collide=True pos={position} rot={rotation} size={size}"
                )
            return

        if not can_collide:
            # Ghost: deliberately NOT attached to the Bullet world at all
            # (see module docstring) -- falls via manual integration,
            # cannot collide with anything by construction.
            body = _PhysicsBody(instance_id, entity, "ghost", None, None)
            self._bodies[instance_id] = body
            if DEBUG_PHYSICS:
                print(
                    f"[PHYSICS] body id={instance_id} kind=ghost anchored=False "
                    f"can_collide=False pos={position} rot={rotation} size={size}"
                )
            return

        volume = max(float(size[0]) * float(size[1]) * float(size[2]), 0.1)
        node = BulletRigidBodyNode(f"dynamic:{instance_id}")
        node.addShape(BulletBoxShape(half_extent))
        node.setMass(volume)
        node.setLinearDamping(LINEAR_DAMPING)
        node.setAngularDamping(ANGULAR_DAMPING)
        node.setDeactivationEnabled(True)
        np = NodePath(node)
        np.setPos(pos)
        np.setQuat(quat)
        self.bullet_world.attachRigidBody(node)
        body = _PhysicsBody(instance_id, entity, "dynamic", node, np)
        self._bodies[instance_id] = body
        if DEBUG_PHYSICS:
            print(
                f"[PHYSICS] body id={instance_id} kind=dynamic anchored=False "
                f"can_collide=True mass={volume:.3f} pos={position} rot={rotation} size={size}"
            )

    def body_count(self) -> int:
        return len(self._bodies)

    def step(self, dt: float) -> None:
        if dt <= 0:
            return
        self.bullet_world.doPhysics(dt, MAX_SUBSTEPS, FIXED_SUBSTEP)

        for body in self._bodies.values():
            if body.kind == "dynamic":
                pos = body.np.getPos()
                quat = body.np.getQuat()
                body.entity.position = Vec3(pos.x, pos.y, pos.z)
                body.entity.rotation = Vec3(*self._rotation.quat_to_euler(quat))
            elif body.kind == "ghost":
                body.velocity_y += GRAVITY_Y * dt
                body.entity.y += body.velocity_y * dt
            # "static"/"none": never moves during Play by definition.

    def destroy(self) -> None:
        removed = len(self._bodies)
        for body in self._bodies.values():
            if body.node is not None:
                self.bullet_world.removeRigidBody(body.node)
        self._bodies.clear()
        if DEBUG_PHYSICS:
            print(f"[PHYSICS] destroyed: {removed} bodies removed, world torn down")
