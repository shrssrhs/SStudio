"""
Stage 3.0: sandboxed Lua runtime and minimal object API.

Architecture summary (see Stage 3.0 final report for full rationale):

- ONE real Lua 5.4 VM (lupa.lua54.LuaRuntime) per Play session, owned by
  LuaRuntimeManager. Scripts do NOT each get a separate lua_State (that was
  SCRIPTING_ARCHITECTURE.md's original sketch) -- they get separate
  *environments* instead (Lua 5.4's per-chunk `_ENV`, set via the 4th
  argument to `load`), which gives the same "no shared globals between
  scripts" guarantee at a fraction of the cost of spinning up dozens of
  embedded VMs, and keeps cross-script Instance proxy identity trivial
  (same VM = same Python<->Lua boundary).
- Every safe value type and every Instance proxy is a real Lua TABLE with a
  locked metatable (`__metatable` set to a non-nil sentinel, so
  getmetatable() can't retrieve the writable table and setmetatable()
  raises), never a raw Python object handed into Lua. This was a deliberate
  reversal from an initial Python-object-proxy prototype: a plain Python
  object exposed to Lua leaks Python internals through ordinary attribute
  access (`proxy.__class__` returns the real class -- verified
  interactively), and Lua does not dispatch its arithmetic/equality
  operators to Python dunder methods, so Vector3/Color3 could not get
  natural `+`/`-`/`*`/`==` syntax that way either. Lua tables with locked
  metatables solve both problems and match this module's own explicit
  design preference.
- Instance proxies read/write through a small, fixed set of Python "bridge"
  functions (`__bridge_get`, `__bridge_set`, `__bridge_find_first_child`,
  ...) that are registered as real Lua globals ONCE, then closed over by
  the prelude's proxy constructor -- they are never placed into a script's
  sandboxed `_ENV`, so user Source cannot call them directly by name even
  though they exist in `_G`.
- CPU protection: `debug.sethook(coroutine, hook, "", N)` installs an
  instruction-count hook on a Lua thread. This does NOT apply automatically
  to coroutines created from the hooked thread -- verified interactively: a
  `while true do end` inside a `coroutine.create`d function ran for over two
  minutes unmolested with only the main thread hooked. Every coroutine this
  module ever resumes (the top-level Script coroutine AND every
  task.spawn/task.delay/task.defer coroutine) gets its own
  `debug.sethook(..., "", MAX_INSTRUCTIONS_PER_RESUME)` call at creation
  time, before its first resume. The `debug` global itself is never placed
  into any script's sandboxed `_ENV`, so scripts cannot install their own
  hook or remove this one.
- Memory protection: `LuaRuntime.set_max_memory()` (a real lupa feature --
  it installs a custom Lua allocator) caps the whole VM for the Play
  session; `get_memory_used()` is exposed to diagnostics.
- Runtime scene isolation: Lua property writes to Part-like instances never
  touch `MultiplayerGame.instances[...].properties` (the authoritative,
  server-synced state) or call `apply_property_edit()`. They mutate the
  visible Entity directly (same object `game.parts[id]` already is) and a
  local RuntimeSceneLayer overlay/snapshot, mirroring the existing
  `_physics_snapshot` / `_start_physics` / `_stop_physics` pattern in
  client_studio.py. Stop() restores every touched property from the
  snapshot captured at Play start, independently of (and before) the
  existing physics Stop-restore.
"""

from __future__ import annotations

import itertools as _itertools
import re as _re
import time as _time
import traceback as _traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from lupa import lua54

# ============================================================
# CONSTANTS
# ============================================================

DEBUG_LUA_RUNTIME = False

MAX_SOURCE_SIZE = 20000  # matches shared/object_registry.py Script.Source PropertySpec("string", 20000)
MAX_VM_MEMORY_BYTES = 64 * 1024 * 1024  # 64 MiB per Play session, whole VM
MAX_INSTRUCTIONS_PER_RESUME = 200_000  # per coroutine.resume() call
MAX_FRAME_WALL_TIME = 0.25  # secondary wall-clock guard per scheduler.update(), seconds
MAX_PRINT_LINES_PER_SECOND = 40
MAX_RUNTIME_INSTANCES = 500
MAX_QUEUED_TASKS_PER_SCRIPT = 200
API_VERSION = "0.1"

_RUNTIME_ID_PREFIX = "runtime:"


def _debug(message: str) -> None:
    if DEBUG_LUA_RUNTIME:
        print(f"[LUA_RUNTIME] {message}")


def _lua_array_to_list(value: Any) -> Any:
    """Normalizes a Lua array-style table (1-based: value[1], value[2],
    value[3], ...) into a plain 0-based Python list. Necessary because
    lupa's indexing convention flips depending on which language is doing
    the indexing: a raw Python list read FROM Lua uses Lua's own [1]
    convention translated onto Python's __getitem__ (so `list[1]` in Lua
    yields the SECOND Python element -- verified interactively), while a
    genuine Lua table read FROM PYTHON uses Lua's native 1-based keys
    directly (`t[1]` is the Lua table's first element, `t[0]` is None).
    The prelude's unwrap_value() always hands bridge_set() a genuine Lua
    array table `{X, Y, Z}` for Position/Rotation/Size/Color -- this
    converts it to `[X, Y, Z]` so the rest of this module can use
    ordinary 0-based Python indexing everywhere else."""
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        length = len(value)
    except TypeError:
        return value
    return [value[i + 1] for i in range(length)]


class LuaBudgetExceeded(Exception):
    """Raised inside the per-coroutine instruction hook; propagates back
    out of coroutine.resume() as a normal (catchable) Lua error."""


# ============================================================
# DIAGNOSTICS
# ============================================================

@dataclass
class ScriptDiagnostic:
    script_id: str
    script_name: str
    severity: str  # "info" | "warning" | "error"
    message: str
    line: Optional[int] = None
    stack_trace: Optional[str] = None
    chunk_name: str = ""
    timestamp: float = field(default_factory=_time.monotonic)
    session_id: int = 0

    def format(self) -> str:
        if self.severity == "error":
            location = f"{self.chunk_name}:{self.line}" if self.line else self.chunk_name
            return f"[LUA ERROR][{location}] {self.message}"
        if self.severity == "warning":
            return f"[LUA WARNING][{self.script_name}] {self.message}"
        return f"[LUA][{self.script_name}] {self.message}"


_LUA_ERROR_LINE_RE = _re.compile(r':(\d+):')

# Process-wide, never reset per LuaRuntimeManager instance (a fresh instance
# is created on every Play -- see class docstring): Stage 3.1's editor gutter
# needs to tell "this diagnostic belongs to the Play session currently
# running" from "this is a straggler from a Play session that already
# Stopped" apart, which per-instance numbering (always restarting at 1)
# cannot distinguish since two different instances would both report 1.
_SESSION_ID_COUNTER = _itertools.count(1)


def _extract_lua_error_line(message: str) -> Optional[int]:
    """Lua's own error format is always `<chunksource>:<line>: <text>`
    (with chunksource wrapped as `[string "name"]` unless the chunk name
    was given an `@`/`=` prefix) -- this pulls the line back out for
    Stage 3.1's editor gutter markers / Output-click-to-navigate, without
    changing the message text Stage 3.0 already logs to Output."""
    match = _LUA_ERROR_LINE_RE.search(message)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


# ============================================================
# LUA PRELUDE -- Vector3, Color3, Instance proxy scaffolding.
# Executed once per Play session into the VM's real _G (host-privileged),
# never re-executed per script. Only the frozen `Vector3`/`Color3`/
# `Instance`/`typeof` proxies this defines are placed into each script's
# sandboxed _ENV -- the internal metatables/bridge functions are not.
# ============================================================

_PRELUDE_LUA = r"""
local function locked_proxy(real)
    return setmetatable({}, {
        __index = real,
        __newindex = function(_, k) error("cannot modify a protected table (key: " .. tostring(k) .. ")", 2) end,
        __metatable = "locked",
    })
end

-- ---------------- Vector3 ----------------
local Vector3Meta = {}
Vector3Meta.__index = Vector3Meta
Vector3Meta.__metatable = "locked"

local function v3_check(n, name)
    if type(n) ~= "number" or n ~= n or n == math.huge or n == -math.huge then
        error("Vector3 component '" .. name .. "' must be a finite number", 3)
    end
    return n
end

function Vector3Meta.new(x, y, z)
    x = v3_check(x or 0, "X"); y = v3_check(y or 0, "Y"); z = v3_check(z or 0, "Z")
    return setmetatable({X = x, Y = y, Z = z}, Vector3Meta)
end

Vector3Meta.__add = function(a, b) return Vector3Meta.new(a.X + b.X, a.Y + b.Y, a.Z + b.Z) end
Vector3Meta.__sub = function(a, b) return Vector3Meta.new(a.X - b.X, a.Y - b.Y, a.Z - b.Z) end
Vector3Meta.__unm = function(a) return Vector3Meta.new(-a.X, -a.Y, -a.Z) end
Vector3Meta.__mul = function(a, b)
    if type(a) == "number" then return Vector3Meta.new(b.X * a, b.Y * a, b.Z * a) end
    if type(b) == "number" then return Vector3Meta.new(a.X * b, a.Y * b, a.Z * b) end
    error("Vector3 * Vector3 is not supported, use :Dot() or :Cross()", 2)
end
Vector3Meta.__div = function(a, b)
    if type(b) ~= "number" then error("Vector3 can only be divided by a number", 2) end
    if b == 0 then error("attempt to divide Vector3 by zero", 2) end
    return Vector3Meta.new(a.X / b, a.Y / b, a.Z / b)
end
Vector3Meta.__eq = function(a, b) return a.X == b.X and a.Y == b.Y and a.Z == b.Z end
Vector3Meta.__tostring = function(a) return string.format("%g, %g, %g", a.X, a.Y, a.Z) end

function Vector3Meta:Dot(other) return self.X * other.X + self.Y * other.Y + self.Z * other.Z end
function Vector3Meta:Cross(other)
    return Vector3Meta.new(
        self.Y * other.Z - self.Z * other.Y,
        self.Z * other.X - self.X * other.Z,
        self.X * other.Y - self.Y * other.X
    )
end
function Vector3Meta:Lerp(other, alpha)
    return Vector3Meta.new(
        self.X + (other.X - self.X) * alpha,
        self.Y + (other.Y - self.Y) * alpha,
        self.Z + (other.Z - self.Z) * alpha
    )
end

Vector3Meta.__properties_magnitude = function(self)
    return math.sqrt(self.X * self.X + self.Y * self.Y + self.Z * self.Z)
end
-- Magnitude/Unit are exposed as computed fields via a secondary __index
-- layered under the method table (Lua only fires __index once, so
-- Magnitude/Unit are resolved specially inside a wrapping __index below).
local Vector3Real = Vector3Meta
local vector3_index = Vector3Meta.__index
Vector3Meta.__index = function(t, key)
    if key == "Magnitude" then return Vector3Real.__properties_magnitude(t) end
    if key == "Unit" then
        local m = Vector3Real.__properties_magnitude(t)
        if m == 0 then error("cannot normalize a zero-length Vector3", 2) end
        return Vector3Meta.new(t.X / m, t.Y / m, t.Z / m)
    end
    return vector3_index[key]
end

_G.Vector3 = locked_proxy(Vector3Meta)

-- ---------------- Color3 ----------------
local Color3Meta = {}
Color3Meta.__index = Color3Meta
Color3Meta.__metatable = "locked"

local function clamp01(n, name)
    if type(n) ~= "number" or n ~= n then error("Color3 component '" .. name .. "' must be a number", 3) end
    if n < 0 then return 0 end
    if n > 1 then return 1 end
    return n
end

function Color3Meta.new(r, g, b)
    return setmetatable({R = clamp01(r or 0, "R"), G = clamp01(g or 0, "G"), B = clamp01(b or 0, "B")}, Color3Meta)
end

function Color3Meta.fromRGB(r, g, b)
    return Color3Meta.new((r or 0) / 255, (g or 0) / 255, (b or 0) / 255)
end

Color3Meta.__eq = function(a, b) return a.R == b.R and a.G == b.G and a.B == b.B end
Color3Meta.__tostring = function(a) return string.format("%g, %g, %g", a.R, a.G, a.B) end

function Color3Meta:Lerp(other, alpha)
    return Color3Meta.new(
        self.R + (other.R - self.R) * alpha,
        self.G + (other.G - self.G) * alpha,
        self.B + (other.B - self.B) * alpha
    )
end

_G.Color3 = locked_proxy(Color3Meta)

-- ---------------- Instance proxy ----------------
local InstanceMeta = {}
InstanceMeta.__metatable = "locked"

local proxy_cache = setmetatable({}, {__mode = "v"})

local function make_proxy(id)
    if id == nil then return nil end
    local cached = proxy_cache[id]
    if cached ~= nil then return cached end
    local proxy = setmetatable({__id = id}, InstanceMeta)
    proxy_cache[id] = proxy
    return proxy
end
_G.__make_proxy = make_proxy

local function unwrap_value(value)
    -- Convert Vector3/Color3 Lua values into plain ARRAY tables (in a
    -- fixed, explicit order) before they cross into Python, so the Python
    -- side never needs to know about Lua metatables. Must use
    -- debug.getmetatable() here, not the public getmetatable() -- the
    -- public one is exactly what __metatable protection blocks (see
    -- typeof() above), so it always returns the string "locked" here and
    -- silently fails to match either branch. That first version of this
    -- function shipped with that exact bug: it fell through to `return
    -- value` (the raw, un-unwrapped Vector3 table), and a Python-side
    -- fallback that called list(value.values()) on it happened to still
    -- "work" most of the time by accident -- except Lua's iteration order
    -- for string-keyed tables is unspecified, so X/Y/Z (or R/G/B) came
    -- through in a silently SHUFFLED order. Caught by an integration test
    -- asserting an exact Y value after `part.Position = part.Position +
    -- Vector3.new(0, 1, 0)`.
    if type(value) == "table" then
        local mt = debug.getmetatable(value)
        if mt == Vector3Real then return {value.X, value.Y, value.Z} end
        if mt == Color3Meta then return {value.R, value.G, value.B} end
    end
    return value
end

local function wrap_value(kind, raw)
    if raw == nil then return nil end
    if kind == "vector3" then return Vector3Meta.new(raw[1], raw[2], raw[3]) end
    if kind == "color3" then return Color3Meta.new(raw[1], raw[2], raw[3]) end
    if kind == "instance" then return make_proxy(raw) end
    return raw
end

InstanceMeta.__index = function(self, key)
    local id = rawget(self, "__id")
    local method = __bridge_get_method(key)
    if method ~= nil then
        return method
    end
    local kind, raw = __bridge_get(id, key)
    if kind == "error" then error(raw, 2) end
    return wrap_value(kind, raw)
end

InstanceMeta.__newindex = function(self, key, value)
    local id = rawget(self, "__id")
    local ok, err = __bridge_set(id, key, unwrap_value(value))
    if not ok then error(err, 2) end
end

InstanceMeta.__tostring = function(self)
    local kind, raw = __bridge_get(rawget(self, "__id"), "Name")
    return type(raw) == "string" and raw or "<destroyed Instance>"
end

-- Fixed method table, resolved before falling back to a property read so
-- a Part named "FindFirstChild" can never shadow the real method.
local InstanceMethods = {}

function InstanceMethods.FindFirstChild(self, name)
    local childId = __bridge_find_first_child(rawget(self, "__id"), name)
    return make_proxy(childId)
end

function InstanceMethods.WaitForChild(self, name, timeout)
    local id = rawget(self, "__id")
    local childId = __bridge_find_first_child(id, name)
    if childId ~= nil then return make_proxy(childId) end
    local deadline = nil
    if timeout ~= nil then deadline = __bridge_now() + timeout end
    local resultId = coroutine.yield({kind = "wait_for_child", parent_id = id, name = name, deadline = deadline})
    return make_proxy(resultId)
end

function InstanceMethods.GetChildren(self)
    local ids = __bridge_get_children(rawget(self, "__id"))
    local result = {}
    for i = 1, #ids do result[i] = make_proxy(ids[i]) end
    return result
end

function InstanceMethods.GetDescendants(self)
    local ids = __bridge_get_descendants(rawget(self, "__id"))
    local result = {}
    for i = 1, #ids do result[i] = make_proxy(ids[i]) end
    return result
end

function InstanceMethods.IsA(self, className)
    return __bridge_is_a(rawget(self, "__id"), className)
end

function InstanceMethods.GetFullName(self)
    return __bridge_get_full_name(rawget(self, "__id"))
end

function InstanceMethods.Destroy(self)
    __bridge_destroy(rawget(self, "__id"))
end

-- Stage 3.5: game:GetService(name) -- only meaningful on the "game"
-- proxy (matching Roblox's DataModel:GetService()); the bridge itself
-- rejects any other __id with the same "not a valid member" error style
-- get_property() already uses elsewhere in this prelude. Returns a
-- locked, cached service proxy table -- never a raw Python object -- see
-- lua_gameplay_api.py, which is the only thing that ever populates
-- what __bridge_get_service resolves.
function InstanceMethods.GetService(self, name)
    local ok, result = __bridge_get_service(rawget(self, "__id"), name)
    if not ok then error(result, 2) end
    return result
end

_G.__bridge_get_method = function(name) return InstanceMethods[name] end

-- ---------------- Instance.new / typeof / game / workspace / script ----------------
local InstanceStatic = {}
function InstanceStatic.new(className, parent)
    local parentId = nil
    if parent ~= nil then parentId = rawget(parent, "__id") end
    local ok, idOrErr = __bridge_instance_new(className, parentId)
    if not ok then error(idOrErr, 2) end
    return make_proxy(idOrErr)
end
_G.Instance = locked_proxy(InstanceStatic)

-- getmetatable() is intentionally blind to __metatable-protected tables
-- (that is the whole point of the protection) -- typeof() needs the REAL
-- metatable to tell Vector3/Color3/Instance apart, so it uses
-- debug.getmetatable() here, inside the prelude's own privileged scope,
-- where `debug` still exists. User Source never receives `debug` (see
-- LuaRuntimeManager._build_sandbox_env) and so can never do this itself.
_G.typeof = function(value)
    if type(value) == "table" then
        local mt = debug.getmetatable(value)
        if mt == Vector3Real then return "Vector3" end
        if mt == Color3Meta then return "Color3" end
        if mt == InstanceMeta then return "Instance" end
    end
    return type(value)
end

-- ---------------- coroutine registry ----------------
-- Coroutines are never handed to Python directly: a Lua thread value that
-- round-trips through Python and back loses its LUA_TTHREAD identity as
-- far as debug.sethook()/coroutine.resume() are concerned (verified
-- interactively -- debug.sethook(co, ...) raised "bad argument #2 to
-- 'sethook' (string expected, got function)" once `co` had been through
-- Python, because Lua no longer recognized it as a thread and silently
-- reinterpreted the argument list under sethook's other overload).
-- Python instead holds a small integer id; every operation goes through
-- these registry functions, which resolve the id back to the real Lua
-- thread value entirely within Lua.
local coroutine_registry = {}
local next_co_id = 0

_G.__registry_create = function(fn)
    next_co_id = next_co_id + 1
    coroutine_registry[next_co_id] = coroutine.create(fn)
    return next_co_id
end

_G.__registry_resume = function(id, ...)
    local co = coroutine_registry[id]
    if co == nil then return false, "coroutine no longer exists" end
    return coroutine.resume(co, ...)
end

_G.__registry_status = function(id)
    local co = coroutine_registry[id]
    if co == nil then return "dead" end
    return coroutine.status(co)
end

_G.__registry_install_hook = function(id, cb, n)
    local co = coroutine_registry[id]
    if co == nil then return end
    local function __t() cb() end
    debug.sethook(co, __t, "", n)
end

_G.__registry_close = function(id)
    local co = coroutine_registry[id]
    coroutine_registry[id] = nil
    if co ~= nil and coroutine.status(co) ~= "dead" then
        pcall(coroutine.close, co)
    end
end

-- ---------------- task scheduler bridge ----------------
local task = {}

function task.wait(seconds)
    return coroutine.yield({kind = "wait_seconds", seconds = seconds or 0})
end

function task.spawn(fn, ...)
    local id = __registry_create(fn)
    __bridge_schedule_immediate(id, {...})
    return id
end

function task.defer(fn, ...)
    local id = __registry_create(fn)
    __bridge_schedule_deferred(id, {...})
    return id
end

function task.delay(seconds, fn, ...)
    local id = __registry_create(fn)
    __bridge_schedule_delayed(id, seconds or 0, {...})
    return id
end

_G.task = locked_proxy(task)
_G.wait = task.wait

-- ---------------- require ----------------
_G.require = function(moduleProxy)
    if typeof(moduleProxy) ~= "Instance" then
        error("invalid argument #1 to 'require' (ModuleScript expected)", 2)
    end
    local id = rawget(moduleProxy, "__id")
    local ok, result = __bridge_require(id)
    if not ok then error(result, 2) end
    return result
end

_G._SSTUDIO_API_VERSION = "__API_VERSION__"
"""


def _build_prelude_source() -> str:
    return _PRELUDE_LUA.replace("__API_VERSION__", API_VERSION)


# ============================================================
# RUNTIME SCENE LAYER -- the ONLY thing Lua property writes touch.
# Never reads/writes MultiplayerGame.instances[...].properties and never
# calls apply_property_edit()/request_*() -- see module docstring.
# ============================================================

_MUTABLE_PART_KEYS = ("Position", "Rotation", "Size", "Anchored", "CanCollide", "Color", "Transparency")
_TRANSFORM_KEYS = ("Position", "Rotation", "Size", "Anchored", "CanCollide")


class LuaInstanceError(Exception):
    """Carries a Roblox-style message string straight back to Lua as a
    normal (pcall-catchable) error -- never a Python traceback."""


@dataclass
class _RuntimeCreated:
    id: str
    class_name: str
    parent_id: Optional[str]
    name: str
    properties: dict[str, Any]
    entity: Any = None


class RuntimeSceneLayer:
    """Owns everything a running script can see or touch: a property
    overlay for existing authoritative instances (Position/Rotation/Size/
    Anchored/CanCollide/Color/Transparency/Name), the runtime-created
    instance registry, and runtime deletions. Restores every touched
    authoritative Entity from a Play-start snapshot on stop() -- modeled
    directly on the accepted _physics_snapshot/_start_physics/
    _stop_physics pattern, just covering the wider property set Lua can
    reach (that pattern only ever needed Position/Rotation/Size)."""

    def __init__(self, game: Any) -> None:
        self.game = game
        self._overlay: dict[str, dict[str, Any]] = {}
        self._name_overlay: dict[str, str] = {}
        self._deleted: set[str] = set()
        self._runtime: dict[str, _RuntimeCreated] = {}
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._next_runtime_index = 0
        self._physics = None  # bound in start()

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        from shared import object_registry  # local import: keeps this module importable headlessly without the editor's full Qt/Ursina stack pre-loaded

        self._overlay.clear()
        self._name_overlay.clear()
        self._deleted.clear()
        self._runtime.clear()
        self._snapshot.clear()
        self._next_runtime_index = 0
        self._physics = self.game._physics_world

        for instance_id, record in self.game.instances.items():
            definition = object_registry.get_object_type(record.class_name)
            if definition is None or not definition.has_3d_entity:
                continue
            properties = record.properties
            self._snapshot[instance_id] = {
                "Position": [float(v) for v in properties.get("Position", [0.0, 0.0, 0.0])],
                "Rotation": [float(v) for v in properties.get("Rotation", [0.0, 0.0, 0.0])],
                "Size": [float(v) for v in properties.get("Size", [1.0, 1.0, 1.0])],
                "Color": [int(v) for v in properties.get("Color", [255, 255, 255])],
                "Transparency": float(properties.get("Transparency", 0.0)),
            }

    def stop(self) -> None:
        """Restore every authoritative entity this session could have
        touched (Position/Rotation/Size/Color/Transparency/enabled) from
        the Play-start snapshot, destroy every runtime-created entity/
        body, discard all overlay state. Deliberately self-sufficient --
        it does NOT rely on the caller's separate _stop_physics() (which
        also restores Position/Rotation/Size as part of its own,
        pre-existing responsibility) running afterward; the two are
        redundant on purpose rather than this module silently depending
        on a specific external call order for correctness."""
        from ursina import Vec3, color as ursina_color

        for instance_id, snapshot in self._snapshot.items():
            entity = self.game.parts.get(instance_id)
            if entity is None:
                continue
            # Lua-side Destroy() on an AUTHORITATIVE instance only ever
            # disables its Entity (see destroy()) -- it must never be
            # removed from game.parts, so Stop can always find and
            # restore it here, same as every other touched property.
            try:
                entity.enabled = True
            except Exception:
                pass
            try:
                pos = snapshot["Position"]; rot = snapshot["Rotation"]; size = snapshot["Size"]
                entity.position = Vec3(pos[0], pos[1], pos[2])
                entity.rotation = Vec3(rot[0], rot[1], rot[2])
                entity.scale = Vec3(size[0], size[1], size[2])
                rgb = snapshot["Color"]
                entity.color = ursina_color.rgba32(
                    int(rgb[0]), int(rgb[1]), int(rgb[2]), int(255 * (1.0 - snapshot["Transparency"])),
                )
            except (TypeError, ValueError, IndexError):
                pass

        for runtime_id, item in list(self._runtime.items()):
            if self._physics is not None:
                self._physics.remove_part(runtime_id)
            if item.entity is not None:
                try:
                    item.entity.disable()
                except Exception:
                    pass

        if self._physics is not None:
            self._physics.wake_all_dynamic()

        self._overlay.clear()
        self._name_overlay.clear()
        self._deleted.clear()
        self._runtime.clear()
        self._snapshot.clear()
        self._physics = None

    # ---------------- lookups shared by proxy + scheduler ----------------

    def _definition(self, class_name: str):
        from shared import object_registry
        return object_registry.get_object_type(class_name)

    def exists(self, instance_id: str) -> bool:
        if instance_id in ("game", "Workspace"):
            return True
        if instance_id in self._deleted:
            return False
        if instance_id in self._runtime:
            return True
        return instance_id in self.game.instances

    def class_name_of(self, instance_id: str) -> Optional[str]:
        if instance_id == "game":
            return "DataModel"
        if instance_id == "Workspace":
            return "Workspace"
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.class_name
        record = self.game.instances.get(instance_id)
        return record.class_name if record is not None else None

    def name_of(self, instance_id: str) -> Optional[str]:
        if instance_id == "game":
            return "game"
        if instance_id == "Workspace":
            return "Workspace"
        if instance_id in self._name_overlay:
            return self._name_overlay[instance_id]
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.name
        record = self.game.instances.get(instance_id)
        return record.name if record is not None else None

    def parent_of(self, instance_id: str) -> Optional[str]:
        if instance_id in ("game", "Workspace"):
            return None
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.parent_id or "Workspace"
        record = self.game.instances.get(instance_id)
        if record is None:
            return None
        return record.parent_id or "Workspace"

    def children_of(self, instance_id: str) -> list[str]:
        result: list[str] = []
        for record in self.game.instances.values():
            if record.id in self._deleted:
                continue
            if (record.parent_id or "Workspace") == instance_id:
                result.append(record.id)
        for item in self._runtime.values():
            if item.id in self._deleted:
                continue
            if (item.parent_id or "Workspace") == instance_id:
                result.append(item.id)
        return result

    def descendants_of(self, instance_id: str) -> list[str]:
        result: list[str] = []
        frontier = [instance_id]
        while frontier:
            current = frontier.pop()
            children = self.children_of(current)
            result.extend(children)
            frontier.extend(children)
        return result

    def find_first_child(self, instance_id: str, name: str) -> Optional[str]:
        for child_id in self.children_of(instance_id):
            if self.name_of(child_id) == name:
                return child_id
        return None

    def full_name_of(self, instance_id: str) -> str:
        """Stage 3.6 fix: this used to hardcode "Workspace" as the only
        recognized root, so a script under any OTHER pseudo-root (e.g.
        ServerScriptService, StarterPlayer) hit name_of() returning None on
        that root string (it isn't a real Instance) and silently fell back
        to a path ending in ".Workspace" regardless of its real location.
        Now every shared.object_registry.ROOT_SERVICES string is a
        recognized terminal segment, not just "Workspace"."""
        from shared.object_registry import ROOT_SERVICES
        parts: list[str] = []
        current: Optional[str] = instance_id
        seen: set[str] = set()
        while current is not None and current not in ROOT_SERVICES and current not in seen:
            seen.add(current)
            name = self.name_of(current)
            if name is None:
                break
            parts.append(name)
            current = self.parent_of(current)
        parts.append(current if current in ROOT_SERVICES else "Workspace")
        return ".".join(reversed(parts))

    def is_a(self, instance_id: str, class_name: str) -> bool:
        actual = self.class_name_of(instance_id)
        if actual is None:
            return False
        if actual == class_name:
            return True
        # Minimal single-level inheritance: Part-like types answer IsA("Part") consistent with existing PART_LIKE handling.
        if class_name == "Part" and actual == "SpawnPoint":
            return True
        return False

    # ---------------- property get/set (the __bridge_get/__bridge_set backends) ----------------

    def get_property(self, instance_id: str, key: str) -> tuple[str, Any]:
        if instance_id == "game":
            if key == "Workspace":
                return "instance", "Workspace"
            return "error", f"'{key}' is not a valid member of DataModel"
        if instance_id == "Workspace":
            if key == "Name" or key == "ClassName":
                return "string", "Workspace"
            if key == "Parent":
                return "nil", None
            return "error", f"'{key}' is not a valid member of Workspace"

        if not self.exists(instance_id):
            return "error", "attempt to use a destroyed Instance"

        class_name = self.class_name_of(instance_id)
        if key == "Name":
            return "string", self.name_of(instance_id)
        if key == "ClassName":
            return "string", class_name
        if key == "Parent":
            parent_id = self.parent_of(instance_id)
            return ("nil", None) if parent_id is None else ("instance", parent_id)

        definition = self._definition(class_name) if class_name else None
        if definition is None or not definition.has_3d_entity:
            return "error", f"'{key}' is not a valid member of {class_name}"

        overlay = self._overlay.get(instance_id, {})
        base = self._base_properties(instance_id)
        if key in ("Position", "Rotation"):
            value = overlay.get(key, base.get(key, [0.0, 0.0, 0.0]))
            return "vector3", [float(v) for v in value]
        if key == "Size":
            value = overlay.get(key, base.get(key, [1.0, 1.0, 1.0]))
            return "vector3", [float(v) for v in value]
        if key == "Color":
            value = overlay.get(key, base.get(key, [255, 255, 255]))
            return "color3", [float(c) / 255.0 for c in value[:3]]
        if key == "Transparency":
            return "number", float(overlay.get(key, base.get(key, 0.0)))
        if key in ("Anchored", "CanCollide"):
            default = True if key == "Anchored" else True
            return "bool", bool(overlay.get(key, base.get(key, default)))
        return "error", f"'{key}' is not a valid member of {class_name}"

    def _base_properties(self, instance_id: str) -> dict[str, Any]:
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.properties
        record = self.game.instances.get(instance_id)
        return record.properties if record is not None else {}

    def set_property(self, instance_id: str, key: str, value: Any) -> tuple[bool, Optional[str]]:
        if instance_id in ("game", "Workspace"):
            return False, f"'{key}' cannot be assigned to (read-only)"
        if not self.exists(instance_id):
            return False, "attempt to use a destroyed Instance"

        if key == "Name":
            if not isinstance(value, str):
                return False, "Name must be a string"
            self._name_overlay[instance_id] = value
            return True, None
        if key in ("ClassName", "Parent"):
            return False, f"'{key}' cannot be assigned to in this API version"

        class_name = self.class_name_of(instance_id)
        definition = self._definition(class_name) if class_name else None
        if definition is None or not definition.has_3d_entity:
            return False, f"'{key}' is not a valid member of {class_name}"

        try:
            if key in ("Position", "Rotation", "Size"):
                vec = [float(value[0]), float(value[1]), float(value[2])]
                for component in vec:
                    if component != component or component in (float("inf"), float("-inf")):
                        return False, f"{key} components must be finite numbers"
                if key == "Size":
                    from shared.instance import MIN_PART_SIZE
                    vec = [max(MIN_PART_SIZE, v) for v in vec]
                self._overlay.setdefault(instance_id, {})[key] = vec
            elif key == "Color":
                rgb = [max(0, min(255, int(round(float(c) * 255.0)))) for c in value[:3]]
                self._overlay.setdefault(instance_id, {})[key] = rgb
            elif key == "Transparency":
                self._overlay.setdefault(instance_id, {})[key] = max(0.0, min(1.0, float(value)))
            elif key in ("Anchored", "CanCollide"):
                self._overlay.setdefault(instance_id, {})[key] = bool(value)
            else:
                return False, f"'{key}' is not a valid member of {class_name}"
        except (TypeError, ValueError, IndexError):
            return False, f"invalid value for {key}"

        self._apply_visual(instance_id)
        if key in _TRANSFORM_KEYS:
            self._rebuild_physics(instance_id)
        return True, None

    def _merged_properties(self, instance_id: str) -> dict[str, Any]:
        merged = dict(self._base_properties(instance_id))
        merged.update(self._overlay.get(instance_id, {}))
        return merged

    def _entity_for(self, instance_id: str):
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.entity
        return self.game.parts.get(instance_id)

    def _apply_visual(self, instance_id: str) -> None:
        from ursina import Vec3, color as ursina_color

        entity = self._entity_for(instance_id)
        if entity is None:
            return
        merged = self._merged_properties(instance_id)
        try:
            position = merged.get("Position", [0.0, 0.0, 0.0])
            entity.position = Vec3(float(position[0]), float(position[1]), float(position[2]))
            rotation = merged.get("Rotation", [0.0, 0.0, 0.0])
            entity.rotation = Vec3(float(rotation[0]), float(rotation[1]), float(rotation[2]))
            size = merged.get("Size", [1.0, 1.0, 1.0])
            entity.scale = Vec3(float(size[0]), float(size[1]), float(size[2]))
            rgb = merged.get("Color", [255, 255, 255])
            transparency = float(merged.get("Transparency", 0.0))
            entity.color = ursina_color.rgba32(
                int(rgb[0]), int(rgb[1]), int(rgb[2]), int(255 * (1.0 - transparency)),
            )
        except (TypeError, ValueError, IndexError):
            pass

    def _rebuild_physics(self, instance_id: str) -> None:
        """Direct Position/Rotation/Size/Anchored/CanCollide assignment
        teleports and resets velocity (documented Stage 3.0 policy) --
        implemented as remove+re-add, which is also exactly correct when
        Anchored/CanCollide changes require a different body kind
        (static/dynamic/ghost/none)."""
        if self._physics is None:
            return
        entity = self._entity_for(instance_id)
        if entity is None:
            return
        merged = self._merged_properties(instance_id)
        had_body = self._physics.has_body(instance_id)
        if had_body:
            self._physics.remove_part(instance_id)
        self._physics.add_part(
            instance_id,
            entity,
            merged.get("Position", [0.0, 0.0, 0.0]),
            merged.get("Rotation", [0.0, 0.0, 0.0]),
            merged.get("Size", [1.0, 1.0, 1.0]),
            bool(merged.get("Anchored", True)),
            bool(merged.get("CanCollide", True)),
        )
        self._physics.wake_all_dynamic()

    # ---------------- creation / destruction ----------------

    def instance_new(self, class_name: str, parent_id: Optional[str]) -> tuple[bool, str]:
        if len(self._runtime) >= MAX_RUNTIME_INSTANCES:
            return False, f"runtime instance limit reached ({MAX_RUNTIME_INSTANCES})"
        definition = self._definition(class_name)
        if definition is None or class_name not in ("Part", "Model", "Folder"):
            return False, f"Instance.new(\"{class_name}\") is not supported in this API version"
        if parent_id is not None and not self.exists(parent_id):
            return False, "parent does not exist"

        self._next_runtime_index += 1
        runtime_id = f"{_RUNTIME_ID_PREFIX}{self._next_runtime_index}"
        default_name = class_name
        properties = dict(definition.default_properties)
        entity = None
        if definition.has_3d_entity:
            properties.setdefault("Position", [0.0, 0.0, 0.0])
            properties.setdefault("Rotation", [0.0, 0.0, 0.0])
            properties.setdefault("Size", [1.0, 1.0, 1.0])
            properties.setdefault("Color", [255, 255, 255])
            properties.setdefault("Transparency", 0.0)
            entity = self.game._build_part_entity(properties)

        item = _RuntimeCreated(
            id=runtime_id, class_name=class_name, parent_id=parent_id or "Workspace",
            name=default_name, properties=properties, entity=entity,
        )
        self._runtime[runtime_id] = item
        if entity is not None and definition.has_3d_entity:
            self.game.parts[runtime_id] = entity
            if self._physics is not None:
                self._physics.add_part(
                    runtime_id, entity, properties["Position"], properties["Rotation"], properties["Size"],
                    bool(properties.get("Anchored", True)), bool(properties.get("CanCollide", True)),
                )
        _debug(f"Instance.new({class_name!r}) -> {runtime_id}")
        return True, runtime_id

    def destroy(self, instance_id: str) -> None:
        if instance_id in ("game", "Workspace") or instance_id in self._deleted:
            return
        self._deleted.add(instance_id)
        item = self._runtime.pop(instance_id, None)
        had_body = self._physics is not None and self._physics.has_body(instance_id)
        if self._physics is not None and had_body:
            self._physics.remove_part(instance_id)
        if item is not None:
            self.game.parts.pop(instance_id, None)
            if item.entity is not None:
                try:
                    item.entity.disable()
                except Exception:
                    pass
        else:
            # Authoritative instance destroyed only for the remainder of
            # this Play session -- its Entity is hidden, never actually
            # removed from game.parts (Stop must be able to find and
            # restore it); its physics body is dropped so nothing keeps
            # resting on a Lua-vanished Part.
            entity = self.game.parts.get(instance_id)
            if entity is not None:
                try:
                    entity.enabled = False
                except Exception:
                    pass
        if self._physics is not None and had_body:
            self._physics.wake_all_dynamic()


# ============================================================
# TASK SCHEDULER -- cooperative, coroutine-based. One Lua coroutine per
# task, never one OS thread per task (see module docstring on the
# per-coroutine debug.sethook requirement -- install_hook() below is what
# makes every one of these coroutines interruptible).
# ============================================================

@dataclass
class _ScheduledEntry:
    co_id: int
    owner_script_id: str
    kind: str  # "wait_seconds" | "wait_for_child" | "deferred" | "signal_wait"
    args: tuple = ()
    deadline: Optional[float] = None
    parent_id: Optional[str] = None
    child_name: Optional[str] = None
    # Stage 3.5: LuaGameplayContext's LuaSignal:Wait() support -- see
    # mark_signal_fired()/the "signal_wait" branches in _resume()/update()
    # below. Not used by anything from Stage 3.0-3.4.
    signal_id: Optional[int] = None
    started: bool = False


class LuaTaskScheduler:
    """Coroutines are tracked by a small integer id (see the Lua-side
    coroutine_registry in the prelude) rather than a raw coroutine
    reference -- a Lua thread value that round-trips through Python and
    back loses its LUA_TTHREAD identity as far as debug.sethook()/
    coroutine.resume() are concerned (verified interactively)."""

    def __init__(self, manager: "LuaRuntimeManager") -> None:
        self.manager = manager
        self._pending: list[_ScheduledEntry] = []
        self._deferred: list[_ScheduledEntry] = []
        self._task_counts: dict[str, int] = {}
        self.current_script_id: Optional[str] = None
        # Stage 3.5: signal_id -> args tuple, populated by
        # mark_signal_fired() (called by LuaGameplayContext right before it
        # dispatches :Connect() listeners for the same firing), consumed by
        # update()'s "signal_wait" branch. A plain dict is sufficient since
        # a signal fires at most meaningfully-once per frame for any given
        # :Wait() caller's purposes -- if it fires again before a waiter is
        # polled, the newer args simply win, matching "the next time this
        # event fires" semantics of a real Wait().
        self._fired_signal_values: dict[int, tuple] = {}

    def reset(self) -> None:
        for entry in self._pending + self._deferred:
            self._close(entry.co_id)
        self._pending.clear()
        self._deferred.clear()
        self._task_counts.clear()
        self._fired_signal_values.clear()
        self.current_script_id = None

    def cancel_owner(self, script_id: str) -> None:
        for bucket in (self._pending, self._deferred):
            remaining = []
            for entry in bucket:
                if entry.owner_script_id == script_id:
                    self._close(entry.co_id)
                else:
                    remaining.append(entry)
            bucket[:] = remaining
        self._task_counts.pop(script_id, None)

    def _close(self, co_id: int) -> None:
        try:
            self.manager.lua.globals()["__registry_close"](co_id)
        except Exception:
            pass

    def _count_task(self, script_id: str) -> bool:
        count = self._task_counts.get(script_id, 0) + 1
        if count > MAX_QUEUED_TASKS_PER_SCRIPT:
            self.manager._report_error(script_id, f"too many queued tasks (limit {MAX_QUEUED_TASKS_PER_SCRIPT})")
            return False
        self._task_counts[script_id] = count
        return True

    # ---------------- entry points called from the Lua "task" bridge ----------------

    @staticmethod
    def _payload_field(payload: Any, key: str) -> Any:
        """Subscript access, never attribute access -- a lupa Lua table's
        `.get`/`.items` attribute lookups resolve as ordinary Lua field
        lookups (returning nil/None for an absent key) rather than a
        Python dict's bound methods, so `hasattr(payload, 'get')` is
        always true and `payload.get` is `None`, not a callable (found via
        a real TypeError: 'NoneType' object is not callable while testing
        task.wait's yielded descriptor table)."""
        if payload is None:
            return None
        try:
            return payload[key]
        except (TypeError, KeyError, IndexError):
            return None

    @staticmethod
    def _args_tuple(args_table: Any) -> tuple:
        if args_table is None:
            return ()
        try:
            return tuple(args_table.values())
        except AttributeError:
            return tuple(args_table)

    def start_script(self, script_id: str, co_id: int) -> None:
        entry = _ScheduledEntry(co_id=co_id, owner_script_id=script_id, kind="wait_seconds", deadline=0.0)
        self._pending.append(entry)

    def schedule_immediate(self, co_id: int, args_table: Any = None) -> None:
        owner = self.current_script_id
        if owner is None or not self._count_task(owner):
            return
        entry = _ScheduledEntry(
            co_id=co_id, owner_script_id=owner, kind="wait_seconds", deadline=0.0,
            args=self._args_tuple(args_table),
        )
        self._resume(entry)

    def schedule_immediate_external(self, owner_script_id: str, co_id: int, args: tuple = ()) -> None:
        """Stage 3.5: same as schedule_immediate(), but for callers OUTSIDE
        any script's own execution context -- self.current_script_id is
        only meaningful while _resume() is actively resuming a coroutine
        (see its docstring), which is never the case when
        LuaGameplayContext fires a signal from a native Python event
        (character spawned, a key was pressed, ...). Every listener gets
        its own coroutine/co_id (already created in Lua by
        __gameplay_fire_signal before this is called) and its own fresh
        instruction budget via the normal _resume() path -- this does not
        bypass any of the existing per-resume protections, it only
        supplies the owner attribution schedule_immediate() would
        otherwise read from current_script_id."""
        if not self._count_task(owner_script_id):
            return
        entry = _ScheduledEntry(co_id=co_id, owner_script_id=owner_script_id, kind="wait_seconds", deadline=0.0, args=tuple(args))
        self._resume(entry)

    def mark_signal_fired(self, signal_id: int, args: tuple) -> None:
        """Stage 3.5: called by LuaGameplayContext immediately before it
        asks Lua to dispatch a signal's :Connect() listeners -- makes the
        fired value available to any coroutine currently parked in a
        "signal_wait" pending entry for this signal_id (see update()'s
        matching branch below), independent of whether that signal has
        any :Connect() listeners at all."""
        self._fired_signal_values[signal_id] = tuple(args)

    def schedule_deferred(self, co_id: int, args_table: Any = None) -> None:
        owner = self.current_script_id
        if owner is None or not self._count_task(owner):
            return
        self._deferred.append(_ScheduledEntry(
            co_id=co_id, owner_script_id=owner, kind="deferred", args=self._args_tuple(args_table),
        ))

    def schedule_delayed(self, co_id: int, seconds: float, args_table: Any = None) -> None:
        owner = self.current_script_id
        if owner is None or not self._count_task(owner):
            return
        deadline = _time.monotonic() + max(0.0, float(seconds))
        self._pending.append(_ScheduledEntry(
            co_id=co_id, owner_script_id=owner, kind="wait_seconds", deadline=deadline,
            args=self._args_tuple(args_table),
        ))

    # ---------------- per-frame update ----------------

    def update(self) -> None:
        frame_start = _time.monotonic()
        due_deferred, self._deferred = self._deferred, []
        for entry in due_deferred:
            self._resume(entry)
            if _time.monotonic() - frame_start > MAX_FRAME_WALL_TIME:
                self._deferred.extend(due_deferred[due_deferred.index(entry) + 1:])
                break

        now = _time.monotonic()
        still_pending: list[_ScheduledEntry] = []
        for entry in self._pending:
            if _time.monotonic() - frame_start > MAX_FRAME_WALL_TIME:
                still_pending.append(entry)
                continue
            if entry.kind == "wait_seconds":
                if now >= (entry.deadline or 0.0):
                    self._resume(entry)
                else:
                    still_pending.append(entry)
            elif entry.kind == "wait_for_child":
                child_id = self.manager.scene.find_first_child(entry.parent_id, entry.child_name)
                if child_id is not None:
                    self._resume(entry, resume_value=child_id)
                elif entry.deadline is not None and now >= entry.deadline:
                    self._resume(entry, resume_value=None)
                else:
                    still_pending.append(entry)
            elif entry.kind == "signal_wait":
                # Stage 3.5: see mark_signal_fired()'s docstring.
                fired = self._fired_signal_values.pop(entry.signal_id, None) if entry.signal_id is not None else None
                if fired is not None:
                    self._resume(entry, resume_value=fired)
                else:
                    still_pending.append(entry)
            else:
                still_pending.append(entry)
        self._pending = still_pending

    def _resume(self, entry: _ScheduledEntry, resume_value: Any = None) -> None:
        self.current_script_id = entry.owner_script_id
        self.manager._install_hook(entry.co_id)  # fresh per-resume budget -- see _install_hook docstring
        try:
            resume = self.manager.lua.globals()["__registry_resume"]
            if entry.started:
                # Stage 3.5: a "signal_wait" resume value is a tuple of
                # every argument the signal fired with (LuaSignal:Wait()
                # can return multiple values, e.g. a Character proxy) --
                # unpacked into the real Lua call so coroutine.yield(...)
                # gets them all back, not a single Lua table. Every
                # pre-existing resume_value (wait_for_child's child_id,
                # wait_seconds' None) is never a tuple, so this is a no-op
                # for every call site that predates Stage 3.5.
                if isinstance(resume_value, tuple):
                    result = resume(entry.co_id, *resume_value)
                else:
                    result = resume(entry.co_id, resume_value)
            else:
                entry.started = True
                result = resume(entry.co_id, *entry.args)
        except Exception as exc:  # pragma: no cover -- lupa call machinery itself failing
            self.manager._report_error(entry.owner_script_id, f"scheduler error: {exc}")
            self.current_script_id = None
            return
        self.current_script_id = None

        if isinstance(result, tuple):
            ok = result[0]
            payload = result[1] if len(result) > 1 else None
        else:
            ok, payload = result, None

        if not ok:
            self.manager._report_error(entry.owner_script_id, str(payload))
            return

        status = self.manager.lua.globals()["__registry_status"](entry.co_id)
        if status == "dead":
            return  # task finished normally, nothing left to reschedule

        kind = self._payload_field(payload, "kind")
        if kind == "wait_seconds":
            seconds = self._payload_field(payload, "seconds") or 0
            self._pending.append(_ScheduledEntry(
                co_id=entry.co_id, owner_script_id=entry.owner_script_id,
                kind="wait_seconds", deadline=_time.monotonic() + float(seconds), started=True,
            ))
            return
        if kind == "wait_for_child":
            self._pending.append(_ScheduledEntry(
                co_id=entry.co_id, owner_script_id=entry.owner_script_id,
                kind="wait_for_child", parent_id=self._payload_field(payload, "parent_id"),
                child_name=self._payload_field(payload, "name"),
                deadline=self._payload_field(payload, "deadline"), started=True,
            ))
            return
        if kind == "signal_wait":
            # Stage 3.5: LuaSignal:Wait() -- see mark_signal_fired()'s
            # docstring and the matching branch in update() above. No
            # deadline/timeout: a Wait() with nothing ever firing it parks
            # here for the rest of the Play session, same as a real
            # RBXScriptSignal:Wait() with no firer.
            self._pending.append(_ScheduledEntry(
                co_id=entry.co_id, owner_script_id=entry.owner_script_id,
                kind="signal_wait", signal_id=self._payload_field(payload, "signal_id"), started=True,
            ))
            return
        # Unrecognized yield (e.g. a bare coroutine.yield() with no
        # descriptor) -- treat as "resume again next frame" rather than
        # silently dropping the task.
        self._pending.append(_ScheduledEntry(
            co_id=entry.co_id, owner_script_id=entry.owner_script_id,
            kind="wait_seconds", deadline=0.0, started=True,
        ))


# ============================================================
# SCRIPT EXECUTION PLAN -- Stage 3.6.
#
# Replaces the Stage 3.0-3.5 Workspace-only BFS (_discover_run_order, now
# removed) with explicit, named execution roots per class:
#   Script      auto-runs from Workspace, ServerScriptService.
#   LocalScript auto-runs from Workspace, StarterPlayer.
#   ModuleScript NEVER auto-runs (require()-only, unchanged from Stage 3.0).
#
# StarterPlayerScripts is intentionally NOT introduced here: it does not
# exist anywhere in shared/object_registry.py's ROOT_SERVICES, in any of
# the 12 templates, or in any allowed_parent_types list today. Inventing a
# new pseudo-container purely for this stage would be schema surface with
# no editor-side support (Insert Object, default_parent, drag-reparent
# validation) behind it. StarterPlayer itself already covers the "a
# LocalScript conventionally lives under StarterPlayer" case end-to-end
# (it's already LocalScript's schema default_parent). A dedicated
# StarterPlayerScripts container is a clean future extension: add it to
# ROOT_SERVICES + LocalScript.allowed_parent_types, then add its literal
# string to _LOCALSCRIPT_ROOTS below -- nothing else in this module would
# need to change.
#
# Still no true server/client VM split: Script and LocalScript both run in
# the SAME local Play VM, same sandbox, same scheduler budget -- only
# their *discovery* is root-aware now. See LuaGameplayContext's module
# docstring for the matching UserInputService-is-really-LocalScript-only
# caveat this shares.
# ============================================================

_SCRIPT_ROOTS: tuple[str, ...] = ("Workspace", "ServerScriptService")
_LOCALSCRIPT_ROOTS: tuple[str, ...] = ("Workspace", "StarterPlayer")
# Fixed walk order -- this is what makes overall execution order
# deterministic across repeated Play sessions: every Workspace-rooted
# script (Script and LocalScript interleaved in natural parent-before-
# child, id-tiebroken BFS order) runs first, then every ServerScriptService
# script, then every StarterPlayer LocalScript. Walking "Workspace" only
# ONCE (not once per class) is also what makes overlap-safety free: a
# Script and a LocalScript can never both claim the same instance id (an
# instance has exactly one class_name), and each of these three literal
# root strings is only ever walked a single time.
_EXECUTION_ROOTS: tuple[str, ...] = ("Workspace", "ServerScriptService", "StarterPlayer")

# Names game:GetService() will hand back as a plain generic Instance proxy
# (see bridge_get_service below) when no gameplay-specific service already
# claimed them. Deliberately every shared.object_registry.ROOT_SERVICES
# entry EXCEPT "Players" -- Players must stay gameplay-mediated-only so it
# keeps failing cleanly when no gameplay layer is attached, instead of
# silently handing back a useless memberless container.
_GENERIC_CONTAINER_SERVICES: frozenset[str] = frozenset(
    {"Workspace", "StarterPlayer", "StarterGui", "ReplicatedStorage", "ServerScriptService", "ServerStorage"}
)


@dataclass(frozen=True)
class ScriptExecutionEntry:
    """One Script/LocalScript that WILL be started this Play session, in
    the exact order build_script_execution_plan() decided. order_key is
    redundant with list position (entries are already returned in run
    order) but kept explicit so tests/diagnostics can assert on it without
    depending on list identity."""
    instance_id: str
    class_name: str  # "Script" | "LocalScript"
    root_service: str  # which _EXECUTION_ROOTS entry this was discovered under
    hierarchy_path: str
    order_key: int


@dataclass(frozen=True)
class SkippedScriptInfo:
    """One ENABLED Script/LocalScript that exists in the authoritative
    hierarchy but is not reachable from any root valid for its class --
    reported once as an Output warning, never executed. ModuleScript is
    never represented here (see build_script_execution_plan)."""
    instance_id: str
    class_name: str
    hierarchy_path: str
    actual_root: str  # best-effort: the real top-level container it sits under


def _find_top_level_root(instance_id: str, instances: dict[str, Any]) -> str:
    """Walk parent_id upward from instance_id until a known ROOT_SERVICES
    string is reached, for skipped-script diagnostics only. Defensive
    depth cap + visited-set guard against a corrupted/cyclic parent chain
    (see build_script_execution_plan's docstring for why an ordinary,
    root-reachable cycle cannot actually occur given the single-parent_id
    model -- this handles the pathological case of a chain that was
    corrupted into a cycle disconnected from every known root)."""
    from shared.object_registry import ROOT_SERVICES
    visited: set[str] = set()
    current_id = instance_id
    for _ in range(10_000):
        record = instances.get(current_id)
        if record is None:
            return "Workspace"
        parent = record.parent_id or "Workspace"
        if parent in ROOT_SERVICES:
            return parent
        if parent in visited:
            return "Unknown"
        visited.add(parent)
        current_id = parent
    return "Unknown"


def build_script_execution_plan(
    instances: dict[str, Any],
    path_of: Optional[Callable[[str], str]] = None,
) -> tuple[list[ScriptExecutionEntry], list[SkippedScriptInfo]]:
    """Pure function, no Lua/Qt/Ursina dependency -- takes the same
    `dict[str, InstanceRecord]` shape as MultiplayerGame.instances (or a
    plain test double with .id/.class_name/.parent_id/.enabled) and
    returns (entries to start in order, skipped-but-enabled scripts to
    warn about). Never mutates `instances`.

    Cycle safety: each instance has exactly ONE parent_id value, so it can
    only ever appear in exactly one children_by_parent bucket -- a node
    reachable from a fixed root string therefore has a unique, finite path
    from that root (revisiting would require the same id to be a child of
    two different already-visited parents simultaneously, which the data
    model cannot represent). The per-root `visited_this_root` set below is
    still kept as an explicit, cheap guard rather than relying on that
    proof alone.
    """
    if path_of is None:
        path_of = lambda instance_id: instance_id  # noqa: E731 -- trivial test fallback

    children_by_parent: dict[str, list[str]] = {}
    for record in instances.values():
        children_by_parent.setdefault(record.parent_id or "Workspace", []).append(record.id)
    for ids in children_by_parent.values():
        ids.sort()

    entries: list[ScriptExecutionEntry] = []
    seen: set[str] = set()
    order_key = 0

    for root in _EXECUTION_ROOTS:
        frontier: list[str] = list(children_by_parent.get(root, []))
        visited_this_root: set[str] = set()
        while frontier:
            current_id = frontier.pop(0)
            if current_id in visited_this_root:
                continue
            visited_this_root.add(current_id)
            record = instances.get(current_id)
            if record is None:
                continue
            if current_id not in seen:
                if record.class_name == "Script" and record.enabled and root in _SCRIPT_ROOTS:
                    entries.append(ScriptExecutionEntry(current_id, "Script", root, path_of(current_id), order_key))
                    seen.add(current_id)
                    order_key += 1
                elif record.class_name == "LocalScript" and record.enabled and root in _LOCALSCRIPT_ROOTS:
                    entries.append(ScriptExecutionEntry(current_id, "LocalScript", root, path_of(current_id), order_key))
                    seen.add(current_id)
                    order_key += 1
            frontier.extend(children_by_parent.get(current_id, []))

    skipped: list[SkippedScriptInfo] = []
    for record in instances.values():
        if record.class_name not in ("Script", "LocalScript"):
            continue  # ModuleScript never auto-runs and never gets a skipped warning either
        if not record.enabled or record.id in seen:
            continue
        skipped.append(SkippedScriptInfo(
            record.id, record.class_name, path_of(record.id), _find_top_level_root(record.id, instances),
        ))

    return entries, skipped


# ============================================================
# LUA RUNTIME MANAGER -- top-level orchestrator, one per Play session.
# ============================================================

_SAFE_GLOBAL_NAMES = (
    "assert", "error", "ipairs", "pairs", "next", "pcall", "xpcall", "select",
    "tonumber", "tostring", "type", "math", "string", "table", "utf8",
)


class LuaRuntimeManager:
    """Owned by MultiplayerGame as `self._lua_runtime` (created fresh on
    every Play, destroyed on every Stop -- see module docstring and the
    Stage 2.4 precedent this mirrors for PhysicsWorld)."""

    def __init__(self, game: Any) -> None:
        self.game = game
        self.scene = RuntimeSceneLayer(game)
        self.scheduler = LuaTaskScheduler(self)
        self.lua: Any = None
        self.diagnostics: list[ScriptDiagnostic] = []
        self.session_id = 0
        self._diagnostic_listeners: list[Callable[[ScriptDiagnostic], None]] = []
        self._module_cache: dict[str, Any] = {}
        self._module_in_progress: set[str] = set()
        self._script_names: dict[str, str] = {}
        # Stage 3.6: the execution plan from the most recent start(), kept
        # for introspection/tests -- see build_script_execution_plan().
        self._execution_plan: list[ScriptExecutionEntry] = []
        self._print_count = 0
        self._print_window_start = 0.0
        self._suppressed_this_window = 0
        self._active = False
        # Stage 3.5: set by client_studio.py right after constructing a
        # LuaGameplayContext for this same Play session (see
        # lua_gameplay_api.py) -- None whenever no gameplay layer is
        # attached (e.g. a headless test that only exercises Script/
        # LocalScript execution). __bridge_get_service (registered below)
        # is the only thing that ever reads this; nothing in this class
        # otherwise knows or cares that the gameplay layer exists.
        self.gameplay: Any = None

    # ---------------- lifecycle ----------------

    def add_diagnostic_listener(self, callback: Callable[[ScriptDiagnostic], None]) -> None:
        """Stage 3.1 hook: the code editor's gutter markers/Output
        navigation need the SAME diagnostics Stage 3.0 already logs as
        formatted strings, just structured (script_id + line) instead of
        parsed back out of text. Additive -- the existing
        studio_adapter.log() formatted-string path is untouched."""
        self._diagnostic_listeners.append(callback)

    def _notify_diagnostic(self, diag: ScriptDiagnostic) -> None:
        for callback in self._diagnostic_listeners:
            try:
                callback(diag)
            except Exception:
                _debug(f"diagnostic listener raised: {_traceback.format_exc()}")

    def start(self) -> list[ScriptDiagnostic]:
        self.session_id = next(_SESSION_ID_COUNTER)
        self.diagnostics = []
        self._module_cache.clear()
        self._module_in_progress.clear()
        self._script_names.clear()
        self._print_count = 0
        self._print_window_start = _time.monotonic()
        self._suppressed_this_window = 0

        self.scene.start()
        self.lua = lua54.LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=True)
        try:
            self.lua.set_max_memory(MAX_VM_MEMORY_BYTES)
        except Exception:
            _debug("lua.set_max_memory unavailable in this lupa build; memory limit not enforced")

        self._register_bridge_functions()
        self.lua.execute(_build_prelude_source())
        self._active = True

        # Stage 3.6: explicit multi-root execution plan replaces the old
        # Workspace-only BFS. This still runs entirely BEFORE
        # LuaGameplayContext.start() is even constructed (see
        # client_studio.py's _start_lua()) -- but that's fine: start_script()
        # only ENQUEUES each script's top-level coroutine via
        # scheduler.start_script() (a deadline=0.0 pending entry), it does
        # not resume it. The first time any script body actually executes
        # is the first update_lua() tick next frame, by which point
        # _start_lua() has already fully returned -- gameplay context
        # construction included. game:GetService(...) is therefore already
        # live before a single line of Script/LocalScript source runs.
        self._execution_plan, skipped = build_script_execution_plan(self.game.instances, self.scene.full_name_of)
        for skip in skipped:
            self._report_error(
                "<engine>",
                f'{skip.class_name} "{skip.hierarchy_path}" (id={skip.instance_id}) was skipped: '
                f'{skip.class_name} does not auto-run from {skip.actual_root}.',
                severity="warning",
            )
        for entry in self._execution_plan:
            self._start_script(entry.instance_id)

        _debug(f"start(): {len(self._execution_plan)} script(s) launched, {len(skipped)} skipped, {len(self.diagnostics)} diagnostic(s) so far")
        return list(self.diagnostics)

    def update(self, dt: float) -> None:
        if not self._active or self.lua is None:
            return
        self.scheduler.update()

    def stop(self) -> None:
        """Must complete before the caller's physics/editor Stop-restore
        runs -- see module docstring. Cancels every task, clears the
        module cache, restores/cleans up the scene layer, then drops the
        Lua state so a fresh one is built on the next Play."""
        if not self._active:
            return
        self._active = False
        self.scheduler.reset()
        self._module_cache.clear()
        self._module_in_progress.clear()
        self.scene.stop()
        self.lua = None
        _debug("stop(): runtime torn down")

    # ---------------- script discovery / startup ----------------
    # See build_script_execution_plan() above (Stage 3.6) for the actual
    # discovery algorithm; start() calls it directly and stores the result
    # in self._execution_plan for introspection/tests.

    def _start_script(self, script_id: str) -> None:
        record = self.game.instances.get(script_id)
        if record is None:
            return
        self._script_names[script_id] = record.name
        source = str(record.properties.get("Source", ""))
        chunk_name = self.scene.full_name_of(script_id)
        fn, err = self._compile(source, chunk_name, script_id)
        if fn is None:
            self._report_error(script_id, err or "compile failed", chunk_name=chunk_name)
            return
        co_id = self.lua.globals()["__registry_create"](fn)
        self.scheduler.start_script(script_id, co_id)

    def _compile(self, source: str, chunk_name: str, script_id: str) -> tuple[Any, Optional[str]]:
        if len(source) > MAX_SOURCE_SIZE:
            return None, f"Source exceeds the {MAX_SOURCE_SIZE}-byte limit"
        env = self._build_sandbox_env(script_id)
        loader = self.lua.globals().load
        result = loader(source, chunk_name, "t", env)
        if isinstance(result, tuple):
            fn, err = result[0], (result[1] if len(result) > 1 else None)
        else:
            fn, err = result, None
        if fn is None or fn is False:
            return None, str(err) if err is not None else "compile failed"
        return fn, None

    def _build_sandbox_env(self, script_id: str) -> Any:
        env = self.lua.table_from({})
        g = self.lua.globals()
        for name in _SAFE_GLOBAL_NAMES:
            env[name] = g[name]
        env["Vector3"] = g["Vector3"]
        env["Color3"] = g["Color3"]
        env["Instance"] = g["Instance"]
        env["typeof"] = g["typeof"]
        env["task"] = g["task"]
        env["wait"] = g["wait"]
        env["require"] = g["require"]
        env["_SSTUDIO_API_VERSION"] = g["_SSTUDIO_API_VERSION"]
        env["game"] = self.lua.eval("__make_proxy")("game")
        env["workspace"] = self.lua.eval("__make_proxy")("Workspace")
        env["script"] = self.lua.eval("__make_proxy")(script_id)
        env["print"] = self._make_print(script_id)
        env["warn"] = self._make_warn(script_id)
        env["_G"] = env  # scripts see their OWN sandbox as _G, never the host's real _G
        return env

    # ---------------- output ----------------

    def _lua_tostring(self, value: Any) -> str:
        try:
            return str(self.lua.globals().tostring(value))
        except Exception:
            return str(value)

    def _rate_limited(self) -> bool:
        now = _time.monotonic()
        if now - self._print_window_start >= 1.0:
            if self._suppressed_this_window:
                self._report_error("<engine>", f"{self._suppressed_this_window} Output message(s) suppressed (rate limit)", severity="warning")
            self._print_window_start = now
            self._print_count = 0
            self._suppressed_this_window = 0
        self._print_count += 1
        if self._print_count > MAX_PRINT_LINES_PER_SECOND:
            self._suppressed_this_window += 1
            return True
        return False

    def _make_print(self, script_id: str) -> Callable[..., None]:
        def _print(*args: Any) -> None:
            if self._rate_limited():
                return
            message = "\t".join(self._lua_tostring(a) for a in args)
            self._report_info(script_id, message)
        return _print

    def _make_warn(self, script_id: str) -> Callable[..., None]:
        def _warn(*args: Any) -> None:
            if self._rate_limited():
                return
            message = "\t".join(self._lua_tostring(a) for a in args)
            self._report_error(script_id, message, severity="warning")
        return _warn

    def _script_name(self, script_id: str) -> str:
        return self._script_names.get(script_id, script_id)

    def _log(self, level: str, message: str) -> None:
        adapter = getattr(self.game, "studio_adapter", None)
        if adapter is not None:
            adapter.log(level, message)
        else:
            print(f"[{level.upper()}] {message}")

    def _report_info(self, script_id: str, message: str) -> None:
        diag = ScriptDiagnostic(script_id, self._script_name(script_id), "info", message, session_id=self.session_id)
        self.diagnostics.append(diag)
        self._log("info", diag.format())
        _debug(diag.format())
        self._notify_diagnostic(diag)

    def _report_error(self, script_id: str, message: str, *, severity: str = "error", line: Optional[int] = None, chunk_name: str = "") -> None:
        if line is None:
            line = _extract_lua_error_line(message)
        diag = ScriptDiagnostic(
            script_id, self._script_name(script_id), severity, message, line=line,
            chunk_name=chunk_name or self._script_name(script_id), session_id=self.session_id,
        )
        self.diagnostics.append(diag)
        self._log(severity if severity != "info" else "info", diag.format())
        self._notify_diagnostic(diag)

    # ---------------- module require ----------------

    def _require(self, module_id: str) -> tuple[bool, Any]:
        if module_id in self._module_cache:
            return True, self._module_cache[module_id]
        if module_id in self._module_in_progress:
            return False, f"circular require detected involving {self.scene.full_name_of(module_id)}"
        record = self.game.instances.get(module_id)
        if record is None or record.class_name != "ModuleScript":
            return False, "require() target is not a ModuleScript"

        self._module_in_progress.add(module_id)
        try:
            source = str(record.properties.get("Source", ""))
            chunk_name = self.scene.full_name_of(module_id)
            fn, err = self._compile(source, chunk_name, module_id)
            if fn is None:
                return False, err or "module compile failed"
            try:
                result = fn()
            except Exception as exc:
                return False, f"{chunk_name}: {exc}"
            if isinstance(result, tuple):
                result = result[0] if result else None
            self._module_cache[module_id] = result
            return True, result
        finally:
            self._module_in_progress.discard(module_id)

    # ---------------- bridge function registration ----------------

    def _install_hook(self, co_id: int) -> None:
        """Re-arms a fresh MAX_INSTRUCTIONS_PER_RESUME instruction-count
        hook on the coroutine registered under `co_id`. Must be called
        immediately before EVERY resume, not just once at coroutine
        creation: debug.sethook's count applies to the coroutine's
        cumulative instruction count from when the hook was (re-)
        installed, not a per-resume counter reset automatically by
        yield/resume. Installing once at creation would silently turn
        this into a LIFETIME budget across all of a coroutine's resumes
        (e.g. a legitimate `while part do task.wait(0.1) end` loop would
        eventually be killed just for running for a while) instead of the
        intended per-resume budget -- see LuaTaskScheduler._resume(),
        which is the only call site."""
        self.lua.globals()["__registry_install_hook"](co_id, self._on_budget_exceeded, MAX_INSTRUCTIONS_PER_RESUME)

    @staticmethod
    def _on_budget_exceeded() -> None:
        raise LuaBudgetExceeded(f"script exceeded its execution budget ({MAX_INSTRUCTIONS_PER_RESUME} instructions)")

    def _register_bridge_functions(self) -> None:
        g = self.lua.globals()
        scene = self.scene

        def bridge_get(instance_id: Any, key: Any) -> tuple:
            kind, value = scene.get_property(str(instance_id), str(key))
            if kind in ("vector3", "color3"):
                return kind, self.lua.table_from([float(v) for v in value])
            return kind, value

        def bridge_set(instance_id: Any, key: Any, value: Any) -> tuple:
            if key in ("Position", "Rotation", "Size", "Color"):
                value = _lua_array_to_list(value)
            return scene.set_property(str(instance_id), str(key), value)

        def bridge_find_first_child(instance_id: Any, name: Any) -> Optional[str]:
            return scene.find_first_child(str(instance_id), str(name))

        def bridge_get_children(instance_id: Any):
            return self.lua.table_from(scene.children_of(str(instance_id)))

        def bridge_get_descendants(instance_id: Any):
            return self.lua.table_from(scene.descendants_of(str(instance_id)))

        def bridge_is_a(instance_id: Any, class_name: Any) -> bool:
            return scene.is_a(str(instance_id), str(class_name))

        def bridge_get_full_name(instance_id: Any) -> str:
            return scene.full_name_of(str(instance_id))

        def bridge_destroy(instance_id: Any) -> None:
            script_id = str(instance_id)
            if self.game.instances.get(script_id) is not None and \
               self.game.instances[script_id].class_name in ("Script", "LocalScript"):
                self.scheduler.cancel_owner(script_id)
            scene.destroy(script_id)

        def bridge_instance_new(class_name: Any, parent_id: Any) -> tuple:
            if len(self.scene._runtime) > MAX_RUNTIME_INSTANCES:
                return False, "runtime instance limit reached"
            return scene.instance_new(str(class_name), str(parent_id) if parent_id is not None else None)

        def bridge_now() -> float:
            return _time.monotonic()

        def bridge_require(module_id: Any) -> tuple:
            return self._require(str(module_id))

        # Stage 3.5: game:GetService(name) -- only "game" itself may be
        # asked for a service (matches get_property()'s own "game"/
        # "Workspace" special-casing above); everything else, including
        # "no gameplay layer attached at all" (e.g. a headless Script-only
        # test), fails the exact same way Roblox's own GetService() does
        # for an unrecognized name -- a clean, catchable Lua error, never
        # a Python exception.
        #
        # Stage 3.6 addition: the gameplay-mediated names (currently just
        # "Players"/"UserInputService") are tried FIRST and, if attached,
        # always win. Any OTHER name in shared.object_registry.ROOT_SERVICES
        # except "Players" itself now falls back to a plain generic
        # Instance proxy for that pseudo-root -- this is what makes
        # `require(ReplicatedStorage:FindFirstChild("Foo"))` (and the
        # equivalent for ServerScriptService/StarterPlayer/Workspace/
        # StarterGui/ServerStorage) actually reachable from real script
        # code, not just true "in theory" because _require() itself never
        # cared about location. This is generic object-model plumbing
        # (the same FindFirstChild/GetChildren machinery every other
        # Instance proxy already has), not a new gameplay API -- "Players"
        # is deliberately excluded here so it keeps failing cleanly
        # (instead of returning a useless memberless container) whenever
        # no gameplay layer is attached, exactly as before.
        def bridge_get_service(instance_id: Any, name: Any) -> tuple:
            if str(instance_id) != "game":
                return False, "GetService is only callable on 'game'"
            name = str(name)
            if self.gameplay is not None:
                ok, result = self.gameplay.get_service(name)
                if ok:
                    return True, result
            if name in _GENERIC_CONTAINER_SERVICES:
                return True, self.lua.eval("__make_proxy")(name)
            return False, f"\"{name}\" is not a valid service"

        # Stage 3.5: lets LuaSignal:Connect() (defined in
        # lua_gameplay_api.py's own prelude fragment, not this one) tag a
        # new listener with the script that's connecting it, purely by
        # reading the SAME current_script_id the scheduler already
        # maintains for task.spawn/defer/delay attribution -- see
        # LuaTaskScheduler.schedule_immediate_external()'s docstring for
        # why this can't just be looked up from Lua's own `script` local.
        def bridge_current_script_id() -> Optional[str]:
            return self.scheduler.current_script_id

        g["__bridge_get"] = bridge_get
        g["__bridge_set"] = bridge_set
        g["__bridge_find_first_child"] = bridge_find_first_child
        g["__bridge_get_children"] = bridge_get_children
        g["__bridge_get_descendants"] = bridge_get_descendants
        g["__bridge_is_a"] = bridge_is_a
        g["__bridge_get_full_name"] = bridge_get_full_name
        g["__bridge_destroy"] = bridge_destroy
        g["__bridge_instance_new"] = bridge_instance_new
        g["__bridge_now"] = bridge_now
        g["__bridge_require"] = bridge_require
        g["__bridge_get_service"] = bridge_get_service
        g["__bridge_current_script_id"] = bridge_current_script_id
        g["__bridge_schedule_immediate"] = self.scheduler.schedule_immediate
        g["__bridge_schedule_deferred"] = self.scheduler.schedule_deferred
        g["__bridge_schedule_delayed"] = self.scheduler.schedule_delayed
