"""
Stage 3.5: Lua Player, Character, Signal, and UserInputService API.

Architecture:

    LuaGameplayContext           <- one per Play session, owned by
                                     MultiplayerGame as self._lua_gameplay
                                     (mirrors character_controller.
                                     CharacterRuntime / lua_runtime.
                                     LuaRuntimeManager's own "fresh every
                                     Play, destroyed every Stop" lifecycle)
    +-- a small Lua prelude fragment (GAMEPLAY_PRELUDE below), executed
    |   ONCE into the live Lua VM's real _G by start() -- defines Signal/
    |   Connection, Character, Player, Players, and UserInputService as
    |   real Lua tables with locked metatables, exactly like
    |   lua_runtime.py's own Vector3/Color3/Instance prelude. This is a
    |   SEPARATE lua.execute() call from lua_runtime.py's own prelude --
    |   it does not edit or touch _PRELUDE_LUA itself (see this module's
    |   "do not rewrite the Lua sandbox" constraint). The only two
    |   surgical additions made to lua_runtime.py itself are (a) one new
    |   InstanceMethods.GetService method, so `game:GetService(...)` reads
    |   the same way every other Instance method already does, and (b)
    |   two small new bridge functions (__bridge_get_service,
    |   __bridge_current_script_id) -- both additive, neither changes any
    |   existing behavior.
    +-- Python-side bridge functions (registered as plain Lua globals,
        same convention as lua_runtime.py's own __bridge_* functions) that
        the prelude's Character/Player/UserInputService metatables call
        into for every property read/write and method call. Every one of
        these returns Lua-safe data ONLY -- a string, number, bool, a
        {R,G,B}/{X,Y,Z} array for Vector3/Color3 wrapping, or an
        (ok, error_message) pair. Never a Python object, Ursina Entity,
        Panda NodePath, Bullet node, or Qt object.

NETWORK/PERSISTENCE BOUNDARY (mirrors character_controller.py's and
character_rig.py's own documented boundaries): every property this module
lets Lua read or write is LOCAL-RUNTIME-ONLY. Appearance changes go
straight to the existing CharacterVisualRig (character_controller.py/
character_rig.py's own accepted, unmodified API) and never touch
self.game.instances, never call mark_place_dirty(), never enter
editor_history, and are never sent over the network. LocalPlayer's Name/
UserId are a fixed local-preview identity (see LOCAL_PLAYER_NAME/
LOCAL_PLAYER_USER_ID below) -- not an account, not authentication, not
remote-replicated. Script and LocalScript both see the identical Players/
Character API in this stage because there is still only one local Play VM
(see lua_runtime.py's own module docstring) -- UserInputService is
documented here as intended primarily for LocalScript, but nothing
currently prevents a Script from reading it too, matching the "no real
server/client split yet" limitation this stage explicitly does not pretend
to have solved.

SIGNAL FIRING SAFETY: every listener (whether from :Connect()/:Once() or
a :Wait() resume) is dispatched through the EXISTING
LuaTaskScheduler._resume() path (via schedule_immediate_external() or the
scheduler's own "signal_wait" pending-entry handling, both added to
lua_runtime.py as small, additive methods) -- never called directly from
Python. This means every listener gets its own coroutine, its own fresh
per-resume instruction budget, and errors are isolated/reported exactly
like any other script code, with zero new diagnostic plumbing needed (see
LuaTaskScheduler._resume()'s existing error-reporting branch).
"""

from __future__ import annotations

from typing import Any, Optional

import character_rig
from lua_runtime import _lua_array_to_list

# ============================================================
# LOCAL PREVIEW IDENTITY (Stage 3.5 spec: "deterministic local preview
# identity" -- explicitly NOT an account, NOT authentication, NOT a real
# permanent UserId, NOT remote-replicated).
# ============================================================

LOCAL_PLAYER_NAME = "LocalPreviewPlayer"
LOCAL_PLAYER_USER_ID = -1  # negative/sentinel, deliberately never a plausible real Roblox-style UserId

ACCESSORY_SLOT_TO_PART = {
    "Head": character_rig.PART_HEAD_ACCESSORY_SLOT,
    "Neck": character_rig.PART_NECK_ACCESSORY_SLOT,
    "Back": character_rig.PART_BACK_ACCESSORY_SLOT,
}

CAMERA_MODES = ("FirstPerson", "ThirdPerson")

# Stage 3.5/3.9 spec's stable string enums.
TRACKED_KEYS = {
    "w": "W", "a": "A", "s": "S", "d": "D", "space": "Space",
    "v": "V", "escape": "Escape",
    # Stage 3.9: common gameplay action keys (interact/use, secondary
    # action, sprint) -- "left shift"'s own " up" release event
    # ("left shift up") already fits the generic suffix-stripping
    # convention below unchanged, unlike the mouse buttons (see
    # TRACKED_MOUSE_BUTTONS).
    "e": "E", "f": "F", "left shift": "LeftShift",
}
# Movement/jump keys are already consumed by the character controller
# (see update_character() in client_studio.py) -- Stage 3.5 spec: "gameplay
# movement input is marked processed=true where appropriate".
_PROCESSED_KEYS = {"W", "A", "S", "D", "Space"}

# Stage 3.9: Ursina reports mouse buttons as two entirely distinct strings
# ("left mouse down" / "left mouse up"), NOT a base key name plus an " up"
# suffix the way keyboard keys are -- confirmed by inspection of
# client_studio.py's existing input() dispatch for 'left mouse down'/
# 'right mouse down'. An exact-match table, checked BEFORE the generic
# suffix-stripping path in on_key_event() below, rather than bolting a
# second naming convention onto TRACKED_KEYS. Maps directly to
# (KeyCode, is_up) -- Roblox's own InputObject.UserInputType convention
# for mouse buttons ("MouseButton1"/"MouseButton2"), not "Keyboard".
TRACKED_MOUSE_BUTTONS: dict[str, tuple[str, bool]] = {
    "left mouse down": ("MouseButton1", False),
    "left mouse up": ("MouseButton1", True),
    "right mouse down": ("MouseButton2", False),
    "right mouse up": ("MouseButton2", True),
}


# ============================================================
# LUA PRELUDE FRAGMENT -- executed once per Play session, after
# lua_runtime.py's own prelude, into the same live _G. See module
# docstring for why this is a separate execute() call.
# ============================================================

GAMEPLAY_PRELUDE = r"""
-- Stage 3.9: the generic Signal/Connection engine (Connect/Once/Wait/
-- Disconnect) that used to be defined HERE, under a __gameplay_ prefix,
-- now lives in lua_runtime.py's own core prelude as __signal_new/
-- __signal_make_proxy/__signal_fire/__signal_clear_all -- Touched (a
-- non-gameplay-layer, purely physics-driven signal) needed the exact same
-- machinery, so it was promoted to core rather than duplicated. This
-- fragment just calls the relocated functions by their new names; nothing
-- about signal BEHAVIOR changed.
local make_signal_proxy = __signal_make_proxy

-- ---------------- Character ----------------
local CharacterMeta = {}
CharacterMeta.__metatable = "locked"
CharacterMeta.__tostring = function(self) return "Character" end

local CharacterMethods = {}

function CharacterMethods.Jump(self)
    local ok, err = __bridge_character_jump()
    if not ok then error(err, 2) end
    return true
end

function CharacterMethods.SetCameraMode(self, mode)
    local ok, err = __bridge_character_set_camera_mode(tostring(mode))
    if not ok then error(err, 2) end
end

function CharacterMethods.SetAccessory(self, slot, kind)
    local ok, err = __bridge_character_set_accessory(tostring(slot), tostring(kind))
    if not ok then error(err, 2) end
end

function CharacterMethods.RemoveAccessory(self, slot)
    local ok, err = __bridge_character_remove_accessory(tostring(slot))
    if not ok then error(err, 2) end
end

CharacterMeta.__index = function(self, key)
    local method = CharacterMethods[key]
    if method ~= nil then return method end
    local kind, raw = __bridge_character_get(key)
    if kind == "error" then error(raw, 2) end
    if kind == "vector3" then return Vector3.new(raw[1], raw[2], raw[3]) end
    if kind == "color3" then return Color3.new(raw[1], raw[2], raw[3]) end
    return raw
end

CharacterMeta.__newindex = function(self, key, value)
    local payload = value
    if typeof(value) == "Color3" then payload = {value.R, value.G, value.B} end
    local ok, err = __bridge_character_set(key, payload)
    if not ok then error(err, 2) end
end

local character_proxy = setmetatable({}, CharacterMeta)
_G.__gameplay_character_proxy = character_proxy

-- ---------------- Player / Players ----------------
local PlayerMeta = {}
PlayerMeta.__metatable = "locked"
PlayerMeta.__tostring = function(self) return "Player" end

PlayerMeta.__index = function(self, key)
    if key == "Character" then
        if __bridge_character_exists() then return character_proxy end
        return nil
    end
    local kind, raw = __bridge_player_get(key)
    if kind == "error" then error(raw, 2) end
    if kind == "color3" then return Color3.new(raw[1], raw[2], raw[3]) end
    if kind == "signal" then return make_signal_proxy(raw) end
    return raw
end

PlayerMeta.__newindex = function(self, key, value)
    local payload = value
    if typeof(value) == "Color3" then payload = {value.R, value.G, value.B} end
    local ok, err = __bridge_player_set(key, payload)
    if not ok then error(err, 2) end
end

local player_proxy = setmetatable({}, PlayerMeta)
_G.__gameplay_player_proxy = player_proxy

local PlayersMeta = {}
PlayersMeta.__metatable = "locked"
PlayersMeta.__tostring = function(self) return "Players" end

local PlayersMethods = {}
function PlayersMethods.GetPlayers(self)
    -- Fresh table every call (Stage 3.5 spec: "not a mutable reference to
    -- internal storage") -- there's only ever one local runtime player in
    -- this stage, but callers must not be able to influence future calls
    -- by mutating whatever this method returns.
    return {player_proxy}
end

PlayersMeta.__index = function(self, key)
    local method = PlayersMethods[key]
    if method ~= nil then return method end
    if key == "LocalPlayer" then return player_proxy end
    if key == "PlayerAdded" then return make_signal_proxy(__gameplay_signal_ids.PlayerAdded) end
    if key == "PlayerRemoving" then return make_signal_proxy(__gameplay_signal_ids.PlayerRemoving) end
    error("'" .. tostring(key) .. "' is not a valid member of Players", 2)
end

_G.__gameplay_players_service = setmetatable({}, PlayersMeta)

-- ---------------- UserInputService ----------------
local UserInputServiceMeta = {}
UserInputServiceMeta.__metatable = "locked"
UserInputServiceMeta.__tostring = function(self) return "UserInputService" end

local UserInputServiceMethods = {}
function UserInputServiceMethods.IsKeyDown(self, keyCode)
    return __bridge_uis_is_key_down(tostring(keyCode))
end
function UserInputServiceMethods.IsInputCaptured(self)
    return __bridge_uis_is_input_captured()
end

UserInputServiceMeta.__index = function(self, key)
    local method = UserInputServiceMethods[key]
    if method ~= nil then return method end
    if key == "InputBegan" then return make_signal_proxy(__gameplay_signal_ids.InputBegan) end
    if key == "InputEnded" then return make_signal_proxy(__gameplay_signal_ids.InputEnded) end
    if key == "JumpRequest" then return make_signal_proxy(__gameplay_signal_ids.JumpRequest) end
    error("'" .. tostring(key) .. "' is not a valid member of UserInputService", 2)
end

_G.__gameplay_uis_service = setmetatable({}, UserInputServiceMeta)
"""


def build_gameplay_prelude() -> str:
    """Currently just returns GAMEPLAY_PRELUDE verbatim -- kept as its own
    function (mirroring lua_runtime.py's own _build_prelude_source())
    rather than executing the module-level string directly, so a future
    stage that needs to bake in a per-session constant has an obvious,
    already-established place to do it, the same way _build_prelude_source()
    substitutes __API_VERSION__ today."""
    return GAMEPLAY_PRELUDE


# ============================================================
# PYTHON-SIDE CONTEXT
# ============================================================

class LuaGameplayContext:
    """Owned by MultiplayerGame as self._lua_gameplay -- created fresh
    every Play (after _start_lua()'s LuaRuntimeManager.start() succeeds),
    destroyed every Stop (before LuaRuntimeManager.stop() tears down the
    VM -- see stop()'s docstring for why that ordering matters). Never
    created standalone; always paired with a live LuaRuntimeManager whose
    .gameplay attribute is set to this instance for the
    InstanceMethods.GetService bridge to find."""

    def __init__(self, game: Any, lua_manager: Any) -> None:
        self.game = game
        self.lua_manager = lua_manager
        self._signal_ids: dict[str, int] = {}
        self._character_valid = False
        self._character_added_pending = False
        self._first_update_done = False
        self._accent_color = (0.0, 0.784, 1.0)  # mirrors character_rig.default_appearance()'s accent_color
        self._held_keys: set[str] = set()
        self._active = False

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        lua = self.lua_manager.lua
        lua.execute(build_gameplay_prelude())
        self._register_bridge_functions()

        self._signal_ids = {
            name: lua.globals()["__signal_new"]()
            for name in ("PlayerAdded", "PlayerRemoving", "CharacterAdded", "CharacterRemoving", "InputBegan", "InputEnded", "JumpRequest")
        }
        lua.globals()["__gameplay_signal_ids"] = lua.table_from(dict(self._signal_ids))

        self.lua_manager.gameplay = self
        self._held_keys.clear()
        self._first_update_done = False
        self._active = True

        # Stage 3.5 spec Play ordering: by the time _start_lua() (and so
        # this context) is created, _start_character() has ALREADY run
        # (see client_studio.py's set_studio_playing()) -- the controller/
        # rig already exist in Python. Character becomes Lua-readable
        # immediately (matches real usage: a script that does
        # `local character = Players.LocalPlayer.Character` on its very
        # first line, without waiting for CharacterAdded, must not see nil
        # here -- CharacterAdded exists for scripts that specifically want
        # a respawn/(re)connect notification, not as the only way to reach
        # an already-existing character). CharacterAdded itself is still
        # deferred to the first update() tick (below) so a script's first
        # line -- typically `player.CharacterAdded:Connect(...)` -- has
        # already run and connected before the event actually fires,
        # instead of racing it.
        if self.game._character_runtime is not None and self.game._character_visual is not None:
            self._character_valid = True
            self._character_added_pending = True
            color = self.game._character_visual.appearance.accent_color
            self._accent_color = (float(color[0]), float(color[1]), float(color[2]))

    def update(self, dt: float) -> None:
        """Call AFTER LuaRuntimeManager.update(dt) each frame (see
        client_studio.py's update_lua())."""
        if not self._active:
            return
        if not self._first_update_done:
            self._first_update_done = True
            if self._character_added_pending:
                self._character_added_pending = False
                self._fire("CharacterAdded", (self.lua_manager.lua.globals()["__gameplay_character_proxy"],))

    def stop(self) -> None:
        """Must run BEFORE LuaRuntimeManager.stop() tears down the VM --
        see client_studio.py's _stop_lua(). Fires CharacterRemoving while
        the character proxy is still fully readable (self._character_valid
        is still True at the moment of firing), pumps the scheduler a
        bounded number of times so listeners actually get to run within
        their normal instruction budget, THEN invalidates the proxy and
        clears all signal state. The VM itself (and therefore every
        Connection any script still held) is discarded moments later by
        the caller's own LuaRuntimeManager.stop() -- this method does not
        need to individually walk and disconnect every Connection, only
        guarantee CharacterRemoving's listeners got their chance to run
        first."""
        if not self._active:
            return
        if self._character_valid:
            self._fire("CharacterRemoving", (self.lua_manager.lua.globals()["__gameplay_character_proxy"],))
            # Bounded pump: let CharacterRemoving listeners (and anything
            # they themselves schedule, e.g. task.spawn) actually run,
            # without risking an unbounded loop if a listener keeps
            # rescheduling itself forever (task.wait(0) in a loop, etc.) --
            # each pump tick is itself budget/wall-time protected exactly
            # like a normal frame (see LuaTaskScheduler.update()).
            for _ in range(4):
                self.lua_manager.scheduler.update()

        self._character_valid = False
        self._character_added_pending = False
        self._held_keys.clear()
        try:
            self.lua_manager.lua.globals()["__signal_clear_all"]()
        except Exception:
            pass
        self.lua_manager.gameplay = None
        self._active = False

    # ---------------- signal firing ----------------

    def _fire(self, signal_name: str, args: tuple) -> None:
        """Fires a named built-in signal via the shared
        LuaRuntimeManager.fire_signal() dispatcher (see lua_runtime.py) --
        Stage 3.9 moved the actual Connect/fire/dispatch plumbing there so
        Touched and Player/Character/UserInputService signals share ONE
        implementation instead of each keeping a private copy."""
        if not self._active:
            return
        signal_id = self._signal_ids.get(signal_name)
        if signal_id is None:
            return
        self.lua_manager.fire_signal(signal_id, args)

    # ---------------- input ----------------

    def on_key_event(self, ursina_key: str) -> None:
        """Called from client_studio.py's input() for every Play-mode key
        event, regardless of whether that key is also separately handled
        (mouse-look capture, V toggle, Escape release, ...) -- this is a
        purely additive observer, never a replacement for that existing
        dispatch. Only forwards TRACKED_KEYS/TRACKED_MOUSE_BUTTONS;
        anything else (arbitrary keys Ursina reports) is ignored so this
        can never flood Lua."""
        if not self._active:
            return
        mouse_entry = TRACKED_MOUSE_BUTTONS.get(ursina_key)
        if mouse_entry is not None:
            key_code, is_up = mouse_entry
            # Roblox convention: a mouse button's UserInputType IS its own
            # enum name (MouseButton1/MouseButton2), unlike keyboard keys
            # where UserInputType is the constant "Keyboard" and KeyCode
            # varies.
            self._handle_tracked_input(key_code, is_up, key_code)
            return
        is_up = ursina_key.endswith(" up")
        base = ursina_key[:-3] if is_up else ursina_key
        key_code = TRACKED_KEYS.get(base)
        if key_code is None:
            return
        self._handle_tracked_input(key_code, is_up, "Keyboard")

    def _handle_tracked_input(self, key_code: str, is_up: bool, user_input_type: str) -> None:
        if is_up:
            if key_code not in self._held_keys:
                return
            self._held_keys.discard(key_code)
            self._dispatch_input(key_code, "End", user_input_type)
        else:
            if key_code in self._held_keys:
                return  # key-repeat guard -- one InputBegan per physical press, not per frame held
            self._held_keys.add(key_code)
            self._dispatch_input(key_code, "Begin", user_input_type)
            if key_code == "Space":
                self._fire("JumpRequest", ())

    def release_all_keys(self) -> None:
        """Called from client_studio.py's release_play_input_capture()
        (Escape, focus loss, Stop) -- Stage 3.5 spec: "input released by
        Escape must not remain logically stuck" / "losing viewport/
        application focus generates safe release behavior". Fires
        InputEnded for whatever is still marked down, then clears state,
        so a script's own held-key bookkeeping (if any) never sees a key
        that's stuck 'down' forever."""
        if not self._active or not self._held_keys:
            return
        for key_code in list(self._held_keys):
            self._dispatch_input(key_code, "End")
        self._held_keys.clear()

    def _dispatch_input(self, key_code: str, state: str, user_input_type: str = "Keyboard") -> None:
        lua = self.lua_manager.lua
        input_object = lua.table_from({
            "KeyCode": key_code,
            "UserInputType": user_input_type,
            "State": state,
        })
        processed = key_code in _PROCESSED_KEYS
        signal_name = "InputBegan" if state == "Begin" else "InputEnded"
        self._fire(signal_name, (input_object, processed))

    # ---------------- GetService ----------------

    def get_service(self, name: str) -> tuple[bool, Any]:
        lua = self.lua_manager.lua
        if name == "Players":
            return True, lua.globals()["__gameplay_players_service"]
        if name == "UserInputService":
            return True, lua.globals()["__gameplay_uis_service"]
        return False, f"\"{name}\" is not a valid service name"

    # ---------------- bridge function registration ----------------

    def _register_bridge_functions(self) -> None:
        g = self.lua_manager.lua.globals()

        def character_exists() -> bool:
            return self._character_valid

        def character_get(key: str) -> tuple:
            if not self._character_valid:
                return "error", "Character is no longer available"
            kind, value = self._character_get(str(key))
            if kind in ("vector3", "color3"):
                # A raw Python list crossing into Lua does NOT behave like a
                # 1-based Lua array (Lua's `t[1]` on a bare Python list
                # calls Python's __getitem__(1), i.e. the list's SECOND
                # element -- see lua_runtime.py's _lua_array_to_list()
                # docstring for the full explanation). table_from() builds
                # a genuine Lua table instead, which DOES use native 1-based
                # indexing when read from Lua -- exactly what
                # lua_runtime.py's own bridge_get() already does for the
                # same reason.
                return kind, self.lua_manager.lua.table_from([float(v) for v in value])
            return kind, value

        def character_set(key: str, value: Any) -> tuple:
            if not self._character_valid:
                return False, "Character is no longer available"
            return self._character_set(str(key), value)

        def character_jump() -> tuple:
            if not self._character_valid:
                return False, "Character is no longer available"
            jumped = self.game._character_runtime.controller.try_jump()
            return True, None if jumped else False

        def character_set_camera_mode(mode: str) -> tuple:
            if not self._character_valid:
                return False, "Character is no longer available"
            if mode not in CAMERA_MODES:
                return False, f"'{mode}' is not a valid camera mode (expected \"FirstPerson\" or \"ThirdPerson\")"
            # Stage 3.8: StarterPlayer.CameraMode == "LockFirstPerson" for
            # this Play session blocks the ThirdPerson direction with a
            # real, catchable Lua error (unlike the V-key path, which has
            # no error-reporting channel and just silently no-ops via the
            # same guard inside set_camera_mode() itself).
            if mode == "ThirdPerson" and getattr(self.game, "_camera_mode_locked_first_person", False):
                return False, "SetCameraMode('ThirdPerson') is not allowed while StarterPlayer.CameraMode is LockFirstPerson"
            self.game.set_camera_mode(mode == "ThirdPerson")
            return True, None

        def character_set_accessory(slot: str, kind: str) -> tuple:
            if not self._character_valid:
                return False, "Character is no longer available"
            part_name = ACCESSORY_SLOT_TO_PART.get(slot)
            if part_name is None:
                return False, f"'{slot}' is not a valid accessory slot (expected one of {sorted(ACCESSORY_SLOT_TO_PART)})"
            # character_rig.py's built-in accessory kinds are lowercase
            # ("fedora", "cap"); the Lua-facing API follows the spec's own
            # examples (SetAccessory("Head", "Fedora")) -- normalize here
            # rather than touching character_rig.py's own naming.
            try:
                self.game._character_visual.attach_accessory(part_name, kind.lower())
            except ValueError:
                return False, f"'{kind}' is not a known accessory (expected one of {sorted(k.capitalize() for k in character_rig._ACCESSORY_BUILDERS)})"
            return True, None

        def character_remove_accessory(slot: str) -> tuple:
            if not self._character_valid:
                return False, "Character is no longer available"
            part_name = ACCESSORY_SLOT_TO_PART.get(slot)
            if part_name is None:
                return False, f"'{slot}' is not a valid accessory slot (expected one of {sorted(ACCESSORY_SLOT_TO_PART)})"
            self.game._character_visual.remove_accessory(part_name)
            return True, None

        def player_get(key: str) -> tuple:
            if key == "Name":
                return "string", LOCAL_PLAYER_NAME
            if key == "UserId":
                return "number", LOCAL_PLAYER_USER_ID
            if key == "CharacterAdded":
                return "signal", self._signal_ids["CharacterAdded"]
            if key == "CharacterRemoving":
                return "signal", self._signal_ids["CharacterRemoving"]
            if key == "AccentColor":
                if self._character_valid:
                    color = self.game._character_visual.appearance.accent_color
                    rgb = [float(color[0]), float(color[1]), float(color[2])]
                else:
                    rgb = list(self._accent_color)
                return "color3", self.lua_manager.lua.table_from(rgb)  # see character_get()'s comment above
            return "error", f"'{key}' is not a valid member of Player"

        def player_set(key: str, value: Any) -> tuple:
            if key == "AccentColor":
                rgb = self._coerce_color(value)
                if rgb is None:
                    return False, "AccentColor must be assigned a Color3"
                self._accent_color = rgb
                if self._character_valid:
                    return self._character_set("AccentColor", value)
                return True, None
            return False, f"'{key}' cannot be assigned to"

        def uis_is_key_down(key_code: str) -> bool:
            return key_code in self._held_keys

        def uis_is_input_captured() -> bool:
            return bool(self.game._mouse_look_captured())

        g["__bridge_character_get"] = character_get
        g["__bridge_character_set"] = character_set
        g["__bridge_character_exists"] = character_exists
        g["__bridge_character_jump"] = character_jump
        g["__bridge_character_set_camera_mode"] = character_set_camera_mode
        g["__bridge_character_set_accessory"] = character_set_accessory
        g["__bridge_character_remove_accessory"] = character_remove_accessory
        g["__bridge_player_get"] = player_get
        g["__bridge_player_set"] = player_set
        g["__bridge_uis_is_key_down"] = uis_is_key_down
        g["__bridge_uis_is_input_captured"] = uis_is_input_captured

    # ---------------- character property get/set backends ----------------

    @staticmethod
    def _coerce_color(value: Any) -> Optional[tuple]:
        """value crosses in as a genuine Lua array table `{R, G, B}` (see
        the prelude's Character/Player __newindex, which converts a Color3
        into one) -- MUST go through _lua_array_to_list() first, exactly
        like lua_runtime.py's own bridge_set() does for Position/Rotation/
        Size/Color, or component[0] silently reads the wrong element (see
        _lua_array_to_list's own docstring for the full explanation of why
        a raw Lua table's indexing convention flips depending on which
        language reads it)."""
        try:
            components = _lua_array_to_list(value)
            r, g, b = float(components[0]), float(components[1]), float(components[2])
        except (TypeError, IndexError, KeyError, ValueError):
            return None
        return (max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)))

    def _character_get(self, key: str) -> tuple:
        runtime = self.game._character_runtime
        visual = self.game._character_visual
        controller = runtime.controller

        if key == "Name":
            # Stage 3.9: lets a script that receives the character proxy as
            # Touched's `otherPart` (see lua_runtime.py's _fire_touched())
            # print/compare a sensible name, mirroring Roblox's convention
            # that a Character model is named after its player.
            return "string", LOCAL_PLAYER_NAME
        if key == "Position":
            return "vector3", list(controller.get_position())
        if key == "HorizontalVelocity":
            vx, vz = controller.horizontal_velocity()
            return "vector3", [vx, 0.0, vz]
        if key == "Velocity":
            vx, vz = controller.horizontal_velocity()
            return "vector3", [vx, controller.vertical_velocity(), vz]
        if key == "HorizontalSpeed":
            return "number", controller.horizontal_speed()
        if key == "VerticalVelocity":
            return "number", controller.vertical_velocity()
        if key == "IsGrounded":
            return "bool", controller.is_grounded()
        if key == "FacingYaw":
            return "number", visual.facing_yaw
        if key == "CameraMode":
            return "string", "ThirdPerson" if self.game.third_person_enabled else "FirstPerson"
        if key in ("IsJumping", "IsFalling"):
            state = character_rig.select_animation_state(controller.horizontal_speed(), controller.is_grounded(), controller.vertical_velocity())
            if key == "IsJumping":
                return "bool", state == character_rig.ANIM_JUMP
            return "bool", state == character_rig.ANIM_FALL

        appearance = visual.appearance
        color_keys = {
            "AccentColor": "accent_color", "ScreenColor": "screen_color", "ShirtColor": "shirt_color",
            "VestColor": "vest_color", "TrouserColor": "trouser_color",
        }
        if key in color_keys:
            color = getattr(appearance, color_keys[key])
            return "color3", [color[0], color[1], color[2]]

        return "error", f"'{key}' is not a valid member of Character"

    def _character_set(self, key: str, value: Any) -> tuple:
        visual = self.game._character_visual
        color_keys = {
            "AccentColor": "accent_color", "ScreenColor": "screen_color", "ShirtColor": "shirt_color",
            "VestColor": "vest_color", "TrouserColor": "trouser_color",
        }
        if key not in color_keys:
            return False, f"'{key}' cannot be assigned to (read-only or not a member of Character)"
        rgb = self._coerce_color(value)
        if rgb is None:
            return False, f"{key} must be assigned a Color3"

        # character_rig.CharacterAppearance's colors are always real Ursina
        # Color objects (e.g. color.rgb32(...) in default_appearance()) --
        # a bare 3-tuple is NOT a drop-in equivalent (Entity.color's
        # setter expects a 4-component RGBA value; a 3-tuple raised
        # "tuple index out of range" internally, empirically confirmed).
        from ursina import color as ursina_color
        appearance = visual.appearance
        setattr(appearance, color_keys[key], ursina_color.rgba(rgb[0], rgb[1], rgb[2], 1.0))
        visual.set_appearance(appearance, preset_name="lua-runtime")
        return True, None
