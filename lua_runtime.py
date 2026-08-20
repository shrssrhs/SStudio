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

-- ---------------- Signal / Connection ----------------
-- Stage 3.9: relocated here from lua_gameplay_api.py's GAMEPLAY_PRELUDE
-- (Stage 3.5 originally introduced it as "gameplay-only" plumbing, prefixed
-- __gameplay_*) -- Touched (this module) and Players/Character/
-- UserInputService (lua_gameplay_api.py) now both need the exact same
-- generic Connect/Once/Wait/Disconnect machinery, so it lives at the core
-- level, named __signal_* (no "gameplay" in the name -- it isn't
-- gameplay-specific), and both callers share ONE implementation. Nothing
-- about the mechanism itself changed from Stage 3.5's version.
local SignalMeta = {}
SignalMeta.__index = SignalMeta
SignalMeta.__metatable = "locked"
SignalMeta.__tostring = function(self) return "Signal" end

local ConnectionMeta = {}
ConnectionMeta.__index = ConnectionMeta
ConnectionMeta.__metatable = "locked"
ConnectionMeta.__tostring = function(self)
    if rawget(self, "__connected") then return "Connection (connected)" end
    return "Connection (disconnected)"
end

local signal_registry = {}
local next_signal_id = 0

local function new_signal()
    next_signal_id = next_signal_id + 1
    signal_registry[next_signal_id] = {listeners = {}, next_conn_id = 0}
    return next_signal_id
end
_G.__signal_new = new_signal

local signal_proxy_cache = setmetatable({}, {__mode = "v"})
local function make_signal_proxy(signal_id)
    if signal_id == nil then return nil end
    local cached = signal_proxy_cache[signal_id]
    if cached ~= nil then return cached end
    local proxy = setmetatable({__signal_id = signal_id}, SignalMeta)
    signal_proxy_cache[signal_id] = proxy
    return proxy
end
_G.__signal_make_proxy = make_signal_proxy

function SignalMeta:Connect(fn)
    if type(fn) ~= "function" then error("Signal:Connect expects a function", 2) end
    local signal_id = rawget(self, "__signal_id")
    local record = signal_registry[signal_id]
    if record == nil then error("Signal is no longer available", 2) end
    record.next_conn_id = record.next_conn_id + 1
    local conn_id = record.next_conn_id
    -- `cancelled` is a SEPARATE flag from "still present in
    -- record.listeners" -- see __signal_fire's docstring for why
    -- both are needed: a `once` listener is proactively removed from
    -- record.listeners the moment IT gets dispatched (so a re-entrant
    -- firing of the same signal can never double-invoke it), which would
    -- otherwise be indistinguishable from "disconnected by something
    -- else" if presence-in-the-table were the only signal checked at
    -- actual run time.
    record.listeners[conn_id] = {fn = fn, once = false, owner = __bridge_current_script_id(), cancelled = false}
    return setmetatable({__signal_id = signal_id, __conn_id = conn_id, __connected = true}, ConnectionMeta)
end

function SignalMeta:Once(fn)
    local conn = self:Connect(fn)
    local record = signal_registry[rawget(self, "__signal_id")]
    if record ~= nil then
        local entry = record.listeners[rawget(conn, "__conn_id")]
        if entry ~= nil then entry.once = true end
    end
    return conn
end

function SignalMeta:Wait()
    local signal_id = rawget(self, "__signal_id")
    return coroutine.yield({kind = "signal_wait", signal_id = signal_id})
end

function ConnectionMeta:Disconnect()
    if not rawget(self, "__connected") then return end
    rawset(self, "__connected", false)
    local record = signal_registry[rawget(self, "__signal_id")]
    if record ~= nil then
        local conn_id = rawget(self, "__conn_id")
        local entry = record.listeners[conn_id]
        -- Mark the entry cancelled BEFORE removing it from the live
        -- table: a listener already snapshotted (and already turned into
        -- a coroutine) by an in-progress __signal_fire call for
        -- THIS SAME firing still holds a direct reference to this exact
        -- `entry` table via its wrapper closure below -- removing it from
        -- record.listeners alone would be invisible to that closure,
        -- since it never re-reads record.listeners itself. `cancelled`
        -- travels with the entry object regardless of table membership,
        -- which is exactly what lets a listener disconnected by an
        -- EARLIER listener in the same dispatch correctly never run.
        if entry ~= nil then entry.cancelled = true end
        record.listeners[conn_id] = nil
    end
end

-- Fire: snapshot listeners into a separate array BEFORE dispatching any of
-- them (safe against Connect()/Disconnect() happening mid-dispatch from
-- inside a listener -- a live `pairs(record.listeners)` iteration would be
-- undefined behavior if the table it's iterating gets a key added or
-- removed during the loop; iterating a frozen snapshot array instead
-- sidesteps that entirely). `once` listeners are removed from the live
-- table before dispatch, not after, so re-entrant firing (a listener that
-- fires the same signal again) can never invoke them twice. Each
-- dispatched listener is wrapped in a small closure that re-checks
-- entry.cancelled AT ACTUAL RUN TIME (not just here, at snapshot/creation
-- time) -- necessary because every listener's coroutine is CREATED
-- up-front, in this one synchronous loop, before ANY of them have
-- actually RUN yet (see module docstring: dispatch/resume always happens
-- back in Python, through the scheduler); a listener that disconnects a
-- LATER listener in the same firing (e.g. "A disconnects B" where B's
-- coroutine was already created before A ever ran) would otherwise still
-- fire B, since B's snapshot/creation-time liveness check happened before
-- A got a chance to run at all.
_G.__signal_fire = function(signal_id, args)
    local record = signal_registry[signal_id]
    if record == nil then return {} end
    local snapshot = {}
    for conn_id, entry in pairs(record.listeners) do
        snapshot[#snapshot + 1] = {conn_id = conn_id, entry = entry}
    end
    local dispatch = {}
    for i = 1, #snapshot do
        local conn_id = snapshot[i].conn_id
        local entry = snapshot[i].entry
        if not entry.cancelled then
            if entry.once then record.listeners[conn_id] = nil end
            local real_fn = entry.fn
            local wrapped_fn = function(...)
                if entry.cancelled then return end
                return real_fn(...)
            end
            local co_id = __registry_create(wrapped_fn)
            dispatch[#dispatch + 1] = {co_id = co_id, owner = entry.owner}
        end
    end
    return dispatch
end

_G.__signal_clear_all = function()
    signal_registry = {}
    next_signal_id = 0
    signal_proxy_cache = setmetatable({}, {__mode = "v"})
end

-- Stage 3.9 lifecycle fix: per-item cleanup, not just the wholesale
-- __signal_clear_all() above (only ever called on Stop). Called from
-- bridge_destroy() (Python) whenever an Instance owning this signal (see
-- RuntimeSceneLayer._instance_signals) is Destroy()'d -- drops the WHOLE
-- record (its listeners table and every listener closure with it) so
-- nothing keeps them reachable/alive for the rest of the session just
-- because the destroyed instance's signal_id happened to still be a key
-- in this table.
_G.__signal_destroy = function(signal_id)
    signal_registry[signal_id] = nil
    signal_proxy_cache[signal_id] = nil
end

-- Stage 3.9 lifecycle fix: the counterpart for the CONNECTING side --
-- when a Script/LocalScript itself is Destroy()'d (directly or via
-- cascade), any :Connect()/:Once() listener IT registered on some OTHER
-- (still-alive) Instance's signal must not keep running forever just
-- because nothing else ever calls Disconnect() on it. Walks every live
-- signal's listeners (Play sessions have a bounded, small number of
-- concurrently-live signals -- this is a Destroy()-time cleanup call, not
-- a per-frame hot path, so an O(signals * listeners) walk is fine here).
_G.__signal_disconnect_owner = function(owner_script_id)
    for _, record in pairs(signal_registry) do
        for conn_id, entry in pairs(record.listeners) do
            if entry.owner == owner_script_id then
                entry.cancelled = true
                record.listeners[conn_id] = nil
            end
        end
    end
end

-- Stage 3.9 lifecycle fix: read-only diagnostic/test introspection --
-- see __registry_count()'s docstring for why a real pairs() count is
-- used rather than the unreliable `#` operator. Returns (signal_count,
-- total_listener_count) so a test can assert BOTH "the signal itself was
-- pruned" and "no orphaned listener survived attached to some other
-- still-alive signal".
_G.__signal_registry_count = function()
    local signal_count = 0
    local listener_count = 0
    for _, record in pairs(signal_registry) do
        signal_count = signal_count + 1
        for _ in pairs(record.listeners) do
            listener_count = listener_count + 1
        end
    end
    return signal_count, listener_count
end

-- Stage 3.9 lifecycle fix: per-signal listener count, for a test to
-- distinguish "this specific signal's listener was pruned" from "some
-- OTHER signal's listener happened to be pruned too" -- the aggregate
-- __signal_registry_count() above can't tell those apart. Returns 0 for
-- an unknown/already-destroyed signal_id rather than erroring.
_G.__signal_listener_count = function(signal_id)
    local record = signal_registry[signal_id]
    if record == nil then return 0 end
    local n = 0
    for _ in pairs(record.listeners) do n = n + 1 end
    return n
end

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
    -- Stage 3.9: an Instance proxy assigned to a writable instance_ref
    -- property (currently only `Parent`) unwraps to its bare id string --
    -- the ONE extra case beyond Vector3/Color3 this function has ever
    -- needed, added for `part.Parent = otherInstance` support. Never
    -- reached for a Signal/Connection proxy (neither is ever the RHS of a
    -- property assignment in this API).
    if type(value) == "table" then
        local mt = debug.getmetatable(value)
        if mt == Vector3Real then return {value.X, value.Y, value.Z} end
        if mt == Color3Meta then return {value.R, value.G, value.B} end
        if mt == InstanceMeta then return rawget(value, "__id") end
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

-- Stage 3.9: instance-level signals (currently just Touched -- see
-- physics.py/lua_runtime.py's RuntimeSceneLayer for the contact-detection
-- side) are looked up BEFORE the generic property bridge, same precedence
-- as the fixed method table just below -- a Part named "Touched" can never
-- shadow the real signal, matching FindFirstChild's existing precedent.
local INSTANCE_SIGNAL_NAMES = {Touched = true}

InstanceMeta.__index = function(self, key)
    local id = rawget(self, "__id")
    local method = __bridge_get_method(key)
    if method ~= nil then
        return method
    end
    if INSTANCE_SIGNAL_NAMES[key] then
        local ok, result = __bridge_get_signal(id, key)
        if not ok then error(result, 2) end
        return make_signal_proxy(result)
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

function InstanceMethods.Clone(self)
    local ok, idOrErr = __bridge_clone(rawget(self, "__id"))
    if not ok then error(idOrErr, 2) end
    return make_proxy(idOrErr)
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

-- Stage 3.9 lifecycle fix: read-only diagnostic/test introspection --
-- lets the Python side (and its regression tests) verify these tables
-- actually shrink again after per-item cleanup, not just after a
-- wholesale Stop. `#` is unreliable on a table with integer-key gaps
-- (which these tables get once destroy()'d items are actually removed
-- rather than merely nil-marked in place, so a real `pairs()` count is
-- used instead of the `#` length operator.
_G.__registry_count = function()
    local n = 0
    for _ in pairs(coroutine_registry) do n = n + 1 end
    return n
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

# Stage 3.9: sentinel for RuntimeSceneLayer._parent_overlay meaning
# "explicitly parented to nothing" (`instance.Parent = nil`) -- distinct
# from "not present in the overlay at all" (falls through to the
# authoritative/runtime-created value). A plain Python `None` can't be
# reused for this: every OTHER parent_id in this codebase already treats
# `None`/falsy as "defaults to Workspace" (see `record.parent_id or
# "Workspace"`, repeated throughout this module and server.py) -- using
# None here too would make a nil-parented instance indistinguishable from
# one that was never touched.
_NIL_PARENT = object()

# Stage 3.9: classes a running script may call Instance.new() for --
# schema-driven (creatable=True, editor_only=False -- see object_registry.
# ObjectTypeDefinition) MINUS a small denylist of classes that are
# creatable in the EDITOR's Insert Object dialog but make no sense to hand
# out fresh from Lua: Script/LocalScript/ModuleScript (this engine's
# script-execution plan is built ONCE at Play start -- see
# build_script_execution_plan() below -- a Script created mid-session
# would never actually run, which would silently mislead a developer more
# than a clear "not supported" error) and StarterPlayerScripts (a
# server-enforced singleton per Place -- server.py's _singleton_conflict()
# has no way to see a purely-local runtime instance, so Lua creating one
# could not be safely validated against the real singleton rule).
_LUA_UNCREATABLE_CLASSES = frozenset({"Script", "LocalScript", "ModuleScript", "StarterPlayerScripts"})


def _lua_kind_for_property_spec(kind: str, value: Any) -> tuple[str, Any]:
    """Maps a shared.object_registry.PropertySpec.kind to the same
    (kind, raw) wire tuple wrap_value() in the prelude expects -- the
    generic-property read-side counterpart of sanitize_properties_for_type()."""
    if kind in ("vector3", "size3"):
        vec = value if value is not None else [0.0, 0.0, 0.0]
        return "vector3", [float(v) for v in vec]
    if kind == "color":
        rgb = value if value is not None else [255, 255, 255]
        return "color3", [float(c) / 255.0 for c in rgb[:3]]
    if kind == "bool":
        return "bool", bool(value)
    if kind in ("float", "float01"):
        return "number", float(value) if value is not None else 0.0
    if kind == "int":
        # A raw Python int (not float(...)) so lupa marshals it as a real
        # Lua 5.4 integer subtype, not a float -- IntValue.Value should
        # print/compare as "2", not "2.0".
        return "number", int(value) if value is not None else 0
    if kind == "string":
        return "string", str(value) if value is not None else ""
    return "nil", None


def _sanitize_lua_value_for_spec(spec: Any, value: Any) -> tuple[bool, Any]:
    """Validates/coerces a value written from Lua against a PropertySpec,
    returning (True, cleaned_value) or (False, error_message) -- the
    generic-property write-side counterpart of sanitize_properties_for_type(),
    reusing the exact same per-kind rules rather than inventing new ones."""
    try:
        if spec.kind in ("vector3", "size3"):
            vec = [float(value[0]), float(value[1]), float(value[2])]
            for component in vec:
                if component != component or component in (float("inf"), float("-inf")):
                    return False, "value components must be finite numbers"
            if spec.kind == "size3":
                from shared.instance import MIN_PART_SIZE
                vec = [max(MIN_PART_SIZE, v) for v in vec]
            return True, vec
        if spec.kind == "color":
            rgb = [max(0, min(255, int(round(float(c) * 255.0)))) for c in value[:3]]
            return True, rgb
        if spec.kind == "bool":
            return True, bool(value)
        if spec.kind == "float":
            return True, float(value)
        if spec.kind == "float01":
            return True, max(0.0, min(1.0, float(value)))
        if spec.kind == "int":
            return True, int(value)
        if spec.kind == "string":
            if not isinstance(value, str):
                return False, "value must be a string"
            return True, value[: spec.max_len]
    except (TypeError, ValueError, IndexError):
        return False, "invalid value"
    return False, f"unsupported property kind {spec.kind!r}"


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
        # Stage 3.8: root-service (Workspace, StarterPlayer, ...) runtime
        # property overlay -- same "session-local overlay on top of a
        # Play-start snapshot" shape self._overlay already uses for
        # ordinary Instances, just keyed by service name instead of
        # instance id. Seeded from self.game.services (the PERSISTENT,
        # editor-time values) in start(); every read/write in get_property()/
        # set_property() below goes through THIS dict, never
        # self.game.services directly -- that is what keeps a runtime Lua
        # `workspace.Gravity = X` from ever touching the persistent/saved
        # value (spec: "runtime writes do not dirty or serialize the
        # Place"). Discarded wholesale in stop().
        self._service_overlay: dict[str, dict[str, Any]] = {}
        # Stage 3.9: runtime-only Parent overrides for `instance.Parent = X`
        # -- keyed by instance_id, value is either a real parent id/root-
        # service-name string, or the module-level _NIL_PARENT sentinel for
        # `instance.Parent = nil` (a real, distinct "no parent" state --
        # see parent_of()'s docstring for why this can't just be Python
        # None, which the REST of this codebase already uses to mean "no
        # explicit parent_id override, defaults to Workspace"). Never
        # touches the authoritative/networked parent_id -- purely local to
        # this Play session, discarded wholesale in stop().
        self._parent_overlay: dict[str, Any] = {}
        # Stage 3.9: per-instance built-in signal ids (currently just
        # "Touched") -- lazily created the first time a script reads
        # `part.Touched` (see LuaRuntimeManager._register_bridge_functions'
        # bridge_get_signal), looked up again by LuaRuntimeManager._poll_touched()
        # every frame to know which signal_id to fire.
        self._instance_signals: dict[str, dict[str, int]] = {}

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        from shared import object_registry  # local import: keeps this module importable headlessly without the editor's full Qt/Ursina stack pre-loaded

        self._overlay.clear()
        self._name_overlay.clear()
        self._deleted.clear()
        self._runtime.clear()
        self._snapshot.clear()
        self._next_runtime_index = 0
        self._parent_overlay.clear()
        self._instance_signals.clear()
        self._physics = self.game._physics_world
        # Stage 3.8: deep-copy so mutating the overlay (runtime Lua writes)
        # can never reach back into self.game.services (the persistent,
        # serialized dict) through a shared nested dict reference.
        self._service_overlay = {
            name: dict(properties) for name, properties in self.game.services.items()
        }

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
            # Stage 3.9 fix: also drop the disabled Entity from
            # self.game.parts, matching what destroy() already does for a
            # runtime Part destroyed mid-session (see its own
            # game.parts.pop() call). Without this, every runtime Part any
            # script ever spawned across every past Play session stayed in
            # game.parts forever as a permanently-disabled, dead Entity --
            # game.parts is a persistent dict on the editor's own game
            # object, not per-Play state, so this accumulated unboundedly
            # across repeated Play/Stop cycles for the whole process
            # lifetime, not just within one session.
            self.game.parts.pop(runtime_id, None)
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
        self._service_overlay.clear()
        self._parent_overlay.clear()
        self._instance_signals.clear()
        self._physics = None

    # ---------------- lookups shared by proxy + scheduler ----------------

    def _definition(self, class_name: str):
        from shared import object_registry
        return object_registry.get_object_type(class_name)

    def exists(self, instance_id: str) -> bool:
        from shared.object_registry import ROOT_SERVICES
        if instance_id == "game" or instance_id in ROOT_SERVICES:
            return True
        if instance_id in self._deleted:
            return False
        if instance_id in self._runtime:
            return True
        return instance_id in self.game.instances

    def class_name_of(self, instance_id: str) -> Optional[str]:
        from shared.object_registry import ROOT_SERVICES
        if instance_id == "game":
            return "DataModel"
        # Stage 3.8: generalized from a Workspace-only special case -- every
        # root service's ClassName is its own name (Workspace, StarterPlayer,
        # Lighting, ...), same as Roblox's DataModel services.
        if instance_id in ROOT_SERVICES:
            return instance_id
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.class_name
        record = self.game.instances.get(instance_id)
        return record.class_name if record is not None else None

    def name_of(self, instance_id: str) -> Optional[str]:
        from shared.object_registry import ROOT_SERVICES
        if instance_id == "game":
            return "game"
        if instance_id in ROOT_SERVICES:
            return instance_id
        if instance_id in self._name_overlay:
            return self._name_overlay[instance_id]
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.name
        record = self.game.instances.get(instance_id)
        return record.name if record is not None else None

    def parent_of(self, instance_id: str) -> Optional[str]:
        """Stage 3.9: checks self._parent_overlay FIRST -- an instance
        that has ever had `.Parent = X` assigned this Play session always
        answers from there, regardless of what its authoritative/runtime-
        created parent_id says, until Stop discards the overlay. Returns
        real Python None ONLY for "game"/a ROOT_SERVICES name (unchanged
        from before) or for an instance explicitly nil-parented via the
        overlay (see _NIL_PARENT) -- every OTHER instance always resolves
        to a real parent id/root-service string, same guarantee as before
        this stage."""
        from shared.object_registry import ROOT_SERVICES
        if instance_id == "game" or instance_id in ROOT_SERVICES:
            return None
        if instance_id in self._parent_overlay:
            override = self._parent_overlay[instance_id]
            return None if override is _NIL_PARENT else override
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.parent_id or "Workspace"
        record = self.game.instances.get(instance_id)
        if record is None:
            return None
        return record.parent_id or "Workspace"

    def children_of(self, instance_id: str) -> list[str]:
        """Stage 3.9: routed entirely through parent_of() (instead of
        reading record.parent_id/item.parent_id directly) so a runtime
        `.Parent = X` reassignment is immediately reflected here too --
        GetChildren()/FindFirstChild()/GetDescendants() all build on this
        one method."""
        result: list[str] = []
        for record in self.game.instances.values():
            if record.id in self._deleted:
                continue
            if self.parent_of(record.id) == instance_id:
                result.append(record.id)
        for item in self._runtime.values():
            if item.id in self._deleted:
                continue
            if self.parent_of(item.id) == instance_id:
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
        if current in ROOT_SERVICES:
            parts.append(current)
        # else: current is None -- the walk reached a genuinely nil-parented
        # (detached) instance, see parent_of()'s _NIL_PARENT handling. Stage
        # 3.9 fix: this used to unconditionally append "Workspace" here,
        # which made a detached instance's GetFullName() lie about it still
        # living under Workspace -- a detached instance has no root segment
        # to report, matching real Roblox (an unparented Instance's
        # GetFullName() is just its own name chain, no "Workspace." prefix).
        return ".".join(reversed(parts))

    def is_a(self, instance_id: str, class_name: str) -> bool:
        """Stage 3.9: delegates to datamodel_schema.is_a(), which walks the
        REAL registered inheritance chain (Instance -> Part -> SpawnPoint,
        ...) instead of this method's old single hand-rolled special case
        -- a future subclass registered in datamodel_schema.py (e.g. a
        Part-based class added in a later stage) automatically answers
        IsA() correctly with zero changes needed here, matching the
        "adding a future class should not require new dispatch code"
        architecture goal."""
        import datamodel_schema
        actual = self.class_name_of(instance_id)
        if actual is None:
            return False
        if actual == class_name:
            return True
        if datamodel_schema.get_class(actual) is None:
            return False
        return datamodel_schema.is_a(actual, class_name)

    # ---------------- property get/set (the __bridge_get/__bridge_set backends) ----------------

    @staticmethod
    def _lua_kind_for_value(value_type: str, value: Any) -> tuple[str, Any]:
        """Maps a datamodel_schema PropertyDescriptor.value_type + its
        current Python value to the (kind, raw) shape __bridge_get's Lua
        caller (wrap_value() in the prelude, see this module's top) already
        knows how to turn into a real Lua value."""
        if value_type == "vector3":
            return "vector3", [float(v) for v in value]
        if value_type == "color3":
            return "color3", [float(v) for v in value]
        if value_type == "bool":
            return "bool", bool(value)
        if value_type == "int":
            return "number", int(value)
        if value_type == "float":
            return "number", float(value)
        if value_type == "instance_ref":
            return ("nil", None) if value is None else ("instance", value)
        return "string", str(value)  # "string" / "enum"

    def _get_service_property(self, service_name: str, key: str) -> tuple[str, Any]:
        import datamodel_schema
        prop = datamodel_schema.get_property_descriptor(service_name, key)
        if prop is None or not prop.lua_readable:
            return "error", f"'{key}' is not a valid member of {service_name}"
        if prop.runtime_getter is not None:
            return prop.runtime_getter(self.game, service_name)
        overlay = self._service_overlay.get(service_name, {})
        value = overlay.get(key, prop.default)
        return self._lua_kind_for_value(prop.value_type, value)

    def _set_service_property(self, service_name: str, key: str, value: Any) -> tuple[bool, Optional[str]]:
        import datamodel_schema
        prop = datamodel_schema.get_property_descriptor(service_name, key)
        if prop is None or not prop.lua_writable:
            return False, f"'{key}' cannot be assigned to (read-only)"
        result = datamodel_schema.validate_property_value(service_name, key, value)
        if not result.ok:
            return False, result.error
        # Stage 3.8 fix: min<=max ordering for StarterPlayer's zoom-limit
        # pair is a CROSS-field rule (datamodel_schema.
        # validate_starter_player_zoom) that plain per-property
        # validate_property_value() above can't see -- check it here too,
        # against whatever the OTHER bound currently is in this session's
        # overlay (or its persistent default if never overlaid), so a Lua
        # write can never leave the pair inverted, same guarantee the
        # editor/server path enforces (see server.handle_update_service_property).
        if key in ("CameraMinZoomDistance", "CameraMaxZoomDistance"):
            overlay = self._service_overlay.get(service_name, {})
            other_key = "CameraMaxZoomDistance" if key == "CameraMinZoomDistance" else "CameraMinZoomDistance"
            other_default = datamodel_schema.get_property_descriptor(service_name, other_key)
            other_value = overlay.get(other_key, other_default.default if other_default is not None else None)
            min_value = result.value if key == "CameraMinZoomDistance" else other_value
            max_value = result.value if key == "CameraMaxZoomDistance" else other_value
            zoom_result = datamodel_schema.validate_starter_player_zoom(min_value, max_value)
            if not zoom_result.ok:
                return False, zoom_result.error
        self._service_overlay.setdefault(service_name, {})[key] = result.value
        self.game.apply_runtime_service_write(service_name, dict(self._service_overlay[service_name]))
        return True, None

    def get_property(self, instance_id: str, key: str) -> tuple[str, Any]:
        from shared.object_registry import ROOT_SERVICES
        if instance_id == "game":
            if key == "Workspace":
                return "instance", "Workspace"
            return "error", f"'{key}' is not a valid member of DataModel"
        if instance_id in ROOT_SERVICES:
            # Stage 3.8: generalized from a Workspace-only special case --
            # every root service answers Name/ClassName/Parent the same
            # way (Name==ClassName==its own name, Parent==nil); anything
            # else is dispatched through datamodel_schema (Workspace.
            # Gravity, StarterPlayer.CharacterWalkSpeed, ...).
            if key == "Name" or key == "ClassName":
                return "string", instance_id
            if key == "Parent":
                return "nil", None
            return self._get_service_property(instance_id, key)

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
        if definition is None:
            return "error", f"'{key}' is not a valid member of {class_name}"

        if definition.has_3d_entity:
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
            if key == "MeshId":
                # Stage 4.1 (showcase sprint): only meaningful on MeshPart,
                # but reading it on a plain Part/SpawnPoint is harmless
                # (base.get always misses -> "") rather than a special-cased
                # per-class error, matching how Color/Transparency etc. are
                # already handled uniformly across every has_3d_entity class.
                return "string", str(overlay.get(key, base.get(key, "")))
            if key == "Intensity":
                # Stage 4.1 (local lighting foundation): PointLight/SpotLight
                # only, same "harmless on other classes" reasoning as MeshId.
                return "number", float(overlay.get(key, base.get(key, 1.0)))
            if key == "Range":
                return "number", float(overlay.get(key, base.get(key, 8.0)))
            if key == "Angle":
                return "number", float(overlay.get(key, base.get(key, 45.0)))
            return "error", f"'{key}' is not a valid member of {class_name}"

        # Stage 3.9: generic schema-driven property read for classes with
        # no 3D entity (Value instances, and any future class registered
        # the same way) -- reuses the same object_registry.PropertySpec
        # metadata the editor's create/sanitize path already uses, so a
        # newly-registered class needs zero new code here.
        if key in definition.property_schema:
            spec = definition.property_schema[key]
            merged = self._merged_properties(instance_id)
            value = merged.get(key, definition.default_properties.get(key))
            return _lua_kind_for_property_spec(spec.kind, value)
        return "error", f"'{key}' is not a valid member of {class_name}"

    def _base_properties(self, instance_id: str) -> dict[str, Any]:
        item = self._runtime.get(instance_id)
        if item is not None:
            return item.properties
        record = self.game.instances.get(instance_id)
        return record.properties if record is not None else {}

    # ---------------- Parent reparenting (Stage 3.9) ----------------

    def _would_create_cycle(self, instance_id: str, new_parent_id: str) -> bool:
        """True if `new_parent_id` is `instance_id` itself or one of its
        own descendants -- walking UP from new_parent_id toward a root; if
        that walk ever reaches instance_id, parenting there would make
        instance_id its own ancestor. Bounded by a visited-set (defensive
        only -- parent_of() cannot actually produce a cycle today, same
        reasoning as _find_top_level_root's docstring)."""
        walker: Optional[str] = new_parent_id
        seen: set[str] = set()
        from shared.object_registry import ROOT_SERVICES
        while walker is not None and walker not in ROOT_SERVICES:
            if walker == instance_id:
                return True
            if walker in seen:
                return False
            seen.add(walker)
            walker = self.parent_of(walker)
        return False

    def can_set_parent(self, instance_id: str, new_parent_id: Optional[str]) -> tuple[bool, str]:
        """Every rule the spec requires runtime reparenting to still
        respect: destroyed-instance checks (caller already did the
        instance_id side via exists() in set_property()/set_parent();
        this also covers the NEW parent), self-parenting, hierarchy
        cycles, and the same shared.object_registry allowed_parent_types
        schema the editor's own drag-reparent/server SET_PARENT already
        enforce -- ONE shared source of "is this parent allowed", not a
        second copy of the rule."""
        from shared import object_registry
        from shared.object_registry import ROOT_SERVICES
        if new_parent_id is None:
            return True, ""  # `.Parent = nil` is always allowed
        if new_parent_id == instance_id:
            return False, "Cannot set Parent: an instance cannot be its own parent"
        if new_parent_id not in ROOT_SERVICES and not self.exists(new_parent_id):
            return False, "Cannot set Parent: the new parent does not exist"
        if self._would_create_cycle(instance_id, new_parent_id):
            return False, "Cannot set Parent: hierarchy cycle detected"
        class_name = self.class_name_of(instance_id)
        parent_type_id = new_parent_id if new_parent_id in ROOT_SERVICES else self.class_name_of(new_parent_id)
        if class_name is not None and not object_registry.is_parent_allowed(class_name, parent_type_id):
            return False, f"{class_name} cannot be parented to {parent_type_id or 'nil'}"
        return True, ""

    def set_parent(self, instance_id: str, value: Any) -> tuple[bool, Optional[str]]:
        """Backend for `instance.Parent = X` -- X crosses in from the
        prelude's __newindex as either a bare id string (an Instance
        proxy's __id, already unwrapped by unwrap_value()) or None (Lua
        nil). Purely local to RuntimeSceneLayer._parent_overlay -- never
        touches the authoritative/networked parent_id, matches every
        other runtime property write in this class (spec: "runtime
        gameplay mutations should remain Play-session state").

        Stage 3.9 fix: also keeps a has_3d_entity instance's real
        Entity/physics presence in sync with whether it is actually
        reachable from a root (see _is_world_attached()) -- e.g. setting
        Parent = nil now genuinely removes a Part from rendering/physics,
        and setting it back to a real location genuinely restores (or,
        for a Clone() that never had an Entity yet, lazily creates) it.
        Previously the overlay write alone was considered enough; a
        nil-parented has_3d_entity instance kept whatever Entity/physics
        presence it already had, which was harmless for ordinary
        reparenting (Workspace -> a Folder -> Workspace, all attached)
        but wrong the moment nil entered the picture at all."""
        new_parent_id: Optional[str] = None if value is None else str(value)
        ok, error = self.can_set_parent(instance_id, new_parent_id)
        if not ok:
            return False, error
        # Stage 3.9 fix: re-parenting instance_id can change every
        # DESCENDANT's effective world-attachment too, not just its own --
        # e.g. `folder.Parent = nil` must also disable/detach a Part two
        # levels below folder, even though that Part's own _parent_overlay
        # entry never changes (_is_world_attached() walks the whole chain,
        # so its result for a descendant depends on instance_id's new
        # position). Snapshot descendants + their BEFORE attachment state
        # first, since descendants_of() itself depends on parent_of(),
        # which depends on the overlay we're about to mutate.
        descendants = self.descendants_of(instance_id)
        before_states = {instance_id: self._is_world_attached(instance_id)}
        for descendant_id in descendants:
            before_states[descendant_id] = self._is_world_attached(descendant_id)
        self._parent_overlay[instance_id] = _NIL_PARENT if new_parent_id is None else new_parent_id
        for target_id, was_attached in before_states.items():
            is_attached = self._is_world_attached(target_id)
            if was_attached != is_attached:
                self._sync_world_attachment(target_id, is_attached)
        return True, None

    def _is_world_attached(self, instance_id: str) -> bool:
        """True iff instance_id's Parent chain reaches specifically
        "Workspace" without ever passing through a nil parent -- i.e. it
        is a genuine descendant of the live world scene. This is an
        SStudio architecture rule, not a copy of Roblox behavior: only
        Workspace descendants participate in rendering/physics. Every
        OTHER root service (ReplicatedStorage, ServerStorage,
        StarterPlayer, ServerScriptService, StarterGui, Players) is a
        valid, live location for an Instance to sit in -- a template
        Model waiting in ReplicatedStorage to be cloned in later, for
        example -- but never renders or simulates while it sits there.

        Stage 4.0 fix: this used to accept ANY ROOT_SERVICES member (or
        the literal "game" DataModel root), not just Workspace -- so a
        spatial Instance parented anywhere under, say, ReplicatedStorage
        got a real Entity and physics body anyway, and reparenting it
        from Workspace back into a non-Workspace service didn't detach
        it at all (both roots satisfied the old check equally). Confirmed
        via direct behavioral reproduction, not just this docstring's own
        claim -- see test_world_membership.py.

        An instance with Parent == nil, or parented under an ancestor
        that is ITSELF nil-parented (however many levels up) or sitting
        in a non-Workspace root, is not world-attached. Bounded by a
        visited-set, same defensive reasoning as _would_create_cycle()."""
        walker: Optional[str] = instance_id
        seen: set[str] = set()
        while walker is not None:
            if walker == "Workspace":
                return True
            if walker in seen:
                return False
            seen.add(walker)
            walker = self.parent_of(walker)
        return False

    def _sync_world_attachment(self, instance_id: str, attached: bool) -> None:
        """Shows/hides a has_3d_entity instance's Entity and adds/removes
        its physics body to match a Parent-chain attachment transition
        (called only from set_parent(), only when _is_world_attached()'s
        result actually changed). Mirrors destroy()'s own entity.enabled
        = False + physics removal, for a different reason (nil-parenting
        instead of :Destroy()). A runtime instance that has never had an
        Entity built yet (every fresh Clone() -- see its own docstring)
        gets one built here, lazily, the first time it actually becomes
        attached; non-has_3d_entity classes (Folder, Value instances, ...)
        are a no-op, same as they are everywhere else in this class."""
        class_name = self.class_name_of(instance_id)
        definition = self._definition(class_name) if class_name else None
        if definition is None or not definition.has_3d_entity:
            return
        entity = self._entity_for(instance_id)
        properties = self._merged_properties(instance_id)
        if attached:
            if entity is None:
                item = self._runtime.get(instance_id)
                if item is None:
                    return
                entity = self.game._build_part_entity(properties, class_name)
                if entity is None:
                    return
                item.entity = entity
                self.game.parts[instance_id] = entity
            else:
                try:
                    entity.enabled = True
                except Exception:
                    pass
            from shared.object_registry import LIGHT_CLASS_NAMES
            if (
                self._physics is not None
                and not self._physics.has_body(instance_id)
                and class_name not in LIGHT_CLASS_NAMES
            ):
                self._physics.add_part(
                    instance_id, entity, properties.get("Position", [0.0, 0.0, 0.0]),
                    properties.get("Rotation", [0.0, 0.0, 0.0]), properties.get("Size", [1.0, 1.0, 1.0]),
                    bool(properties.get("Anchored", True)), bool(properties.get("CanCollide", True)),
                )
        else:
            if entity is not None:
                try:
                    entity.enabled = False
                except Exception:
                    pass
            if self._physics is not None and self._physics.has_body(instance_id):
                self._physics.remove_part(instance_id)
                self._physics.wake_all_dynamic()

    # ---------------- Clone (Stage 3.9) ----------------

    def clone(self, instance_id: str) -> tuple[bool, str]:
        """Always returns a NEW, independent runtime-only instance (never
        the original id) -- matches Roblox's :Clone() in that respect even
        when the original itself is an AUTHORED instance.

        Stage 3.9 fix: a fresh clone now genuinely starts with Parent ==
        nil, matching real Roblox -- it used to be parented alongside the
        original immediately, documented at the time as a deliberate
        simplification, but that made a cloned Part visibly appear in the
        world (and gain a real physics body) before the developer ever
        chose to put it anywhere, which is both wrong and -- worse --
        actively misleading during manual verification (an untouched
        clone left sitting in Workspace is indistinguishable from a
        genuinely-still-alive original). Because Parent starts nil, and
        _is_world_attached()/_sync_world_attachment() (see set_parent())
        only give a has_3d_entity instance a real Entity/physics body
        once it is actually reachable from a root, NO Entity is built
        here at all -- it is built lazily, the first time the caller
        does `clone.Parent = workspace` (or any other real location)."""
        if len(self._runtime) >= MAX_RUNTIME_INSTANCES:
            return False, f"runtime instance limit reached ({MAX_RUNTIME_INSTANCES})"
        if not self.exists(instance_id):
            return False, "attempt to clone a destroyed Instance"
        class_name = self.class_name_of(instance_id)
        definition = self._definition(class_name) if class_name else None
        if definition is None:
            return False, f"cannot clone an instance of unknown class {class_name}"
        if class_name in _LUA_UNCREATABLE_CLASSES:
            # Same reasoning as instance_new()'s own denylist check (see
            # _LUA_UNCREATABLE_CLASSES' module-level comment): a cloned
            # Script/LocalScript/ModuleScript would never actually run (the
            # execution plan is built once at Play start), and a cloned
            # StarterPlayerScripts can't be validated against the real
            # server-enforced singleton rule -- silently allowing either
            # would mislead a developer more than a clear, upfront error.
            return False, f"{class_name} instances cannot be cloned at runtime"
        name = self.name_of(instance_id) or class_name

        # _merged_properties() is already fully generic (base properties +
        # self._overlay, neither of which is has_3d_entity-specific), so
        # it works unchanged for Value instances/Folders too.
        properties = dict(self._merged_properties(instance_id))

        self._next_runtime_index += 1
        runtime_id = f"{_RUNTIME_ID_PREFIX}{self._next_runtime_index}"
        item = _RuntimeCreated(
            id=runtime_id, class_name=class_name, parent_id=None,
            name=name, properties=properties, entity=None,
        )
        self._runtime[runtime_id] = item
        self._parent_overlay[runtime_id] = _NIL_PARENT
        _debug(f"Clone({instance_id!r}) -> {runtime_id} (Parent=nil)")
        return True, runtime_id

    def set_property(self, instance_id: str, key: str, value: Any) -> tuple[bool, Optional[str]]:
        from shared.object_registry import ROOT_SERVICES
        if instance_id == "game":
            return False, f"'{key}' cannot be assigned to (read-only)"
        if instance_id in ROOT_SERVICES:
            # Stage 3.8: generalized from a Workspace-only unconditional
            # read-only reject -- Name/ClassName/Parent stay read-only for
            # every service (services are never renamed/reparented), but
            # datamodel_schema-registered properties (Workspace.Gravity,
            # StarterPlayer.*) now genuinely accept writes.
            if key in ("Name", "ClassName", "Parent"):
                return False, f"'{key}' cannot be assigned to (read-only)"
            return self._set_service_property(instance_id, key, value)
        if not self.exists(instance_id):
            return False, "attempt to use a destroyed Instance"

        if key == "Name":
            if not isinstance(value, str):
                return False, "Name must be a string"
            self._name_overlay[instance_id] = value
            return True, None
        if key == "ClassName":
            return False, "ClassName cannot be assigned to"
        if key == "Parent":
            return self.set_parent(instance_id, value)

        class_name = self.class_name_of(instance_id)
        definition = self._definition(class_name) if class_name else None
        if definition is None:
            return False, f"'{key}' is not a valid member of {class_name}"

        if not definition.has_3d_entity:
            # Stage 3.9: generic schema-driven property write, the set-side
            # mirror of the get_property() fallback above -- see its
            # comment. Validation mirrors sanitize_properties_for_type()'s
            # per-kind rules in shared/object_registry.py.
            if key not in definition.property_schema:
                return False, f"'{key}' is not a valid member of {class_name}"
            spec = definition.property_schema[key]
            ok, sanitized = _sanitize_lua_value_for_spec(spec, value)
            if not ok:
                return False, sanitized
            self._overlay.setdefault(instance_id, {})[key] = sanitized
            return True, None

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
            elif key == "MeshId":
                # Stage 4.1 follow-up: a runtime Lua write here is
                # deliberately NOT rebuilt into a live mesh swap -- unlike
                # the EDITOR path (Inspector edits DO hot-swap immediately,
                # see client_studio.py's _apply_instance_properties()),
                # there is no established "Lua write should visually
                # rebuild an Entity's geometry" precedent elsewhere in this
                # class to extend, and the sprint's acceptance target is
                # the authoring workflow, not scripted mesh-swapping. The
                # written value is always correctly stored/reported back
                # (get_property reads the same overlay); only an
                # already-built Entity's geometry stays as it was. A
                # genuinely fresh MeshPart (MeshId set before Parent =
                # workspace) is unaffected -- see _build_part_entity.
                if not isinstance(value, str):
                    return False, "MeshId must be a string"
                self._overlay.setdefault(instance_id, {})[key] = value[:256]
            elif key in ("Intensity", "Range", "Angle"):
                # Stage 4.1 (local lighting foundation): PointLight/
                # SpotLight only -- reading these on a non-light class is
                # harmless (see get_property's own comment), and writing
                # them is likewise stored without a class check, matching
                # every other has_3d_entity field's uniform treatment.
                self._overlay.setdefault(instance_id, {})[key] = float(value)
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

        from shared.object_registry import LIGHT_CLASS_NAMES
        class_name = self.class_name_of(instance_id)
        if class_name in LIGHT_CLASS_NAMES:
            # Stage 4.1 (local lighting foundation): Intensity/Range/Angle
            # are light-specific (not part of the generic block above) --
            # the actual Panda3D setColor(scaled)/setAttenuation()/setFov()
            # calls live in client_studio.py (_apply_light_properties),
            # same delegation pattern MeshId hot-swapping already uses for
            # _apply_mesh_geometry, keeping Panda3D-light-API specifics out
            # of this engine-agnostic-ish module.
            try:
                self.game._apply_light_properties(entity, class_name, merged)
            except Exception:
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
        from shared.object_registry import LIGHT_CLASS_NAMES
        if self.class_name_of(instance_id) in LIGHT_CLASS_NAMES:
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
        # Stage 3.9: schema-driven instead of a hardcoded ("Part", "Model",
        # "Folder") allowlist -- any class the registry marks creatable and
        # not editor-only qualifies, MINUS _LUA_UNCREATABLE_CLASSES (see its
        # module-level comment for why those specific classes are excluded
        # even though the editor's Insert Object dialog allows them).
        if (
            definition is None
            or not definition.creatable
            or definition.editor_only
            or class_name in _LUA_UNCREATABLE_CLASSES
        ):
            return False, f"Instance.new(\"{class_name}\") is not supported in this API version"
        if parent_id is not None and not self.exists(parent_id):
            return False, "parent does not exist"

        from shared import object_registry
        from shared.object_registry import ROOT_SERVICES
        if parent_id is not None:
            parent_type_id = parent_id if parent_id in ROOT_SERVICES else self.class_name_of(parent_id)
            if not object_registry.is_parent_allowed(class_name, parent_type_id):
                return False, f"{class_name} cannot be parented to {parent_type_id or 'nil'}"

        self._next_runtime_index += 1
        runtime_id = f"{_RUNTIME_ID_PREFIX}{self._next_runtime_index}"
        default_name = class_name
        properties = dict(definition.default_properties)
        if definition.has_3d_entity:
            properties.setdefault("Position", [0.0, 0.0, 0.0])
            properties.setdefault("Rotation", [0.0, 0.0, 0.0])
            properties.setdefault("Size", [1.0, 1.0, 1.0])
            properties.setdefault("Color", [255, 255, 255])
            properties.setdefault("Transparency", 0.0)

        item = _RuntimeCreated(
            id=runtime_id, class_name=class_name, parent_id=None,
            name=default_name, properties=properties, entity=None,
        )
        self._runtime[runtime_id] = item

        if parent_id is None:
            # Stage 3.9 fix: matches Clone()'s already-fixed behavior (see
            # clone()'s docstring) -- Instance.new(className) with no
            # parent argument now genuinely starts Parent == nil instead
            # of the old eager "defaults to Workspace, immediately visible
            # and physical" behavior. No Entity/physics body is built here
            # at all; both are created lazily the first time the caller
            # does `instance.Parent = workspace` (or any other real
            # location) -- see _sync_world_attachment().
            self._parent_overlay[runtime_id] = _NIL_PARENT
        else:
            item.parent_id = parent_id
            # Stage 4.0 fix: gate the eager build on _is_world_attached()
            # (the same central Workspace-ancestry predicate set_parent()
            # uses), not merely "a parent was given". Instance.new(cls,
            # someInstanceUnderReplicatedStorage) used to build a real
            # Entity/physics body unconditionally -- harmless back when
            # _is_world_attached() itself wrongly accepted any root
            # service, but wrong now that only genuine Workspace
            # descendants are world-attached. An instance created with a
            # non-Workspace-attached parent still gets one lazily, the
            # first time it (or an ancestor) is actually reparented under
            # Workspace -- see _sync_world_attachment().
            if definition.has_3d_entity and self._is_world_attached(runtime_id):
                entity = self.game._build_part_entity(properties, class_name)
                if entity is not None:
                    item.entity = entity
                    self.game.parts[runtime_id] = entity
                    from shared.object_registry import LIGHT_CLASS_NAMES
                    if self._physics is not None and class_name not in LIGHT_CLASS_NAMES:
                        self._physics.add_part(
                            runtime_id, entity, properties["Position"], properties["Rotation"], properties["Size"],
                            bool(properties.get("Anchored", True)), bool(properties.get("CanCollide", True)),
                        )
        _debug(f"Instance.new({class_name!r}) -> {runtime_id} (parent={parent_id!r})")
        return True, runtime_id

    def destroy(self, instance_id: str) -> None:
        if instance_id in ("game", "Workspace") or instance_id in self._deleted:
            return
        # Stage 3.9 fix: Destroy() must cascade to every descendant, same
        # root cause as set_parent()'s attachment cascade above -- without
        # this, a destroyed container's children stayed exists()==True,
        # fully rendered/physical, and script-running/signal-firing for
        # the rest of the Play session; only the container itself actually
        # became unreachable via GetChildren(). Snapshot the descendant
        # list BEFORE destroying anything: descendants_of() walks
        # children_of()/parent_of(), which would stop finding a child the
        # instant its own parent is marked deleted.
        for descendant_id in self.descendants_of(instance_id):
            self.destroy(descendant_id)
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
    # Stage 3.9 lifecycle fix: True iff this entry was created through
    # _count_task() (an explicit task.spawn/defer/delay call), as opposed
    # to a script's own top-level entry from start_script() (never
    # counted against MAX_QUEUED_TASKS_PER_SCRIPT in the first place, see
    # schedule_immediate()'s docstring). Propagated unchanged across every
    # reschedule of the SAME logical task (e.g. a task.spawn'd function
    # that itself calls task.wait() gets a NEW _ScheduledEntry object with
    # the same co_id, but it's still the same counted task) -- only the
    # entry that reaches a truly terminal state (_resume() sees the
    # coroutine finish/error, or the entry is cancelled) triggers exactly
    # one _release_task() call, via this flag.
    counted: bool = False


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
                    self._release_task(entry)
                    self._close(entry.co_id)
                else:
                    remaining.append(entry)
            bucket[:] = remaining

    def cancel_signal(self, signal_id: int) -> None:
        """Stage 3.9 lifecycle fix: releases every coroutine currently
        parked in a Signal:Wait() for signal_id -- called from
        bridge_destroy() right before __signal_destroy() drops the
        registry entry those waiters were waiting on, so a Wait() on a
        just-destroyed Instance's signal doesn't sit blocked for the rest
        of the session with nothing left that could ever resume it."""
        for bucket in (self._pending, self._deferred):
            remaining = []
            for entry in bucket:
                if entry.kind == "signal_wait" and entry.signal_id == signal_id:
                    self._release_task(entry)
                    self._close(entry.co_id)
                else:
                    remaining.append(entry)
            bucket[:] = remaining

    def _close(self, co_id: int) -> None:
        try:
            self.manager.lua.globals()["__registry_close"](co_id)
        except Exception:
            pass

    def _count_task(self, script_id: str) -> bool:
        """Reserves one ACTIVE/PENDING task slot for script_id -- NOT a
        lifetime-use counter. Every task this returns True for is
        guaranteed exactly one matching _release_task() call once it
        reaches a terminal state (see _release_task()'s own docstring),
        so a script issuing many short-lived tasks one after another over
        a long session never approaches the limit; only genuinely
        simultaneously-active/pending tasks count against it."""
        count = self._task_counts.get(script_id, 0) + 1
        if count > MAX_QUEUED_TASKS_PER_SCRIPT:
            self.manager._report_error(script_id, f"too many queued tasks (limit {MAX_QUEUED_TASKS_PER_SCRIPT})")
            return False
        self._task_counts[script_id] = count
        return True

    def _release_task(self, entry: "_ScheduledEntry") -> None:
        """The exactly-once counterpart to _count_task() -- called from
        every place a counted entry reaches a terminal state: _resume()
        (normal completion, error, or the lupa-call-failure exception
        path), cancel_owner() (owner destroyed), and cancel_signal()
        (the signal it was Wait()ing on was destroyed). A no-op for an
        uncounted entry (entry.counted is False -- e.g. a script's own
        top-level entry from start_script(), which _count_task() was
        never called for in the first place). Floor-clamped at 0 and pops
        the key entirely once it reaches 0, so a stray extra release call
        can never drive the count negative."""
        if not entry.counted:
            return
        remaining = self._task_counts.get(entry.owner_script_id, 0) - 1
        if remaining <= 0:
            self._task_counts.pop(entry.owner_script_id, None)
        else:
            self._task_counts[entry.owner_script_id] = remaining

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
            args=self._args_tuple(args_table), counted=True,
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
        entry = _ScheduledEntry(co_id=co_id, owner_script_id=owner_script_id, kind="wait_seconds", deadline=0.0, args=tuple(args), counted=True)
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
            co_id=co_id, owner_script_id=owner, kind="deferred", args=self._args_tuple(args_table), counted=True,
        ))

    def schedule_delayed(self, co_id: int, seconds: float, args_table: Any = None) -> None:
        owner = self.current_script_id
        if owner is None or not self._count_task(owner):
            return
        deadline = _time.monotonic() + max(0.0, float(seconds))
        self._pending.append(_ScheduledEntry(
            co_id=co_id, owner_script_id=owner, kind="wait_seconds", deadline=deadline,
            args=self._args_tuple(args_table), counted=True,
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
        # Stage 3.9 fix: a resumed entry's script can itself call
        # cancel_owner() for a DIFFERENT entry already visited earlier in
        # THIS SAME pass (e.g. Destroy() on a container cascades to cancel
        # a nested Script's own pending task.wait()/WaitForChild()/
        # Signal:Wait()) -- cancel_owner() mutates self._pending directly,
        # but the old code built a separate still_pending list and blindly
        # overwrote self._pending with it at the end, silently UNDOING any
        # such mid-pass cancellation (the cancelled entry was already
        # copied into still_pending before its cancellation happened).
        # Snapshotting here and reconciling by identity below respects
        # both a mid-pass cancel_owner() removal AND a mid-pass _resume()
        # re-scheduling (a resumed entry that yields again pushes a new
        # entry straight onto self._pending, which must not be lost either).
        pending_snapshot = list(self._pending)
        still_pending: list[_ScheduledEntry] = []
        for entry in pending_snapshot:
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
        current_ids = {id(e) for e in self._pending}
        snapshot_ids = {id(e) for e in pending_snapshot}
        kept = [e for e in still_pending if id(e) in current_ids]
        newly_scheduled = [e for e in self._pending if id(e) not in snapshot_ids]
        self._pending = kept + newly_scheduled

    def _resume(self, entry: _ScheduledEntry, resume_value: Any = None) -> None:
        # Stage 3.9 fix: _resume() is REENTRANT. schedule_immediate()/
        # schedule_immediate_external() (task.spawn()'s and a dispatched
        # signal listener's own scheduling path) call this SYNCHRONOUSLY,
        # from within the Lua "task"/signal bridge, while an OUTER
        # coroutine may already be mid-resume (its own call to this same
        # method still on the Python call stack) -- e.g. a script's own
        # top-level coroutine calling task.spawn(fn) resumes fn's new
        # coroutine via a nested _resume() call before the outer
        # coroutine's own Lua execution ever continues.
        #
        # The old code unconditionally reset self.current_script_id to
        # None on every exit path, instead of restoring whatever it was
        # BEFORE this particular call started. For a nested call, that
        # wiped out the OUTER coroutine's own "who am I" context the
        # instant the FIRST task.spawn()'d child finished -- so every
        # task.spawn()/task.defer()/task.delay() call after the first one
        # in the same script execution read current_script_id as None and
        # silently no-op'd (schedule_immediate()'s `if owner is None:
        # return` guard swallows it -- no error, nothing scheduled).
        # From the outside this looked exactly like "a Lua for loop
        # calling task.spawn() repeatedly stops after one iteration" --
        # it didn't: the loop ran to completion every time, only the
        # FIRST task.spawn() call in it ever actually scheduled anything.
        previous_script_id = self.current_script_id
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
            self.current_script_id = previous_script_id
            self._release_task(entry)
            self._close(entry.co_id)
            return
        self.current_script_id = previous_script_id

        if isinstance(result, tuple):
            ok = result[0]
            payload = result[1] if len(result) > 1 else None
        else:
            ok, payload = result, None

        if not ok:
            self.manager._report_error(entry.owner_script_id, str(payload))
            self._release_task(entry)
            self._close(entry.co_id)
            return

        status = self.manager.lua.globals()["__registry_status"](entry.co_id)
        if status == "dead":
            # Stage 3.9 lifecycle fix: task finished normally, nothing
            # left to reschedule -- release its counted slot AND prune
            # its now-dead coroutine_registry entry (previously left
            # behind forever; see __registry_close's own docstring).
            self._release_task(entry)
            self._close(entry.co_id)
            return

        kind = self._payload_field(payload, "kind")
        if kind == "wait_seconds":
            seconds = self._payload_field(payload, "seconds") or 0
            self._pending.append(_ScheduledEntry(
                co_id=entry.co_id, owner_script_id=entry.owner_script_id,
                kind="wait_seconds", deadline=_time.monotonic() + float(seconds), started=True,
                counted=entry.counted,
            ))
            return
        if kind == "wait_for_child":
            self._pending.append(_ScheduledEntry(
                co_id=entry.co_id, owner_script_id=entry.owner_script_id,
                kind="wait_for_child", parent_id=self._payload_field(payload, "parent_id"),
                child_name=self._payload_field(payload, "name"),
                deadline=self._payload_field(payload, "deadline"), started=True,
                counted=entry.counted,
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
                counted=entry.counted,
            ))
            return
        # Unrecognized yield (e.g. a bare coroutine.yield() with no
        # descriptor) -- treat as "resume again next frame" rather than
        # silently dropping the task.
        self._pending.append(_ScheduledEntry(
            co_id=entry.co_id, owner_script_id=entry.owner_script_id,
            kind="wait_seconds", deadline=0.0, started=True, counted=entry.counted,
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
# StarterPlayerScripts (Stage 3.8: a real, serialized, singleton Instance
# under StarterPlayer -- see shared/object_registry.py's ObjectTypeDefinition
# and datamodel_schema.py's ClassDescriptor) does NOT need its own entry in
# _LOCALSCRIPT_ROOTS below. The BFS walk below starts from each literal
# root string in _EXECUTION_ROOTS and then extends the frontier with
# children_by_parent.get(current_id, []) for EVERY discovered node
# regardless of class -- so it already walks straight through a
# StarterPlayerScripts container (and anything else) nested under
# StarterPlayer, with `root` staying fixed to "StarterPlayer" for the whole
# subtree. A LocalScript inside StarterPlayerScripts is therefore already
# discovered and started exactly like one parented directly under
# StarterPlayer -- see test_lua_gameplay_api.py's
# test_localscript_under_starterplayerscripts_container_runs.
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
    {"Workspace", "StarterPlayer", "StarterGui", "ReplicatedStorage", "ServerScriptService", "ServerStorage", "Environment"}
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
        self._poll_touched()

    # ---------------- signal firing (shared by Touched and lua_gameplay_api.py) ----------------

    def fire_signal(self, signal_id: int, args: tuple) -> None:
        """ONE shared dispatch path for every built-in signal this engine
        fires from Python (Touched here, PlayerAdded/CharacterAdded/
        InputBegan/... in lua_gameplay_api.py's LuaGameplayContext) --
        creates one fresh coroutine per currently-connected listener
        (done entirely in Lua, see __signal_fire's docstring in the
        prelude), resumes each one through the normal scheduler path so
        budget/error-isolation apply uniformly, and unblocks any
        :Wait() callers via mark_signal_fired() (both mechanisms share the
        same signal_id). A no-op if the runtime isn't active or the
        signal_id is unknown/already gone."""
        if not self._active or self.lua is None:
            return
        self.scheduler.mark_signal_fired(signal_id, args)
        lua_args = self.lua.table_from(list(args)) if args else self.lua.table_from([])
        dispatch = self.lua.globals()["__signal_fire"](signal_id, lua_args)
        if dispatch is None:
            return
        try:
            items = list(dispatch.values())
        except AttributeError:
            items = list(dispatch)
        for item in items:
            co_id = item["co_id"]
            owner = item["owner"]
            owner_script_id = str(owner) if owner is not None else "<engine>"
            self.scheduler.schedule_immediate_external(owner_script_id, co_id, args)

    # ---------------- Touched (physics-driven, see physics.py) ----------------

    def _poll_touched(self) -> None:
        physics = self.scene._physics
        if physics is None:
            return
        new_pairs = physics.poll_new_contacts()
        if not new_pairs:
            return
        for a, b in new_pairs:
            self._fire_touched(a, b)
            self._fire_touched(b, a)

    def _fire_touched(self, instance_id: str, other_id: str) -> None:
        signals = self.scene._instance_signals.get(instance_id)
        if not signals:
            return
        signal_id = signals.get("Touched")
        if signal_id is None:
            return
        if other_id == "__character__":
            # Stage 3.9: the player character is not a RuntimeSceneLayer
            # Instance (see physics.py's register_external_node() and
            # client_studio.py's _start_character()) -- pass the SAME
            # Character proxy `local character = Players.LocalPlayer.
            # Character` already returns, rather than an Instance proxy
            # that would error the moment a script read `.Name` off it.
            # No-ops (does not fire) if the gameplay layer isn't active or
            # the character isn't currently valid -- matches every other
            # gameplay signal's own "no character, no event" behavior.
            if self.gameplay is None or not getattr(self.gameplay, "_character_valid", False):
                return
            other_proxy = self.lua.eval("__gameplay_character_proxy")
        else:
            other_proxy = self.lua.eval("__make_proxy")(other_id)
        self.fire_signal(signal_id, (other_proxy,))

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
            instance_id = str(instance_id)
            key = str(key)
            kind, value = scene.get_property(instance_id, key)
            if kind == "error":
                # Roblox convention (relied on by the Stage 3.9 spec's own
                # example, `workspace.Baseplate:Destroy()`): `instance.Foo`
                # falls back to "the child named Foo" whenever Foo isn't a
                # recognized property/method/signal -- tried AFTER every
                # other lookup (methods/signals/real properties always
                # win), so a Part named e.g. "Position" still can't shadow
                # the real Position property.
                child_id = scene.find_first_child(instance_id, key)
                if child_id is not None:
                    return "instance", child_id
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
            target_id = str(instance_id)
            # Stage 3.9 fix: RuntimeSceneLayer.destroy() itself now cascades
            # to descendants (render/physics/exists()), but a running
            # Script/LocalScript's scheduled coroutine is cancelled here,
            # one level up -- so cancellation must ALSO walk descendants,
            # not just the exact id Destroy() was called on, otherwise a
            # Script nested under a destroyed Folder/Model kept running
            # (and its own signal connections stayed live) even though its
            # ancestor was gone. Scripts/LocalScripts can only ever be
            # AUTHORED instances (denylisted from Instance.new()/Clone(),
            # see _LUA_UNCREATABLE_CLASSES), so game.instances is the
            # complete place to look them up. Computed BEFORE scene.destroy()
            # runs: its own cascade would stop descendants_of() from finding
            # anything once the walk it's mid-cascading reaches them.
            for candidate_id in (target_id, *scene.descendants_of(target_id)):
                record = self.game.instances.get(candidate_id)
                if record is not None and record.class_name in ("Script", "LocalScript"):
                    # "owned by" cleanup: cancel this script's own
                    # scheduled tasks/waits, AND disconnect every
                    # :Connect()/:Once() listener IT registered on any
                    # OTHER (possibly still-alive) Instance's signal --
                    # otherwise a destroyed script's dangling listener
                    # closures keep firing/keep getting scheduled forever,
                    # nothing else ever calls Disconnect() on them.
                    self.scheduler.cancel_owner(candidate_id)
                    try:
                        self.lua.globals()["__signal_disconnect_owner"](candidate_id)
                    except Exception:
                        pass
                # "targeting" cleanup: this instance's OWN built-in
                # signals (currently just Touched) -- release any
                # Signal:Wait() waiters blocked on them, then drop the
                # whole registry entry (and every listener closure with
                # it) so nothing keeps them referenced just because a
                # destroyed instance's signal_id is still a dict key here.
                signals = scene._instance_signals.pop(candidate_id, None)
                if signals:
                    for signal_id in signals.values():
                        self.scheduler.cancel_signal(signal_id)
                        try:
                            self.lua.globals()["__signal_destroy"](signal_id)
                        except Exception:
                            pass
            scene.destroy(target_id)

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

        # Stage 3.9: per-instance built-in signals (currently only
        # Touched). Lazily creates the underlying __signal_new() the FIRST
        # time any script reads `part.Touched` for a given instance,
        # rather than eagerly creating one for every Part up front --
        # mirrors _instance_signals' own "dict of dicts, populated on
        # demand" design. Existence/class checks reuse the exact same
        # scene.exists()/is_a() every other Instance member access already
        # goes through, so an error here reads the same as any other
        # "not a valid member" case.
        def bridge_get_signal(instance_id: Any, name: Any) -> tuple:
            instance_id = str(instance_id)
            name = str(name)
            if not scene.exists(instance_id):
                return False, "attempt to use a destroyed Instance"
            if name == "Touched" and not scene.is_a(instance_id, "Part"):
                return False, f"'{name}' is not a valid member of {scene.class_name_of(instance_id)}"
            signals = scene._instance_signals.setdefault(instance_id, {})
            signal_id = signals.get(name)
            if signal_id is None:
                signal_id = int(self.lua.globals()["__signal_new"]())
                signals[name] = signal_id
            return True, signal_id

        def bridge_clone(instance_id: Any) -> tuple:
            return scene.clone(str(instance_id))

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
        g["__bridge_get_signal"] = bridge_get_signal
        g["__bridge_clone"] = bridge_clone
        g["__bridge_schedule_immediate"] = self.scheduler.schedule_immediate
        g["__bridge_schedule_deferred"] = self.scheduler.schedule_deferred
        g["__bridge_schedule_delayed"] = self.scheduler.schedule_delayed
