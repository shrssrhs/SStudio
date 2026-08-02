"""Headless test of physics.py's PhysicsWorld against real Ursina entities,
mirroring the Stage 2.4 manual test scenarios (floor, falling cube, ghost
cube, anchored ghost, wall) purely in-process (no server/network needed --
physics.py only touches local Entity transforms).
"""
import sys
sys.path.insert(0, '.')
from ursina import Ursina, Entity, Vec3

app = Ursina(borderless=False, development_mode=False)

import physics

physics.DEBUG_PHYSICS = True


def close(a, b, tol=0.05):
    return abs(a - b) < tol


# --- Scenario A + B: floor (anchored, collidable) + falling cube ---
world = physics.PhysicsWorld()

floor_entity = Entity(position=Vec3(0, 0, 0), scale=Vec3(20, 1, 20), rotation=Vec3(0, 0, 0))
world.add_part("floor", floor_entity, [0, 0, 0], [0, 0, 0], [20, 1, 20], anchored=True, can_collide=True)

cube_entity = Entity(position=Vec3(0, 10, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
world.add_part("cube", cube_entity, [0, 10, 0], [0, 0, 0], [2, 2, 2], anchored=False, can_collide=True)

assert world.body_count() == 2

for _ in range(400):
    world.step(1 / 60)

print("cube final position:", cube_entity.position)
# Floor top at y=0.5, cube half-height=1 -> expected resting center y=1.5
assert close(cube_entity.y, 1.5, 0.1), f"cube should rest on floor at y=1.5, got {cube_entity.y}"
assert close(floor_entity.y, 0.0), "anchored floor must never move"
print("Scenario A/B (floor + falling cube rests) PASSED\n")

world.destroy()
assert world.body_count() == 0

# --- Scenario C: ghost cube (Anchored=false, CanCollide=false) falls
# through the floor ---
world2 = physics.PhysicsWorld()
world2.add_part("floor2", floor_entity, [0, 0, 0], [0, 0, 0], [20, 1, 20], anchored=True, can_collide=True)

ghost_entity = Entity(position=Vec3(5, 10, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
world2.add_part("ghost", ghost_entity, [5, 10, 0], [0, 0, 0], [2, 2, 2], anchored=False, can_collide=False)

for _ in range(120):
    world2.step(1 / 60)

print("ghost position after 2s:", ghost_entity.position)
assert ghost_entity.y < 0.0, f"ghost must fall THROUGH the floor (y<0), got {ghost_entity.y}"
print("Scenario C (ghost falls through floor) PASSED\n")

world2.destroy()

# --- Scenario D: anchored ghost (Anchored=true, CanCollide=false) stays
# fixed; nothing collides with it (verified separately: no body created) ---
world3 = physics.PhysicsWorld()
anchored_ghost_entity = Entity(position=Vec3(-5, 3, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
world3.add_part("anchored_ghost", anchored_ghost_entity, [-5, 3, 0], [0, 0, 0], [2, 2, 2], anchored=True, can_collide=False)
assert world3._bodies["anchored_ghost"].kind == "none"
for _ in range(120):
    world3.step(1 / 60)
assert anchored_ghost_entity.position.x == -5 and anchored_ghost_entity.position.y == 3 and anchored_ghost_entity.position.z == 0
print("Scenario D (anchored ghost stays fixed, no body) PASSED\n")
world3.destroy()

# --- Scenario E: rotated wall blocks a falling/moving object ---
world4 = physics.PhysicsWorld()
wall_entity = Entity(position=Vec3(0, 2, 0), scale=Vec3(0.5, 6, 6), rotation=Vec3(0, 30, 0))
world4.add_part("wall", wall_entity, [0, 2, 0], [0, 30, 0], [0.5, 6, 6], anchored=True, can_collide=True)

falling_entity = Entity(position=Vec3(0, 10, 0), scale=Vec3(1, 1, 1), rotation=Vec3(0, 0, 0))
world4.add_part("falling_on_wall", falling_entity, [0, 10, 0], [0, 0, 0], [1, 1, 1], anchored=False, can_collide=True)

for _ in range(300):
    world4.step(1 / 60)

print("object resting on rotated wall position:", falling_entity.position)
# Should rest ON TOP of the wall (wall top surface is roughly y=2+3=5 along
# its local up, but it's rotated so just assert it did NOT fall past the
# wall's vertical extent down to some large negative/very low Y).
assert falling_entity.y > 3.0, f"object should be resting on top of the wall, not fallen past it (y={falling_entity.y})"
print("Scenario E (rotated wall blocks falling object) PASSED\n")
world4.destroy()

print("ALL PHYSICS MODULE TESTS PASSED")
app.destroy()
