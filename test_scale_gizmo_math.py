"""Headless test of TransformGizmo's Scale math using real Ursina Entities.
Verifies: opposite-face anchoring (unrotated + rotated Part), uniform
scale proportions + position invariance, minimum-size clamp, the uniform
handle's pixel-space drag dead zone, and the delta-based factor baseline
(bug-report follow-up: "uniform handle instant jump on mouse-down").
"""
import sys
sys.path.insert(0, '.')
from ursina import Ursina, Entity, Vec3

app = Ursina(borderless=False, development_mode=False)

from transform_gizmo import TransformGizmo, AXIS_DIRECTION, mouse_world_ray, SCALE_UNIFORM_DEAD_ZONE_PIXELS
from panda3d.core import Quat


def close(a, b, tol=1e-3):
    return abs(a - b) < tol


def set_mouse_pixel(g, x, y):
    """Overrides TransformGizmo._current_mouse_pixel() on this instance so
    the pixel-space dead zone can be driven deterministically in a
    headless test, independent of wherever the real OS mouse cursor
    happens to be (which the synthetic world-space rays below don't move
    at all). A plain function stored directly on the instance shadows the
    class's @staticmethod without needing a bound-method wrapper."""
    g._current_mouse_pixel = lambda: (float(x), float(y))


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

# --- Test 3: uniform scale (past the pixel dead zone) preserves
# proportions, position unchanged. ---
part3 = Entity(position=Vec3(-5, 1, 2), scale=Vec3(2, 3, 4), rotation=Vec3(10, 20, 30))
gizmo.set_target(part3)
old_size3 = Vec3(part3.scale)
old_position3 = Vec3(part3.position)
ray_origin3 = old_position3 + Vec3(0, 0, -10)
ray_dir3 = Vec3(0, 0, 1)
key3, _ = gizmo._best_scale_handle_hit(ray_origin3, ray_dir3)
print("hit handle (expect uniform):", key3)
assert key3 == "uniform"
set_mouse_pixel(gizmo, 0.0, 0.0)
started3 = gizmo._begin_scale_drag(ray_origin3, ray_dir3)
assert started3
# move ray off-axis (world space) AND move the simulated mouse pixel well
# past the dead zone (screen space) to simulate dragging away from center.
ray_origin3b = old_position3 + Vec3(3, 0, -10)
set_mouse_pixel(gizmo, 50.0, 0.0)
result3 = gizmo._update_scale_drag(ray_origin3b, ray_dir3, 0.0, False)
print("uniform-scale result:", result3)
assert result3 is not None, "past the dead zone, a real movement must produce a write"
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

# --- Test 4: minimum size clamp (axis handle) ---
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

# --- Test 5 (REQUIRED test 1: mouse-down, zero movement): pressing and
# holding the uniform handle with the simulated mouse pixel completely
# stationary must produce NO write at all -- update_drag returns None,
# not a dict with an unchanged value. Also exercises the near-zero
# click-to-center ray distance that triggered the original bug. ---
part5 = Entity(position=Vec3(-3, 2, 6), scale=Vec3(2, 3, 4), rotation=Vec3(0, 0, 0))
gizmo.set_target(part5)
old_size5 = Vec3(part5.scale)
old_position5 = Vec3(part5.position)
ray_dir5 = Vec3(0.15, 0.1, 1).normalized()
ray_origin5 = old_position5 - ray_dir5 * 10 + Vec3(0.003, -0.002, 0)
key5, _ = gizmo._best_scale_handle_hit(ray_origin5, ray_dir5)
print("hit handle (expect uniform):", key5)
assert key5 == "uniform"
set_mouse_pixel(gizmo, 400.0, 300.0)
started5 = gizmo._begin_scale_drag(ray_origin5, ray_dir5)
assert started5
print("drag_start_uniform_distance (true, unclamped):", gizmo._drag_start_uniform_distance)
print("drag_start_mouse_pixel:", gizmo._drag_start_mouse_pixel)
# SAME ray AND same simulated mouse pixel -- zero movement since press.
result5 = gizmo._update_scale_drag(ray_origin5, ray_dir5, 0.0, False)
print("zero-movement result (must be None):", result5)
assert result5 is None, "zero physical pointer movement must produce NO write at all, not a factor==1.0 write"
assert part5.scale == old_size5, "Entity.scale itself must be untouched"
assert part5.position.x == old_position5.x and part5.position.y == old_position5.y and part5.position.z == old_position5.z
# Repeated frames, still zero movement: still None, every time.
for _ in range(5):
    result5b = gizmo._update_scale_drag(ray_origin5, ray_dir5, 0.0, False)
    assert result5b is None
gizmo.end_drag()
print("Test 5 (uniform handle, zero movement -> NO write at all, repeated frames included) PASSED\n")

# --- Test 6 (REQUIRED test 2/4: small movement + no cumulative growth):
# a movement just past the dead zone produces a small proportional
# resize, and repeating the SAME (already-moved) pixel position again
# does not keep growing. ---
part6 = Entity(position=Vec3(1, 0, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
gizmo.set_target(part6)
old_size6 = Vec3(part6.scale)
ray_dir6 = Vec3(0.15, 0.1, 1).normalized()
ray_origin6 = part6.position - ray_dir6 * 10
key6, _ = gizmo._best_scale_handle_hit(ray_origin6, ray_dir6)
assert key6 == "uniform"
set_mouse_pixel(gizmo, 0.0, 0.0)
gizmo._begin_scale_drag(ray_origin6, ray_dir6)

# Still inside the dead zone (just under the threshold): no write.
set_mouse_pixel(gizmo, SCALE_UNIFORM_DEAD_ZONE_PIXELS - 1.0, 0.0)
inside_dead_zone = gizmo._update_scale_drag(ray_origin6, ray_dir6, 0.0, False)
print("just-inside-dead-zone result (must be None):", inside_dead_zone)
assert inside_dead_zone is None

# Just past the threshold: a small, not huge, change. Tiny world-space
# ray nudge to go with the tiny pixel movement (world-space delta is what
# actually drives the factor; the pixel check is purely a gate).
tiny_offset = Vec3(0.002, 0.0, 0.0)
set_mouse_pixel(gizmo, SCALE_UNIFORM_DEAD_ZONE_PIXELS + 1.0, 0.0)
result6a = gizmo._update_scale_drag(ray_origin6 + tiny_offset, ray_dir6, 0.0, False)
print("just-past-dead-zone result:", result6a)
assert result6a is not None, "past the dead zone, a real movement must produce a write"
factor6a = result6a["Size"].x / old_size6.x
assert 0.0 < abs(factor6a - 1.0) < 0.5, f"a small movement should produce a small, not huge, change (factor={factor6a})"

# Repeated frames with the SAME (already-moved) pixel+ray position -> no
# further cumulative growth.
result6b = gizmo._update_scale_drag(ray_origin6 + tiny_offset, ray_dir6, 0.0, False)
result6c = gizmo._update_scale_drag(ray_origin6 + tiny_offset, ray_dir6, 0.0, False)
print("repeated-frame results:", result6b["Size"], result6c["Size"])
assert result6b["Size"] == result6a["Size"] == result6c["Size"], "repeated frames with no further pointer movement must not accumulate"
gizmo.end_drag()
print("Test 6 (dead zone gating + small movement + no cumulative growth) PASSED\n")

# --- Test 7 (REQUIRED test 3: return to original pointer position):
# move outward past the dead zone, then return to the EXACT original
# press pixel+ray -> back inside the dead zone -> None (equivalent to
# "exact original Size", since no write means nothing changed it). ---
part7 = Entity(position=Vec3(0, 0, 4), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
gizmo.set_target(part7)
old_size7 = Vec3(part7.scale)
ray_dir7 = Vec3(0.15, 0.1, 1).normalized()
ray_origin7 = part7.position - ray_dir7 * 10
set_mouse_pixel(gizmo, 0.0, 0.0)
gizmo._begin_scale_drag(ray_origin7, ray_dir7)

set_mouse_pixel(gizmo, 40.0, 0.0)
moved_result = gizmo._update_scale_drag(ray_origin7 + Vec3(1.0, 0, 0), ray_dir7, 0.0, False)
print("moved-out result:", moved_result)
assert moved_result is not None
assert moved_result["Size"] != old_size7

set_mouse_pixel(gizmo, 0.0, 0.0)
returned_result = gizmo._update_scale_drag(ray_origin7, ray_dir7, 0.0, False)
print("returned-to-start result (must be None -> Size stays at its last written value, which is the moved one, OR unchanged if never re-applied):", returned_result)
# Back inside the dead zone: no NEW write happens, so Entity.scale keeps
# whatever the LAST real write set it to (moved_result's size) -- the
# dead zone means "don't write", not "snap back". To verify the
# immutable-baseline invariant (returning to the exact start pointer
# restores the exact start Size), drive it PAST the dead zone again at
# the exact start ray/ray-adjacent pixel offset that still counts as
# "moved" in pixel-space but maps back to the original world-space ray.
set_mouse_pixel(gizmo, SCALE_UNIFORM_DEAD_ZONE_PIXELS + 1.0, 0.0)
returned_result2 = gizmo._update_scale_drag(ray_origin7, ray_dir7, 0.0, False)
print("returned-to-start (past dead zone) result:", returned_result2)
assert returned_result2["Size"] == old_size7, "returning to the exact start world-space ray must restore the exact start Size"
gizmo.end_drag()
print("Test 7 (move out and return to origin -> exact original Size) PASSED\n")

# --- Test 8 (REQUIRED test 5: snap enabled, no jump on mouse-down) ---
part8 = Entity(position=Vec3(2, -1, 3), scale=Vec3(2.1, 3.3, 4.7), rotation=Vec3(0, 0, 0))
gizmo.set_target(part8)
old_size8 = Vec3(part8.scale)
ray_dir8 = Vec3(0.15, 0.1, 1).normalized()
ray_origin8 = part8.position - ray_dir8 * 10 + Vec3(0.001, 0.001, 0)
set_mouse_pixel(gizmo, 0.0, 0.0)
gizmo._begin_scale_drag(ray_origin8, ray_dir8)
result8_snap = gizmo._update_scale_drag(ray_origin8, ray_dir8, 0.25, True)
print("snap-enabled zero-movement result (must be None):", result8_snap)
assert result8_snap is None, "snap-enabled zero movement must still produce NO write"
assert part8.scale == old_size8
gizmo.end_drag()
print("Test 8 (snap enabled, no jump on mouse-down) PASSED\n")

# --- Test 9 (REQUIRED test 6: snap disabled, no jump on mouse-down) ---
part9 = Entity(position=Vec3(-2, 1, -3), scale=Vec3(1.7, 2.9, 3.3), rotation=Vec3(0, 0, 0))
gizmo.set_target(part9)
old_size9 = Vec3(part9.scale)
ray_dir9 = Vec3(0.15, 0.1, 1).normalized()
ray_origin9 = part9.position - ray_dir9 * 10 + Vec3(0.001, -0.001, 0)
set_mouse_pixel(gizmo, 0.0, 0.0)
gizmo._begin_scale_drag(ray_origin9, ray_dir9)
result9 = gizmo._update_scale_drag(ray_origin9, ray_dir9, 0.0, False)
print("snap-disabled zero-movement result (must be None):", result9)
assert result9 is None
assert part9.scale == old_size9
gizmo.end_drag()
print("Test 9 (snap disabled, no jump on mouse-down) PASSED\n")

# --- Test 10 (REQUIRED test 7: non-cubic Part, proportions unchanged) ---
part10 = Entity(position=Vec3(4, 4, 4), scale=Vec3(1.0, 2.0, 5.0), rotation=Vec3(0, 0, 0))
gizmo.set_target(part10)
old_size10 = Vec3(part10.scale)
ray_dir10 = Vec3(0.15, 0.1, 1).normalized()
ray_origin10 = part10.position - ray_dir10 * 10
set_mouse_pixel(gizmo, 0.0, 0.0)
gizmo._begin_scale_drag(ray_origin10, ray_dir10)
set_mouse_pixel(gizmo, 60.0, 0.0)
result10 = gizmo._update_scale_drag(ray_origin10 + Vec3(2.0, 0, 0), ray_dir10, 0.0, False)
print("non-cubic scale result:", result10)
new_size10 = result10["Size"]
ratio10_before = (old_size10.x / old_size10.y, old_size10.y / old_size10.z)
ratio10_after = (new_size10.x / new_size10.y, new_size10.y / new_size10.z)
assert close(ratio10_before[0], ratio10_after[0], 1e-3)
assert close(ratio10_before[1], ratio10_after[1], 1e-3)
gizmo.end_drag()
print("Test 10 (non-cubic Part, proportions preserved) PASSED\n")

# --- Test 11 (REQUIRED test 8: drag toward minimum clamps correctly) ---
part11 = Entity(position=Vec3(0, 5, 0), scale=Vec3(1, 1, 1), rotation=Vec3(0, 0, 0))
gizmo.set_target(part11)
ray_dir11 = Vec3(0.15, 0.1, 1).normalized()
ray_origin11 = part11.position - ray_dir11 * 10 + Vec3(0.2, 0, 0)
key11, _ = gizmo._best_scale_handle_hit(ray_origin11, ray_dir11)
assert key11 == "uniform"
set_mouse_pixel(gizmo, 0.0, 0.0)
gizmo._begin_scale_drag(ray_origin11, ray_dir11)
print("Test 11 drag_start_uniform_distance:", gizmo._drag_start_uniform_distance)
ray_origin11_center = part11.position - ray_dir11 * 10
set_mouse_pixel(gizmo, 60.0, 0.0)
result11 = gizmo._update_scale_drag(ray_origin11_center, ray_dir11, 0.0, False)
print("uniform min-size clamp result:", result11)
assert result11["Size"].x >= MIN_PART_SIZE - 1e-6
assert result11["Size"].y >= MIN_PART_SIZE - 1e-6
assert result11["Size"].z >= MIN_PART_SIZE - 1e-6
assert close(result11["Size"].x, MIN_PART_SIZE, 1e-3), "should have clamped to the minimum, not just shrunk a bit"
gizmo.end_drag()
print("Test 11 (uniform handle drag toward minimum clamps correctly) PASSED\n")

# --- Test 12 (REQUIRED test 9: axis handles remain unchanged) — re-run
# of Tests 1-2's exact scenarios, same expected values, no pixel-mouse
# patching involved (the dead zone only gates the uniform handle). ---
part12 = Entity(position=Vec3(0, 0, 0), scale=Vec3(2, 2, 2), rotation=Vec3(0, 0, 0))
gizmo.set_target(part12)
handle_pos_x12 = Vec3(gizmo._scale_handles["x+"].world_position)
ray_dir12 = Vec3(0, 0, 1)
ray_origin12 = handle_pos_x12 - ray_dir12 * 10
gizmo._begin_scale_drag(ray_origin12, ray_dir12)
result12 = gizmo._update_scale_drag(ray_origin12 + Vec3(2.0, 0, 0), ray_dir12, 0.0, False)
print("axis handle regression result:", result12)
assert close(result12["Size"].x, 7.2) and close(result12["Position"].x, 2.6)
gizmo.end_drag()
print("Test 12 (axis handles unaffected by the uniform-handle fix) PASSED\n")

print("ALL SCALE GIZMO MATH TESTS PASSED")
app.destroy()
