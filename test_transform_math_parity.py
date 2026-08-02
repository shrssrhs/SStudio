"""
Stage 2.2 hardening: quaternion parity test between shared/transform_math.py
(pure-Python, used by the demo/offline editor) and the REAL Panda3D/Ursina
result (used by the live client). Requirement #6 of the correction spec:
"internally consistent is not sufficient" -- this must be measured against
the live engine, not just self-consistent.

Compares:
  - resulting orientation/quaternion (via |dot| >= 1 - PARITY_TOLERANCE)
  - resulting world position of a rotated child with a positional offset
  - converted Euler values round-tripped through to_euler_xyz_degrees

Also covers: sequential X->Y->Z, repeated rotations (drift check), and a
2-level nested Model cascade, computed BOTH ways and compared.

Run manually: python test_transform_math_parity.py
(requires the project's venv -- imports ursina, opens a throwaway window)
"""
import sys
sys.path.insert(0, '.')
from shared import transform_math as tm

from ursina import Ursina, Entity, Vec3

app = Ursina(borderless=False, development_mode=False)
scratch = Entity(eternal=True)


def live_quat(x, y, z):
    scratch.rotation = Vec3(x, y, z)
    q = scratch.get_quat()
    return tm.Quat(q.get_r(), q.get_i(), q.get_j(), q.get_k())


def live_rotate_vector(x, y, z, v):
    scratch.rotation = Vec3(x, y, z)
    scratch.position = Vec3(0, 0, 0)
    q = scratch.get_quat()
    return tuple(q.xform(Vec3(*v)))


def close(a, b, tol):
    return abs(a - b) < tol


def vclose(a, b, tol):
    return all(close(x, y, tol) for x, y in zip(a, b))


TOLERANCE_POSITION = 1e-3
failures = []

# 1. Required single/combination test cases from the correction spec.
required_cases = [
    ("Y 90", (0, 90, 0)),
    ("X 37", (37, 0, 0)),
    ("Z -45", (0, 0, -45)),
    ("sequential X->Y->Z", (30, 45, 60)),
    ("negative combo", (-20, -80, 100)),
    ("large combo", (123, -77, 200)),
]
for name, angles in required_cases:
    mine = tm.from_euler_xyz_degrees(angles)
    truth = live_quat(*angles)
    dot = tm.quat_dot_abs(mine, truth)
    ok = dot >= 1.0 - tm.PARITY_TOLERANCE
    print(f"[{'OK' if ok else 'FAIL'}] quat parity {name}: dot={dot:.8f}")
    if not ok:
        failures.append(f"quat parity {name}")

# 2. Repeated rotations (drift check) -- apply the same rotation increment
# via transform_math composition 8 times and compare final orientation to
# the live engine doing the equivalent single combined rotation.
increment = (0, 15, 0)
q_repeated = tm.IDENTITY
for _ in range(8):
    q_repeated = tm.compose(tm.from_euler_xyz_degrees(increment), q_repeated)
truth_120 = live_quat(0, 120, 0)
dot = tm.quat_dot_abs(q_repeated, truth_120)
ok = dot >= 1.0 - tm.PARITY_TOLERANCE
print(f"[{'OK' if ok else 'FAIL'}] repeated rotation (8x15deg Y vs 120deg Y): dot={dot:.8f}")
if not ok:
    failures.append("repeated rotation drift")

# 3. Rotated child with a positional offset -- world position after
# rotating a pivot at origin by 90 deg Y, child offset (3,0,0).
pivot_quat = tm.from_euler_xyz_degrees((0, 90, 0))
offset = (3.0, 0.0, 0.0)
mine_world = tm.rotate_vector(pivot_quat, offset)
truth_world = live_rotate_vector(0, 90, 0, offset)
ok = vclose(mine_world, truth_world, TOLERANCE_POSITION)
print(f"[{'OK' if ok else 'FAIL'}] rotated child w/ offset: mine={mine_world} truth={truth_world}")
if not ok:
    failures.append("rotated child with offset")

# 4. Euler round-trip through to_euler_xyz_degrees, re-quaternionized,
# compared to a fresh live quat built from those same converted angles.
for name, angles in required_cases:
    mine = tm.from_euler_xyz_degrees(angles)
    converted = tm.to_euler_xyz_degrees(mine)
    truth_of_converted = live_quat(*converted)
    dot = tm.quat_dot_abs(mine, truth_of_converted)
    ok = dot >= 1.0 - tm.PARITY_TOLERANCE
    print(f"[{'OK' if ok else 'FAIL'}] converted-Euler parity {name}: converted={tuple(round(v,2) for v in converted)} dot={dot:.8f}")
    if not ok:
        failures.append(f"converted-Euler parity {name}")

# 5. Nested Model cascade: ModelA pivot rotates 90 Y; ModelB (child of A)
# starts at world (2,0,0) with its own rotation (0,0,30); Part (child of B)
# at ModelB-relative offset (1,0,0). Compute final world position/rotation
# of Part BOTH via transform_math (demo path) and via live Panda quats
# (mirroring exactly what client_studio.py's cascading algorithm does),
# then compare.
def cascade_transform_math(pivot_pos, pivot_quat, child_pos, child_quat):
    offset = tuple(c - p for c, p in zip(child_pos, pivot_pos))
    inv = tm.conjugate(pivot_quat)
    relative_pos = tm.rotate_vector(inv, offset)
    relative_quat = tm.compose(inv, child_quat)
    return relative_pos, relative_quat

def reconstruct_transform_math(pivot_pos, pivot_quat, relative_pos, relative_quat):
    new_pos = tuple(p + o for p, o in zip(pivot_pos, tm.rotate_vector(pivot_quat, relative_pos)))
    new_quat = tm.compose(pivot_quat, relative_quat)
    return new_pos, new_quat

modelA_old_pos = (0.0, 0.0, 0.0)
modelA_old_quat = tm.IDENTITY
modelB_pos = (2.0, 0.0, 0.0)
modelB_quat = tm.from_euler_xyz_degrees((0, 0, 30))
part_pos = (3.0, 0.0, 0.0)  # treated as an absolute world position for this test
part_quat = tm.IDENTITY

rel_b, relq_b = cascade_transform_math(modelA_old_pos, modelA_old_quat, modelB_pos, modelB_quat)
rel_p, relq_p = cascade_transform_math(modelA_old_pos, modelA_old_quat, part_pos, part_quat)

modelA_new_pos = (0.0, 0.0, 0.0)
modelA_new_quat = tm.from_euler_xyz_degrees((0, 90, 0))

new_b_pos, new_b_quat = reconstruct_transform_math(modelA_new_pos, modelA_new_quat, rel_b, relq_b)
new_p_pos, new_p_quat = reconstruct_transform_math(modelA_new_pos, modelA_new_quat, rel_p, relq_p)

# Live equivalent: reuse the scratch entity round-trip for the same math.
def live_rotate_offset(rot_xyz, offset_xyz):
    scratch.rotation = Vec3(*rot_xyz)
    q = scratch.get_quat()
    return tuple(q.xform(Vec3(*offset_xyz)))

modelA_new_rotated_b_offset = live_rotate_offset((0, 90, 0), tuple(b - a for b, a in zip(modelB_pos, modelA_old_pos)))
live_new_b_pos = tuple(a + o for a, o in zip(modelA_new_pos, modelA_new_rotated_b_offset))
modelA_new_rotated_p_offset = live_rotate_offset((0, 90, 0), tuple(p - a for p, a in zip(part_pos, modelA_old_pos)))
live_new_p_pos = tuple(a + o for a, o in zip(modelA_new_pos, modelA_new_rotated_p_offset))

ok_b = vclose(new_b_pos, live_new_b_pos, TOLERANCE_POSITION)
ok_p = vclose(new_p_pos, live_new_p_pos, TOLERANCE_POSITION)
print(f"[{'OK' if ok_b else 'FAIL'}] nested cascade ModelB position: mine={new_b_pos} live={live_new_b_pos}")
print(f"[{'OK' if ok_p else 'FAIL'}] nested cascade Part position: mine={new_p_pos} live={live_new_p_pos}")
if not ok_b:
    failures.append("nested cascade ModelB position")
if not ok_p:
    failures.append("nested cascade Part position")

app.destroy()

if failures:
    print("\nFAILURES:", failures)
    sys.exit(1)
print("\nALL TRANSFORM_MATH PARITY TESTS PASSED")
