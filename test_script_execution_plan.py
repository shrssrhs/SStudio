"""Regression tests for Stage 3.6's build_script_execution_plan()
(lua_runtime.py) -- the multi-root Script/LocalScript discovery mechanism
that replaces the old Workspace-only BFS (_discover_run_order, removed).

Pure data-structure tests: no Qt, no Ursina, no Lua VM. A tiny local
_Record stand-in is used instead of client_studio.InstanceRecord so this
file has zero heavy dependencies and runs in well under a second --
VM/gameplay-context-level behavior (require(), CharacterAdded ordering,
diagnostics, Play/Stop session isolation) is covered separately in
test_lua_gameplay_api.py, which already pays the Qt/Ursina/Bullet cost for
other reasons.
"""
import sys

sys.path.insert(0, '.')

import lua_runtime as lr

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


class _Record:
    __slots__ = ("id", "class_name", "name", "parent_id", "properties", "enabled")

    def __init__(self, instance_id, class_name, name, parent_id, enabled=True):
        self.id = instance_id
        self.class_name = class_name
        self.name = name
        self.parent_id = parent_id
        self.properties = {}
        self.enabled = enabled


def instances(*records: _Record) -> dict[str, _Record]:
    return {r.id: r for r in records}


def plan_ids(entries) -> list[str]:
    return [e.instance_id for e in entries]


# ============================================================
# supported roots
# ============================================================

def test_script_under_workspace_runs() -> None:
    data = instances(_Record("s1", "Script", "S", "Workspace"))
    entries, skipped = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["s1"], "Script under Workspace executes")
    check(skipped == [], "no skipped warning for a supported Script placement")


def test_script_under_server_script_service_runs() -> None:
    data = instances(_Record("s1", "Script", "S", "ServerScriptService"))
    entries, skipped = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["s1"], "Script under ServerScriptService executes")
    check(entries[0].root_service == "ServerScriptService", "entry records ServerScriptService as its root_service")


def test_localscript_under_workspace_runs() -> None:
    data = instances(_Record("l1", "LocalScript", "L", "Workspace"))
    entries, skipped = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["l1"], "LocalScript under Workspace executes (backward compatible)")


def test_localscript_under_starterplayer_runs() -> None:
    data = instances(_Record("l1", "LocalScript", "L", "StarterPlayer"))
    entries, skipped = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["l1"], "LocalScript under StarterPlayer executes")
    check(entries[0].root_service == "StarterPlayer", "entry records StarterPlayer as its root_service")


def test_localscript_nested_under_folder_under_starterplayer_runs() -> None:
    data = instances(
        _Record("f1", "Folder", "Sub", "StarterPlayer"),
        _Record("l1", "LocalScript", "L", "f1"),
    )
    entries, skipped = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["l1"], "LocalScript nested under a Folder under StarterPlayer still executes (parent-before-child)")


# ============================================================
# unsupported placements -- skipped with a warning
# ============================================================

def test_localscript_under_replicatedstorage_is_skipped() -> None:
    data = instances(_Record("l1", "LocalScript", "Input", "ReplicatedStorage"))
    entries, skipped = lr.build_script_execution_plan(data)
    check(entries == [], "LocalScript under ReplicatedStorage does not execute")
    check(len(skipped) == 1, "exactly one skipped-location entry")
    check(skipped[0].instance_id == "l1" and skipped[0].class_name == "LocalScript", "skipped entry identifies the right instance/class")
    check(skipped[0].actual_root == "ReplicatedStorage", "skipped entry identifies the actual (unsupported) root")


def test_script_under_starterplayer_is_skipped() -> None:
    """Script is a SERVER-side root concept; StarterPlayer is LocalScript-only."""
    data = instances(_Record("s1", "Script", "S", "StarterPlayer"))
    entries, skipped = lr.build_script_execution_plan(data)
    check(entries == [], "Script under StarterPlayer (a LocalScript-only root) does not execute")
    check(len(skipped) == 1 and skipped[0].actual_root == "StarterPlayer", "Script under an unsupported root is skipped with one warning")


def test_modulescript_never_auto_runs_and_never_warns() -> None:
    data = instances(
        _Record("m1", "ModuleScript", "M", "Workspace"),
        _Record("m2", "ModuleScript", "M2", "ReplicatedStorage"),
    )
    entries, skipped = lr.build_script_execution_plan(data)
    check(entries == [], "ModuleScript never appears in the execution plan, even under a supported root")
    check(skipped == [], "ModuleScript never receives a skipped-location warning merely for not auto-running")


# ============================================================
# Enabled handling
# ============================================================

def test_disabled_script_does_not_execute() -> None:
    data = instances(_Record("s1", "Script", "S", "ServerScriptService", enabled=False))
    entries, skipped = lr.build_script_execution_plan(data)
    check(entries == [], "a disabled Script does not execute")
    check(skipped == [], "a disabled Script does not generate a skipped-location warning either (it's simply off)")


def test_disabled_localscript_does_not_execute() -> None:
    data = instances(_Record("l1", "LocalScript", "L", "StarterPlayer", enabled=False))
    entries, skipped = lr.build_script_execution_plan(data)
    check(entries == [], "a disabled LocalScript does not execute")


# ============================================================
# ordering / dedup / identity
# ============================================================

def test_overlapping_roots_do_not_duplicate_execution() -> None:
    data = instances(
        _Record("s1", "Script", "S", "Workspace"),
        _Record("s2", "Script", "S2", "ServerScriptService"),
        _Record("l1", "LocalScript", "L", "Workspace"),
        _Record("l2", "LocalScript", "L2", "StarterPlayer"),
    )
    entries, skipped = lr.build_script_execution_plan(data)
    ids = plan_ids(entries)
    check(len(ids) == len(set(ids)), "no instance id appears more than once across all three roots")
    check(set(ids) == {"s1", "s2", "l1", "l2"}, "every eligible script from every root is included exactly once")


def test_deterministic_order_is_identical_across_runs() -> None:
    data = instances(
        _Record("s1", "Script", "S", "ServerScriptService"),
        _Record("l1", "LocalScript", "L", "StarterPlayer"),
        _Record("s2", "Script", "S2", "Workspace"),
        _Record("l2", "LocalScript", "L2", "Workspace"),
    )
    first, _ = lr.build_script_execution_plan(data)
    second, _ = lr.build_script_execution_plan(data)
    check(plan_ids(first) == plan_ids(second), "repeated calls with the same input produce the exact same order")


def test_workspace_scripts_ordered_before_other_roots() -> None:
    data = instances(
        _Record("s1", "Script", "S", "ServerScriptService"),
        _Record("l1", "LocalScript", "L", "StarterPlayer"),
        _Record("w1", "Script", "W", "Workspace"),
    )
    entries, _ = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["w1", "s1", "l1"], "Workspace-rooted scripts run before ServerScriptService, which runs before StarterPlayer")


def test_tiebreak_by_instance_id_within_one_parent() -> None:
    data = instances(
        _Record("zzz", "Script", "Z", "Workspace"),
        _Record("aaa", "Script", "A", "Workspace"),
    )
    entries, _ = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["aaa", "zzz"], "siblings under the same parent are ordered by instance id, not insertion order")


def test_duplicate_display_names_remain_distinct_by_id() -> None:
    data = instances(
        _Record("id_a", "Script", "Script1", "ServerScriptService"),
        _Record("id_b", "Script", "Script1", "Workspace"),
    )
    entries, _ = lr.build_script_execution_plan(data)
    check(set(plan_ids(entries)) == {"id_a", "id_b"}, "two scripts sharing a display name still both execute as distinct instances")


def test_does_not_mutate_input() -> None:
    data = instances(_Record("s1", "Script", "S", "Workspace"))
    snapshot = dict(data)
    lr.build_script_execution_plan(data)
    check(data == snapshot, "build_script_execution_plan never mutates the instances dict it was given")


def test_order_key_matches_list_position() -> None:
    data = instances(
        _Record("s1", "Script", "S", "Workspace"),
        _Record("s2", "Script", "S2", "ServerScriptService"),
    )
    entries, _ = lr.build_script_execution_plan(data)
    check([e.order_key for e in entries] == list(range(len(entries))), "order_key is a dense 0-based index matching list position")


def test_cycle_disconnected_from_any_root_does_not_hang() -> None:
    """A corrupted parent_id cycle that is NOT reachable from any known
    root must simply be invisible to the plan, not hang the whole
    traversal -- see build_script_execution_plan's docstring for why this
    can't happen for a root-reachable script given the single-parent_id
    model; this exercises the orphaned-cycle case directly."""
    data = instances(
        _Record("a", "Script", "A", "b"),
        _Record("b", "Script", "B", "a"),
        _Record("real", "Script", "Real", "Workspace"),
    )
    entries, skipped = lr.build_script_execution_plan(data)
    check(plan_ids(entries) == ["real"], "an orphaned cycle disconnected from every root is simply never visited")
    check({s.instance_id for s in skipped} == {"a", "b"}, "the orphaned pair still shows up as skipped (enabled, right class, never reached)")


def test_empty_instances() -> None:
    entries, skipped = lr.build_script_execution_plan({})
    check(entries == [] and skipped == [], "an empty instances dict produces an empty plan and no warnings")


TESTS = [
    test_script_under_workspace_runs,
    test_script_under_server_script_service_runs,
    test_localscript_under_workspace_runs,
    test_localscript_under_starterplayer_runs,
    test_localscript_nested_under_folder_under_starterplayer_runs,
    test_localscript_under_replicatedstorage_is_skipped,
    test_script_under_starterplayer_is_skipped,
    test_modulescript_never_auto_runs_and_never_warns,
    test_disabled_script_does_not_execute,
    test_disabled_localscript_does_not_execute,
    test_overlapping_roots_do_not_duplicate_execution,
    test_deterministic_order_is_identical_across_runs,
    test_workspace_scripts_ordered_before_other_roots,
    test_tiebreak_by_instance_id_within_one_parent,
    test_duplicate_display_names_remain_distinct_by_id,
    test_does_not_mutate_input,
    test_order_key_matches_list_position,
    test_cycle_disconnected_from_any_root_does_not_hang,
    test_empty_instances,
]

for _test in TESTS:
    _test()

if FAILURES:
    print(f"\n{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(f" - {f}")
    sys.exit(1)

print(f"\nAll {len(TESTS)} script-execution-plan tests passed.")
