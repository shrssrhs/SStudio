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

from panda3d.bullet import BulletBoxShape, BulletGhostNode, BulletRigidBodyNode, BulletWorld
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

    def __init__(self, gravity: float = GRAVITY_Y) -> None:
        """Stage 3.8: `gravity` is the SIGNED Y-axis value (negative =
        downward, matching the module-level GRAVITY_Y default) -- callers
        pass -(Workspace.Gravity magnitude) here, never the raw positive
        magnitude the Inspector/Lua property exposes. Stored as instance
        state (not just applied once) so set_gravity() below can update it
        for the rest of this Play session -- see workspace.Gravity's
        runtime-write path in lua_runtime.py."""
        self.bullet_world = BulletWorld()
        self._gravity_y = float(gravity)
        self.bullet_world.setGravity(PVec3(0, self._gravity_y, 0))
        self._rotation = _RotationScratch()
        self._bodies: dict[str, _PhysicsBody] = {}
        self._frame_count = 0
        # Stage 3.9 (Touched support): reverse lookup from a Bullet node
        # (BulletRigidBodyNode for static/dynamic, BulletGhostNode for
        # ghost/none -- see add_part() below) back to the instance_id it
        # belongs to, PLUS any node registered via register_external_node()
        # (the player character's BulletCharacterControllerNode -- owned by
        # character_controller.py, not by a _PhysicsBody here, since its
        # own lifecycle is managed entirely by client_studio.py). Node
        # objects hash/compare by their underlying C++ pointer (confirmed
        # empirically), so a node handed back later by getManifolds()/
        # getOverlappingNodes() -- a different Python wrapper instance --
        # still resolves correctly against this dict.
        self._node_to_id: dict[Any, str] = {}
        # Frame-to-frame diff set for poll_new_contacts() -- holds pairs
        # that were ALREADY touching last poll, so only NEWLY-begun contact
        # is reported (avoids uncontrolled per-frame Touched spam for
        # sustained contact, per the Stage 3.9 spec).
        self._touching_pairs: set[frozenset[str]] = set()
        if DEBUG_PHYSICS:
            print(f"[PHYSICS] backend initialized: Bullet, gravity=(0,{self._gravity_y},0)")

    @property
    def gravity(self) -> float:
        """Current SIGNED Y-axis gravity (negative = downward) -- may
        differ from what PhysicsWorld() was constructed with if a runtime
        Lua write already called set_gravity() this session."""
        return self._gravity_y

    def set_gravity(self, gravity: float) -> None:
        """Runtime Lua `workspace.Gravity = X` write -- takes effect
        immediately for both Bullet's own dynamic bodies and the manual
        ghost-body integration in step() below, which does NOT go through
        Bullet's gravity at all (see class docstring's "ghost" category).

        BulletWorld.setGravity() alone is NOT enough for a body that has
        been sitting motionless long enough for Bullet's deactivation
        timer to put it to sleep (setDeactivationEnabled(True) in
        add_part() -- confirmed empirically: after ~2s of zero gravity a
        dynamic body's isActive() goes False, and neither
        world.setGravity() nor even the body's own setGravity() wakes it
        again on their own; it stays frozen at its old position forever,
        even though the world's gravity constant genuinely changed. Every
        existing dynamic body must be explicitly re-set AND force-woken
        (same setActive(True, True) call wake_all_dynamic() already uses
        for the "support was deleted" case), or a runtime workspace.
        Gravity change silently has no effect on any Part that had
        already gone to sleep in this Play session."""
        self._gravity_y = float(gravity)
        gravity_vec = PVec3(0, self._gravity_y, 0)
        self.bullet_world.setGravity(gravity_vec)
        for body in self._bodies.values():
            if body.kind == "dynamic" and body.node is not None:
                body.node.setGravity(gravity_vec)
                body.node.setActive(True, True)

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
                # produces no COLLISION RESPONSE (CanCollide=False) -- but
                # Stage 3.9's Touched needs overlap detection to still work
                # for exactly this kind (a fixed, walk-through trigger
                # volume: checkpoint/goal-zone/button), so it still gets a
                # real BulletGhostNode (never a BulletRigidBodyNode --
                # ghost objects report overlaps without ever pushing
                # anything, confirmed empirically against both a dynamic
                # rigid body and BulletCharacterControllerNode).
                node = BulletGhostNode(f"none:{instance_id}")
                node.addShape(BulletBoxShape(half_extent))
                np = NodePath(node)
                np.setPos(pos)
                np.setQuat(quat)
                self.bullet_world.attachGhost(node)
                body = _PhysicsBody(instance_id, entity, "none", node, np)
                self._bodies[instance_id] = body
                self._node_to_id[node] = instance_id
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
            self._node_to_id[node] = instance_id
            if DEBUG_PHYSICS:
                print(
                    f"[PHYSICS] body id={instance_id} kind=static anchored=True "
                    f"can_collide=True pos={position} rot={rotation} size={size}"
                )
            return

        if not can_collide:
            # Ghost: no collision RESPONSE (falls via manual integration,
            # see module docstring -- unaffected by Bullet's own dynamics),
            # but still gets a BulletGhostNode purely for Touched overlap
            # detection, kept in sync with the manually-integrated position
            # every step() (see below).
            node = BulletGhostNode(f"ghost:{instance_id}")
            node.addShape(BulletBoxShape(half_extent))
            np = NodePath(node)
            np.setPos(pos)
            np.setQuat(quat)
            self.bullet_world.attachGhost(node)
            body = _PhysicsBody(instance_id, entity, "ghost", node, np)
            self._bodies[instance_id] = body
            self._node_to_id[node] = instance_id
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
        self._node_to_id[node] = instance_id
        if DEBUG_PHYSICS:
            print(
                f"[PHYSICS] body id={instance_id} kind=dynamic anchored=False "
                f"can_collide=True mass={volume:.3f} pos={position} rot={rotation} size={size}"
            )

    def register_external_node(self, instance_id: str, node: Any) -> None:
        """Lets a node this PhysicsWorld did not create (currently: the
        player character's BulletCharacterControllerNode, already attached
        to this same bullet_world by character_controller.py -- see its
        "do not introduce a second Bullet world" constraint) participate in
        poll_new_contacts()'s reverse lookup, WITHOUT creating a
        _PhysicsBody for it (its position/lifecycle stays entirely owned by
        its real owner). `instance_id` need not be a real RuntimeSceneLayer
        instance id -- the caller (LuaRuntimeManager._fire_touched) is
        expected to special-case whatever sentinel string is used here."""
        self._node_to_id[node] = instance_id

    def unregister_external_node(self, node: Any) -> None:
        self._node_to_id.pop(node, None)

    def body_count(self) -> int:
        return len(self._bodies)

    def has_body(self, instance_id: str) -> bool:
        return instance_id in self._bodies

    def remove_part(self, instance_id: str) -> Optional[str]:
        """Bug-report follow-up: an authoritatively-deleted Instance during
        Play used to leave its Bullet body (or ghost/static bookkeeping)
        behind forever — the visible Entity was destroyed, but nothing
        here ever heard about it, so a static floor's body kept
        supporting whatever rested on it even after the floor itself was
        gone. Called from MultiplayerGame.remove_instance() for every
        authoritatively-deleted instance, not just ones the local player
        deleted (see module docstring — same call site the server's
        PART_DELETED broadcast already drives for the Entity/instances
        cleanup, so this piggybacks on the one confirmed-delete path
        rather than adding a second one).

        Returns the removed body's kind ("static"/"dynamic"/"ghost"/
        "none"), or None if this instance had no runtime body at all
        (e.g. it wasn't part of this Play session, or physics already
        tore down) -- the caller uses "static" to decide whether to wake
        remaining dynamic bodies."""
        body = self._bodies.pop(instance_id, None)
        if body is None:
            if DEBUG_PHYSICS:
                print(f"[PHYSICS] remove_part id={instance_id}: no runtime body (nothing to remove)")
            return None

        before = len(self._bodies) + 1
        if body.node is not None:
            if body.kind in ("static", "dynamic"):
                self.bullet_world.removeRigidBody(body.node)
            elif body.kind in ("ghost", "none"):
                self.bullet_world.removeGhost(body.node)
            self._node_to_id.pop(body.node, None)
        after = len(self._bodies)
        if DEBUG_PHYSICS:
            print(
                f"[PHYSICS] remove_part id={instance_id} kind={body.kind} "
                f"body_count_before={before} body_count_after={after}"
            )
        return body.kind

    def wake_all_dynamic(self) -> None:
        """Force every remaining dynamic body active. Called after a
        static (or ghost/none -- harmless either way) body is removed:
        a dynamic body resting on a now-gone support is very likely
        asleep (Bullet deactivates bodies that haven't moved for a
        while, see setDeactivationEnabled in add_part), and a sleeping
        body does NOT re-evaluate gravity/contacts on its own even
        though its support just vanished from the world. Simpler and
        more robust than targeted contact-based waking for the small
        object counts this stage targets (see Stage 2.4 report scope
        note) -- correctness over precision."""
        woken = 0
        for body in self._bodies.values():
            if body.kind == "dynamic" and body.node is not None:
                body.node.setActive(True, True)
                woken += 1
        if DEBUG_PHYSICS:
            print(f"[PHYSICS] wake_all_dynamic: {woken} dynamic bodies forced active")

    def step(self, dt: float) -> None:
        self._frame_count += 1
        frame_no = self._frame_count
        verbose = DEBUG_PHYSICS and frame_no <= 120

        if dt <= 0:
            if verbose:
                print(f"[PHYSICS] frame={frame_no} dt={dt} -> SKIPPED (dt<=0, world not stepped)")
            return

        self.bullet_world.doPhysics(dt, MAX_SUBSTEPS, FIXED_SUBSTEP)

        for body in self._bodies.values():
            if body.kind == "dynamic":
                pos = body.np.getPos()
                quat = body.np.getQuat()
                entity_pos_before = Vec3(body.entity.position)
                body.entity.position = Vec3(pos.x, pos.y, pos.z)
                body.entity.rotation = Vec3(*self._rotation.quat_to_euler(quat))
                if verbose:
                    print(
                        f"[PHYSICS] frame={frame_no} dt={dt:.5f} id={body.instance_id} "
                        f"kind=dynamic body_pos=({pos.x:.4f},{pos.y:.4f},{pos.z:.4f}) "
                        f"entity_pos_before={entity_pos_before} "
                        f"entity_pos_after={body.entity.position} "
                        f"active={body.node.isActive()} copied_to_entity=True"
                    )
            elif body.kind == "ghost":
                body.velocity_y += self._gravity_y * dt
                body.entity.y += body.velocity_y * dt
                if body.np is not None:
                    # Keeps the BulletGhostNode's overlap-test position in
                    # sync with this kind's own manual gravity integration
                    # (Touched support) -- one frame of lag vs. this same
                    # frame's doPhysics() call above is fine (see Stage 3.9
                    # report), same as every other overlap/manifold result
                    # already reflects last frame's transforms.
                    ep = body.entity.position
                    body.np.setPos(PVec3(ep.x, ep.y, ep.z))
                if verbose:
                    print(
                        f"[PHYSICS] frame={frame_no} dt={dt:.5f} id={body.instance_id} "
                        f"kind=ghost velocity_y={body.velocity_y:.4f} entity_pos_after={body.entity.position}"
                    )
            elif verbose and frame_no <= 3:
                # static/none: log only the first few frames to confirm
                # they're deliberately skipped, not silently forgotten.
                print(f"[PHYSICS] frame={frame_no} id={body.instance_id} kind={body.kind} -> not simulated (by design)")

        if verbose:
            print(f"[PHYSICS] frame={frame_no} world stepped=True accumulated_steps={frame_no} bodies={len(self._bodies)}")

    def poll_new_contacts(self) -> list[tuple[str, str]]:
        """Stage 3.9 Touched support. Returns only pairs whose contact
        began SINCE THE LAST CALL (frame-to-frame diff against
        self._touching_pairs) -- sustained contact is deliberately not
        re-reported every frame, per the spec's "avoid uncontrolled
        per-frame callback spam" requirement. Two independent detection
        paths, because they cover genuinely different body kinds:

        1. Real Bullet contact manifolds (bullet_world.getManifolds()) --
           covers CanCollide=True static/dynamic bodies AND the player
           character (BulletCharacterControllerNode DOES generate real
           manifold points against static/dynamic geometry it rests
           against or bumps into -- confirmed empirically, not assumed).
        2. BulletGhostNode.getOverlappingNodes() for each "ghost"/"none"
           (CanCollide=False) body -- confirmed empirically to also detect
           overlap with a dynamic rigid body AND the character controller
           node, which manifolds alone would NOT give us for a
           non-collidable trigger volume (Bullet never generates collision
           response/manifold points for a ghost object).

        Returns raw (id, id) pairs -- caller (LuaRuntimeManager) decides
        how to map ids to Lua values, including the "__character__"
        sentinel id used by register_external_node()."""
        current_pairs: set[frozenset[str]] = set()

        for manifold in self.bullet_world.getManifolds():
            if manifold.getNumManifoldPoints() <= 0:
                continue
            id0 = self._node_to_id.get(manifold.getNode0())
            id1 = self._node_to_id.get(manifold.getNode1())
            if id0 is not None and id1 is not None and id0 != id1:
                current_pairs.add(frozenset((id0, id1)))

        for body in self._bodies.values():
            if body.kind not in ("ghost", "none") or body.node is None:
                continue
            for other_node in body.node.getOverlappingNodes():
                other_id = self._node_to_id.get(other_node)
                if other_id is not None and other_id != body.instance_id:
                    current_pairs.add(frozenset((body.instance_id, other_id)))

        new_pairs = current_pairs - self._touching_pairs
        self._touching_pairs = current_pairs
        return [tuple(pair) for pair in new_pairs]

    def destroy(self) -> None:
        removed = len(self._bodies)
        for body in self._bodies.values():
            if body.node is not None:
                if body.kind in ("static", "dynamic"):
                    self.bullet_world.removeRigidBody(body.node)
                elif body.kind in ("ghost", "none"):
                    self.bullet_world.removeGhost(body.node)
        self._bodies.clear()
        self._node_to_id.clear()
        self._touching_pairs.clear()
        if DEBUG_PHYSICS:
            print(f"[PHYSICS] destroyed: {removed} bodies removed, world torn down")
