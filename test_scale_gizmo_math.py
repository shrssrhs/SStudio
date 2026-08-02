"""Headless test of TransformGizmo's Scale math using real Ursina Entities.
Verifies: opposite-face anchoring (unrotated + rotated Part), uniform
scale proportions + position invariance, minimum-size clamp.
"""
import sys
sys.path.insert(0, '.')
from ursina import Ursina, Entity, Vec3

app = Ursina(borderless=False, development_mode=False)

from transform_gizmo import TransformGizmo, AXIS_DIRECTION, mouse_world_ray
from panda3d.core import Quat


def close(a, b, tol=1e-3):
    return abs(a - b) < tol


gizmo = TransformGizmo()
gizmo.set_mode("scale")

# --- Test 1: unrotated Part, +X handle keeps -X face stationary ---
part = Entity(position=Vec3(0, 0, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
gizmo.set_target(part)
old_size = Vec3(part.scale)
old_position = Vec3(part.position)
neg_x_face_before = old_position.x - old_size.x / 2.0

# Simulate a +X handle drag: build a ray that passes exactly through the
# real +X handle's world position (queried directly, not guessed).
handle_pos_x = Vec3(gizmo._scale_handles["x+"].world_position)
print("x+ handle world position:", handle_pos_x)
ray_direction = Vec3(0, 0, 1)
ray_origin = handle_pos_x - ray_direction * 10
key, _ = gizmo._best_scale_handle_hit(ray_origin, ray_direction)
print("hit handle (expect x+):", key)
assert key == "x+"
started = gizmo._begin_scale_drag(ray_origin, ray_direction)
assert started

# Move the ray further along +X to simulate dragging the handle outward by 1.0
ray_origin2 = ray_origin + Vec3(2.0, 0, 0)
result = gizmo._update_scale_drag(ray_origin2, ray_direction, 0.0, False)
print("axis-scale result:", result)
new_size = result["Size"]
new_position = result["Position"]
neg_x_face_after = new_position.x - new_size.x / 2.0
print(f"-X face before={neg_x_face_before} after={neg_x_face_after}")
assert close(neg_x_face_before, neg_x_face_after, 1e-2), "opposite face drifted!"
assert new_size.x > old_size.x, "Size.x should have grown"
assert close(new_size.y, old_size.y) and close(new_size.z, old_size.z), "other axes must not change"
gizmo.end_drag()
print("Test 1 (unrotated +X anchor) PASSED\n")

# --- Test 2: rotated Part (45 deg around Y), +X LOCAL handle ---
part2 = Entity(position=Vec3(5, 0, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 45, 0))
gizmo.set_target(part2)
old_size2 = Vec3(part2.scale)
old_position2 = Vec3(part2.position)
local_quat = Quat(part2.get_quat())
local_x_world = Vec3(local_quat.xform(AXIS_DIRECTION["x"]))
print("local +X world direction (45deg Y rot):", local_x_world)
neg_x_face_before2 = old_position2 - local_x_world * (old_size2.x / 2.0)

# Ray pointing at the +X handle's world position (old_position2 + local_x_world*SCALE_HANDLE_DISTANCE)
from transform_gizmo import SCALE_HANDLE_DISTANCE
handle_world_pos = old_position2 + local_x_world * SCALE_HANDLE_DISTANCE * gizmo._current_scale
# build a ray from a point far along -Z-ish toward that handle (camera-like), simple approach:
# use a ray that passes exactly through the handle position, direction arbitrary but not parallel to local_x_world
ray_dir2 = Vec3(0, 0, 1)
ray_origin_start = handle_world_pos - ray_dir2 * 10
key2, _ = gizmo._best_scale_handle_hit(ray_origin_start, ray_dir2)
print("hit handle (expect x+):", key2)
assert key2 == "x+"
started2 = gizmo._begin_scale_drag(ray_origin_start, ray_dir2)
assert started2

# Drag further along the LOCAL +X world direction by 1.5 units
ray_origin_end = ray_origin_start + local_x_world * 1.5
result2 = gizmo._update_scale_drag(ray_origin_end, ray_dir2, 0.0, False)
print("rotated axis-scale result:", result2)
new_size2 = result2["Size"]
new_position2 = result2["Position"]
neg_x_face_after2 = new_position2 - local_x_world * (new_size2.x / 2.0)
print(f"-X face before={neg_x_face_before2} after={neg_x_face_after2}")
assert close(neg_x_face_before2.x, neg_x_face_after2.x, 1e-2)
assert close(neg_x_face_before2.y, neg_x_face_after2.y, 1e-2)
assert close(neg_x_face_before2.z, neg_x_face_after2.z, 1e-2)
gizmo.end_drag()
print("Test 2 (rotated +X anchor, local axes) PASSED\n")

# --- Test 3: uniform scale preserves proportions, position unchanged ---
part3 = Entity(position=Vec3(-5, 1, 2), scale=Vec3(2, 3, 4), rotation=Vec3(10, 20, 30))
gizmo.set_target(part3)
old_size3 = Vec3(part3.scale)
old_position3 = Vec3(part3.position)
ray_origin3 = old_position3 + Vec3(0, 0, -10)
ray_dir3 = Vec3(0, 0, 1)
key3, _ = gizmo._best_scale_handle_hit(ray_origin3, ray_dir3)
print("hit handle (expect uniform):", key3)
assert key3 == "uniform"
started3 = gizmo._begin_scale_drag(ray_origin3, ray_dir3)
assert started3
# move ray off-axis to simulate dragging away from center
ray_origin3b = old_position3 + Vec3(3, 0, -10)
result3 = gizmo._update_scale_drag(ray_origin3b, ray_dir3, 0.0, False)
print("uniform-scale result:", result3)
new_size3 = result3["Size"]
assert "Position" not in result3, "uniform scale must not touch Position"
ratio_before = (old_size3.x / old_size3.y, old_size3.y / old_size3.z)
ratio_after = (new_size3.x / new_size3.y, new_size3.y / new_size3.z)
print(f"proportions before={ratio_before} after={ratio_after}")
assert close(ratio_before[0], ratio_after[0], 1e-3)
assert close(ratio_before[1], ratio_after[1], 1e-3)
assert part3.position.x == old_position3.x and part3.position.y == old_position3.y and part3.position.z == old_position3.z
gizmo.end_drag()
print("Test 3 (uniform scale proportions + position invariance) PASSED\n")

# --- Test 4: minimum size clamp ---
part4 = Entity(position=Vec3(0, 0, 0), scale=Vec3(1, 1, 1), rotation=Vec3(0, 0, 0))
gizmo.set_target(part4)
handle_pos_x4 = Vec3(gizmo._scale_handles["x+"].world_position)
ray_dir4 = Vec3(0, 0, 1)
ray_origin4 = handle_pos_x4 - ray_dir4 * 10
key4, _ = gizmo._best_scale_handle_hit(ray_origin4, ray_dir4)
assert key4 == "x+"
gizmo._begin_scale_drag(ray_origin4, ray_dir4)
# Drag WAY past zero (negative direction, huge distance)
ray_origin4b = ray_origin4 + Vec3(-50.0, 0, 0)
result4 = gizmo._update_scale_drag(ray_origin4b, ray_dir4, 0.0, False)
print("min-size clamp result:", result4)
from shared.instance import MIN_PART_SIZE
assert result4["Size"].x >= MIN_PART_SIZE - 1e-6
assert result4["Size"].x > 0
gizmo.end_drag()
print("Test 4 (minimum size clamp, no negative/zero) PASSED\n")

print("ALL SCALE GIZMO MATH TESTS PASSED")
app.destroy()
