#!/usr/bin/env python3
"""agent-glow: blink your keyboard's RGB when Claude Code, Codex or GitHub Copilot is waiting for you.

Usage:
  agent-glow devices                    list usable keyboards and whether agent-glow can drive them
  agent-glow test [AGENT|all|done] [SECONDS]
                                        blink as if an agent were waiting (default: all, 5 seconds)
  agent-glow status                     daemon, keyboard and pending alerts
  agent-glow stop                       stop the background daemon (the lighting is put back)
  agent-glow restore                    put the lighting back if a crash left it changed
  agent-glow udev-rule [VID:PID ...]    print the Linux udev rule that grants access to the keyboard
  agent-glow daemon                     run the daemon in the foreground
  agent-glow hook AGENT ACTION          called by the agent hooks, reads the event JSON on stdin
  agent-glow version

AGENT is claude, codex or copilot; ACTION is needs-input, done, resume or clear.
State, config.json and daemon.log live in ~/.agent-glow (or $AGENT_GLOW_HOME).
Configuration and troubleshooting: https://github.com/f/agent-glow
"""
# How it works: agent hooks only write a small marker file (stdlib only, always exit 0, never touch
# the keyboard), so they cannot slow down or break an agent. The daemon watches the markers and
# drives the keyboard over VIA raw HID, Logitech HID++ or a MacBook's backlight API. Every VIA and MacBook
# light change is a non-saving write, so the lighting stored in the keyboard's EEPROM is never modified.
# The previous lighting is snapshotted first and
# put back when the alert ends; if the daemon dies mid-alert, the snapshot is restored on its next start.
import json
import os
import re
import struct
import sys
import time

__version__ = "0.1.0"

USAGE_PAGE, USAGE = 0xFF60, 0x61  # the VIA raw HID interface
CMD_PROTOCOL, CMD_SET, CMD_GET, UNHANDLED = 0x01, 0x07, 0x08, 0xFF
# VIA protocol 12 (QMK, Feb 2023) and later address the lighting by channel. Protocol 11 firmware
# may or may not; see Keyboard.probe.
MIN_PROTOCOL, AMBIGUOUS_PROTOCOL = 12, 11
CH_RGBLIGHT, CH_RGB_MATRIX, CH_PROBE = 2, 3, 0x7F
LIGHTING = {"rgblight": CH_RGBLIGHT, "rgb_matrix": CH_RGB_MATRIX}
ID_BRIGHTNESS, ID_EFFECT, ID_SPEED, ID_COLOR = 1, 2, 3, 4
EFFECT_STATIC = 1  # rgblight "static light", rgb_matrix "solid color"
READ_TIMEOUT_MS = 500
# rgb_matrix scales brightness by RGB_MATRIX_MAXIMUM_BRIGHTNESS, so it does not read back exactly.
BRIGHTNESS_SLACK = 2

LOGITECH_VID = 0x046D
HIDPP_LONG, HIDPP_SHORT, HIDPP_LONG_SIZE = 0x11, 0x10, 20
HIDPP_ERRORS = (0xFF, 0x8F)
HIDPP_BLE_PAGE, HIDPP_RECEIVER_PAGE, HIDPP_RECEIVER_USAGE = 0xFF43, 0xFF00, 0x0002
HIDPP_DIRECT, HIDPP_RECEIVER_SLOTS = 0xFF, range(1, 7)
HIDPP_SWID, HIDPP_TIMEOUT_MS = 0x0A, 1500
BACKLIGHT2_HIGH, BACKLIGHT2_LOW = 0x19, 0x82
BACKLIGHT_GET, BACKLIGHT_SET, BACKLIGHT_RANGE = 0, 1, 2
BACKLIGHT_MANUAL, BACKLIGHT_MANUAL_BIT, BACKLIGHT_NO_EFFECT = 3, 0x20, 0xFF

MACBOOK = "macbook"
OBJC_LIBRARY = "/usr/lib/libobjc.A.dylib"
COREBRIGHTNESS = "/System/Library/PrivateFrameworks/CoreBrightness.framework/CoreBrightness"
MAC_SELECTORS = (b"copyKeyboardBacklightIDs", b"isKeyboardBuiltIn:", b"brightnessForKeyboard:",
                 b"setBrightness:fadeSpeed:commit:forKeyboard:")

HAPTIC_HIGH, HAPTIC_LOW = 0x19, 0xB0
HAPTIC_CAPABILITIES, HAPTIC_CONFIGURATION, HAPTIC_PLAY = 0, 1, 4
HAPTIC_GAP = 1.5
WAVEFORMS = {"sharp_state_change": 0x00, "damp_state_change": 0x01, "sharp_collision": 0x02, "damp_collision": 0x03,
             "subtle_collision": 0x04, "happy_alert": 0x05, "angry_alert": 0x06, "completed": 0x07, "square": 0x08,
             "wave": 0x09, "firework": 0x0A, "mad": 0x0B, "knock": 0x0C, "jingle": 0x0D, "ringing": 0x0E}
AGENT_WAVEFORMS = {"claude": "happy_alert", "codex": "angry_alert", "copilot": "ringing"}
FALLBACK_WAVEFORM, DONE_WAVEFORM = "happy_alert", "completed"

NEEDS_INPUT, DONE = "needs_input", "done"
AGENTS = ("claude", "codex", "copilot")
AGENT_COLORS = {"claude": (0, 255), "codex": (170, 255), "copilot": (85, 255)}  # (hue, sat): red, blue, green
FALLBACK_COLOR = AGENT_COLORS["claude"]
TEST_SESSION = "agent-glow-test"
ACTIONS = ("needs-input", "done", "resume", "clear")
COPILOT_WAITING = ("permission_prompt", "elicitation_dialog")
# The Copilot app sends no notification for these prompts, only preToolUse.
COPILOT_ASK_TOOLS = ("ask_user", "exit_plan_mode")
MAIN = "main"
STATE_KEYS = ("effect", "brightness", "speed", "hue", "sat")

POLL_IDLE, POLL_ACTIVE, RETRY_SECONDS, STOP_WAIT = 0.25, 0.1, 2.0, 10.0
LOG_MAX_BYTES = 1 << 20


def state_root():
    return os.environ.get("AGENT_GLOW_HOME") or os.path.join(os.path.expanduser("~"), ".agent-glow")


def isatty(stream):
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


class Logger:
    """Timestamped lines to a log file (rotated at 1 MB) and/or stdout."""

    def __init__(self, path=None, echo=True):
        self.path, self.echo = path, echo

    def __call__(self, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        if self.path:
            try:
                if os.path.getsize(self.path) > LOG_MAX_BYTES:
                    os.replace(self.path, self.path + ".1")
            except OSError:
                pass
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass
        if self.echo and sys.stdout is not None:
            sys.stdout.write(line)
            sys.stdout.flush()


log = Logger()


def num(value, default, lo=None, hi=None):
    if isinstance(value, bool):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def usb_id(value):
    """A USB vendor or product id from config: 19788, "0x4D4C" or "4d4c". 0 means any."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, str):
        try:
            value = int(value.strip(), 16)
        except ValueError:
            return 0
    return value if isinstance(value, int) and 0 <= value <= 0xFFFF else 0


def parse_vid_pid(text):
    """"4d4c:6505" -> (0x4D4C, 0x6505), or None."""
    m = re.fullmatch(r"\s*(?:0x)?([0-9a-fA-F]{1,4})\s*:\s*(?:0x)?([0-9a-fA-F]{1,4})\s*", text or "")
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else None


def safe(value, limit=80):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:limit] or "_"


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def write_text(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class Store:
    """Marker files (one per agent session) plus the lighting snapshot."""

    def __init__(self, root):
        self.root = root
        self.sessions = os.path.join(root, "sessions")
        self.snapshot_path = os.path.join(root, "snapshot.json")

    def _marker(self, agent, session, agent_id):
        # "+" never survives safe(), so different (agent, session, agent_id) never share a file.
        return os.path.join(self.sessions, f"{safe(agent)}+{safe(session)}+{safe(agent_id)}.json")

    def put(self, agent, session, agent_id, state, tool=None, cwd=None, now=None):
        os.makedirs(self.sessions, exist_ok=True)
        write_json(self._marker(agent, session, agent_id), {
            "agent": agent, "session": session, "agent_id": agent_id, "state": state,
            "tool": tool, "cwd": cwd, "ts": time.time() if now is None else now})

    def get(self, agent, session, agent_id):
        data = read_json(self._marker(agent, session, agent_id))
        return data if isinstance(data, dict) else None

    def markers(self):
        try:
            names = sorted(os.listdir(self.sessions))
        except OSError:
            return []
        out = []
        for name in names:
            if name.endswith(".json"):
                path = os.path.join(self.sessions, name)
                data = read_json(path)
                out.append((path, data if isinstance(data, dict) else None))
        return out

    def drop(self, agent, session, agent_id=None):
        if agent_id is not None:
            remove(self._marker(agent, session, agent_id))
            return
        for path, data in self.markers():
            if data and data.get("agent") == agent and data.get("session") == session:
                remove(path)

    def expire(self, path, seen):
        # A hook may have replaced the marker since it was read; only drop what was seen.
        current = read_json(path)
        if current == seen or not isinstance(current, dict):
            remove(path)

    def save_snapshot(self, state):
        os.makedirs(self.root, exist_ok=True)
        write_json(self.snapshot_path, state)

    def load_snapshot(self):
        data = read_json(self.snapshot_path)
        if not isinstance(data, dict) or not all(
                isinstance(data.get(k), int) and not isinstance(data.get(k), bool) and 0 <= data[k] <= 255
                for k in STATE_KEYS):
            return None
        snap = {k: data[k] for k in STATE_KEYS}
        if isinstance(data.get("device"), str):
            snap["device"] = data["device"]
        return snap

    def has_snapshot(self):
        return os.path.exists(self.snapshot_path)

    def drop_snapshot(self):
        remove(self.snapshot_path)


# ---------------------------------------------------------------- hooks

def pick(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def handle_hook(store, agent, action, payload, now=None):
    """Apply one agent lifecycle event to the marker files."""
    session = pick(payload, "session_id", "sessionId") or "default"
    agent_id = pick(payload, "agent_id", "agentId") or MAIN
    tool = pick(payload, "tool_name", "toolName")
    cwd = pick(payload, "cwd")

    if action == "needs-input":
        kind = pick(payload, "notification_type")
        if agent == "copilot" and kind and kind not in COPILOT_WAITING:
            return
        if agent == "copilot" and not kind and tool and tool not in COPILOT_ASK_TOOLS:
            return
        store.put(agent, session, agent_id, NEEDS_INPUT, tool, cwd, now)
    elif action == "done":
        if agent_id != MAIN:
            store.drop(agent, session, agent_id)
            return
        store.drop(agent, session)
        store.put(agent, session, MAIN, DONE, None, cwd, now)
    elif action == "resume":
        marker = store.get(agent, session, agent_id)
        if marker and (marker.get("state") == DONE or not marker.get("tool") or not tool
                       or marker.get("tool") == tool):
            store.drop(agent, session, agent_id)
    elif action == "clear":
        store.drop(agent, session)


def parse_payload(raw):
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        # Oversized or cut-off payload: the identifying fields are near the start.
        head = raw[:65536].decode("utf-8", "replace")
        found = {}
        for key in ("session_id", "sessionId", "agent_id", "agentId", "tool_name", "toolName",
                    "notification_type", "cwd"):
            m = re.search(r'"%s"\s*:\s*"([^"\\]*)"' % key, head)
            if m:
                found[key] = m.group(1)
        return found


def read_select(fd, limit, timeout):
    import select
    end = time.monotonic() + timeout
    chunks, size = [], 0
    while size < limit:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return b"".join(chunks), True
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return b"".join(chunks), True
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


def read_threaded(fd, limit, timeout):
    """For Windows, where select() does not work on pipes. Reads the raw fd, not sys.stdin, so a
    reader that is still blocked cannot hold the stdin buffer lock at interpreter shutdown."""
    import threading
    chunks, finished = [], threading.Event()

    def reader():
        size = 0
        try:
            while size < limit:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        except OSError:
            pass
        finished.set()

    threading.Thread(target=reader, daemon=True).start()
    done = finished.wait(timeout)
    return b"".join(list(chunks)), not done


def read_stdin(limit=16 << 20, timeout=2.0, threaded=None):
    """The hook payload, and whether a reader may still be blocked on stdin (agent kept it open)."""
    if sys.stdin is None or isatty(sys.stdin):
        return b"", False
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return b"", False
    if threaded is None:
        threaded = os.name == "nt"
    return (read_threaded if threaded else read_select)(fd, limit, timeout)


def cmd_hook(args):
    pending = False
    try:
        agent, action = args[0], args[1]
        if agent in AGENTS and action in ACTIONS:
            raw, pending = read_stdin()
            handle_hook(Store(state_root()), agent, action, parse_payload(raw or b"{}"))
    except BaseException:  # a hook must never disturb the agent that called it
        pass
    if sys.stdout is not None:
        sys.stdout.write("{}")  # Codex requires JSON on stdout for some events, and {} is a no-op everywhere
        sys.stdout.flush()
    if pending and os.name == "nt":
        os._exit(0)  # do not wait at exit for the thread that is still blocked on stdin
    return 0


# ---------------------------------------------------------------- keyboard

class KeyboardError(OSError):
    pass


def load_hid():
    if sys.platform.startswith("linux"):
        try:
            import hidraw  # hidapi's hidraw backend; the default libusb one detaches the kernel driver
            return hidraw
        except ImportError:
            pass
    import hid
    return hid


def via_interfaces(hid_module, vid=0, pid=0):
    return [info for info in hid_module.enumerate(vid, pid)
            if info.get("usage_page") == USAGE_PAGE and info.get("usage") == USAGE]


def device_id(info):
    return f"{info.get('vendor_id') or 0:04x}:{info.get('product_id') or 0:04x}"


def device_name(info):
    parts = [str(info.get(k)).strip() for k in ("manufacturer_string", "product_string") if info.get(k)]
    return " ".join(p for p in parts if p) or "unnamed keyboard"


class Keyboard:
    """VIA raw HID client for the RGB lighting settings, non-saving writes only."""

    def __init__(self, dev, info=None, channel=CH_RGBLIGHT, protocol=None):
        self.dev = dev
        self.info = info or {}
        self.channel = channel
        self.protocol = protocol

    @property
    def id(self):
        return device_id(self.info)

    @property
    def name(self):
        return device_name(self.info)

    @property
    def lighting(self):
        return {v: k for k, v in LIGHTING.items()}.get(self.channel, str(self.channel))

    @property
    def details(self):
        return f"VIA protocol {self.protocol}, {self.lighting}"

    @classmethod
    def probe(cls, hid_module, info, lighting="auto"):
        """Open one VIA interface: (Keyboard, None) if its lighting can be driven, else (None, reason)."""
        dev = hid_module.device()
        try:
            dev.open_path(info["path"])
        except (OSError, ValueError, KeyError) as e:
            hint = " (on Linux: see `agent-glow udev-rule`)" if sys.platform.startswith("linux") else ""
            return None, f"cannot open it: {e}{hint}"
        kb = cls(dev, info)
        try:
            reply = kb.xfer([CMD_PROTOCOL])
            kb.protocol = reply[1] << 8 | reply[2]
            if reply[0] != CMD_PROTOCOL:
                reason = "it does not answer VIA commands"
            elif kb.protocol < MIN_PROTOCOL and not (kb.protocol >= AMBIGUOUS_PROTOCOL and kb.has_channels()):
                reason = (f"its VIA protocol {kb.protocol} is too old; agent-glow needs {MIN_PROTOCOL} or newer "
                          f"(QMK firmware from 2023 on)")
            else:
                wanted = [LIGHTING[lighting]] if lighting in LIGHTING else [CH_RGBLIGHT, CH_RGB_MATRIX]
                for channel in wanted:
                    if kb.supports(channel):
                        kb.channel = channel
                        return kb, None
                reason = "its firmware does not expose " + (
                    lighting if lighting in LIGHTING else "RGB Light or RGB Matrix") + " over VIA"
        except OSError as e:
            reason = f"it did not answer: {e}"
        kb.close()
        return None, reason

    @classmethod
    def open(cls, hid_module=None, vid=0, pid=0, lighting="auto"):
        """The first VIA keyboard whose lighting can be driven, or None if there is no VIA keyboard."""
        hid_module = hid_module or load_hid()
        reasons = []
        for info in via_interfaces(hid_module, vid, pid):
            kb, reason = cls.probe(hid_module, info, lighting)
            if kb:
                return kb
            reasons.append(f"{device_id(info)} {device_name(info)}: {reason}")
        if reasons:
            raise KeyboardError("; ".join(reasons))
        return None

    def close(self):
        try:
            self.dev.close()
        except OSError:
            pass

    def xfer(self, payload):
        if self.dev.write(b"\x00" + bytes(payload).ljust(32, b"\x00")) < 0:
            raise KeyboardError("write failed")
        reply = self.dev.read(32, READ_TIMEOUT_MS)
        if not reply:
            raise KeyboardError("keyboard did not answer")
        return bytes(reply).ljust(32, b"\x00")

    def supports(self, channel):
        return self.xfer([CMD_GET, channel, ID_EFFECT])[:3] == bytes([CMD_GET, channel, ID_EFFECT])

    def has_channels(self):
        # Channel-aware firmware rejects an unknown channel; older firmware just echoes the request.
        return self.xfer([CMD_GET, CH_PROBE, ID_EFFECT])[0] == UNHANDLED

    def get(self, value_id):
        r = self.xfer([CMD_GET, self.channel, value_id])
        if r[:3] != bytes([CMD_GET, self.channel, value_id]):
            raise KeyboardError(f"unexpected reply to get {value_id}: {r[:4].hex()}")
        return r[3], r[4]

    def set(self, value_id, *values):
        r = self.xfer([CMD_SET, self.channel, value_id, *values])
        if r[:3] != bytes([CMD_SET, self.channel, value_id]):
            raise KeyboardError(f"unexpected reply to set {value_id}: {r[:4].hex()}")

    def read_state(self):
        hue, sat = self.get(ID_COLOR)
        return {"effect": self.get(ID_EFFECT)[0], "brightness": self.get(ID_BRIGHTNESS)[0],
                "speed": self.get(ID_SPEED)[0], "hue": hue, "sat": sat}

    def alert(self, hue, sat, brightness):
        self.set(ID_EFFECT, EFFECT_STATIC)
        self.set(ID_COLOR, hue, sat)
        self.set(ID_BRIGHTNESS, brightness)

    def restore(self, s):
        # Effect last so an animated effect starts from the restored color and brightness;
        # effect 0 turns the lighting off again.
        self.set(ID_COLOR, s["hue"], s["sat"])
        self.set(ID_BRIGHTNESS, s["brightness"])
        self.set(ID_EFFECT, s["effect"])
        self.set(ID_SPEED, s["speed"])


class HidppTimeout(KeyboardError):
    pass


def hidpp_interfaces(hid_module, vid=0, pid=0):
    if vid and vid != LOGITECH_VID:
        return []
    return [info for info in hid_module.enumerate(LOGITECH_VID, pid)
            if info.get("usage_page") == HIDPP_BLE_PAGE
            or (info.get("usage_page") == HIDPP_RECEIVER_PAGE and info.get("usage") == HIDPP_RECEIVER_USAGE)]


def hidpp_exchange(device, feature, function, *params):
    """Send one HID++ 2.0 request to device.number over device.dev and return the reply's parameters."""
    call = function << 4 | HIDPP_SWID
    report = bytes([HIDPP_LONG, device.number, feature, call, *params]).ljust(HIDPP_LONG_SIZE, b"\x00")
    if device.dev.write(report) < 0:
        raise KeyboardError("write failed")
    deadline = time.monotonic() + HIDPP_TIMEOUT_MS / 1000
    while True:
        remaining = int((deadline - time.monotonic()) * 1000)
        reply = device.dev.read(32, remaining) if remaining > 0 else None
        if not reply:
            raise HidppTimeout("device did not answer")
        r = bytes(reply).ljust(HIDPP_LONG_SIZE, b"\x00")
        if r[0] not in (HIDPP_LONG, HIDPP_SHORT) or r[1] not in (device.number, device.number ^ 0xFF):
            continue
        if r[2:4] == bytes([feature, call]):
            return r[4:]
        if r[2] in HIDPP_ERRORS and r[3:5] == bytes([feature, call]):
            raise KeyboardError(f"HID++ error {r[5]:#04x}")


def set_open_exclusive(hid_module, exclusive):
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        ctypes.CDLL(hid_module.__file__).hid_darwin_set_open_exclusive(int(exclusive))
    except (AttributeError, ImportError, OSError, TypeError):
        pass


class LogiKeyboard(Keyboard):
    def __init__(self, dev, info=None, number=HIDPP_DIRECT):
        super().__init__(dev, info)
        self.number = number
        self.feature, self.version = 0, 0
        self.manual, self.levels = False, 0
        self.enabled, self.options, self.level = 0, 0, 0
        self.durations = (0, 0, 0, 0)

    @property
    def lighting(self):
        return "backlight"

    @property
    def details(self):
        return f"HID++ backlight v{self.version}, white only"

    @classmethod
    def probe(cls, hid_module, info, lighting="auto"):
        dev = hid_module.device()
        set_open_exclusive(hid_module, False)
        try:
            dev.open_path(info["path"])
        except (OSError, ValueError, KeyError) as e:
            hint = " (on Linux: see `agent-glow udev-rule`)" if sys.platform.startswith("linux") else ""
            return None, f"cannot open it: {e}{hint}"
        finally:
            set_open_exclusive(hid_module, True)
        kb = cls(dev, info)
        reason = "it has no HID++ backlight"
        slots = (HIDPP_DIRECT,) if info.get("usage_page") == HIDPP_BLE_PAGE else HIDPP_RECEIVER_SLOTS
        for number in slots:
            kb.number = number
            try:
                if kb.find_backlight():
                    return kb, None
            except OSError as e:
                reason = f"it did not answer: {e}"
        kb.close()
        return None, reason

    @classmethod
    def open(cls, hid_module=None, vid=0, pid=0, lighting="auto"):
        hid_module = hid_module or load_hid()
        reasons = []
        for info in hidpp_interfaces(hid_module, vid, pid):
            kb, reason = cls.probe(hid_module, info, lighting)
            if kb:
                return kb
            reasons.append(f"{device_id(info)} {device_name(info)}: {reason}")
        if reasons:
            raise KeyboardError("; ".join(reasons))
        return None

    def request(self, feature, function, *params):
        try:
            return self.exchange(feature, function, *params)
        except HidppTimeout:
            return self.exchange(feature, function, *params)

    def exchange(self, feature, function, *params):
        return hidpp_exchange(self, feature, function, *params)

    def find_backlight(self):
        index, _kind, version = self.request(0, 0, BACKLIGHT2_HIGH, BACKLIGHT2_LOW)[:3]
        if not index:
            return False
        self.feature, self.version = index, version
        supported = self.read_config()
        self.levels = self.request(index, BACKLIGHT_RANGE)[0] if version >= 3 else 0
        self.manual = bool(supported & BACKLIGHT_MANUAL_BIT) and self.levels > 1
        return True

    def read_config(self):
        r = self.request(self.feature, BACKLIGHT_GET)
        self.enabled, self.options, supported, _effects, level, *durations = struct.unpack("<BBBHBHHHH", r[:14])
        self.level = level if self.mode(self.options) == BACKLIGHT_MANUAL else 0
        self.durations = tuple(durations)
        return supported

    @staticmethod
    def mode(options):
        return options >> 3 & 0x03

    def write_config(self, enabled, options, level):
        level = level if self.mode(options) == BACKLIGHT_MANUAL else 0
        self.request(self.feature, BACKLIGHT_SET, *struct.pack(
            "<BBBBHHHH", enabled, options, BACKLIGHT_NO_EFFECT, level, *self.durations))
        self.enabled, self.options, self.level = enabled, options, level

    def set(self, value_id, *values):
        if value_id != ID_BRIGHTNESS:
            return
        if not values[0]:
            self.write_config(0, self.options, self.level)
        elif self.manual:
            level = max(1, round(values[0] * (self.levels - 1) / 255))
            self.write_config(1, self.options & 0x07 | BACKLIGHT_MANUAL << 3, level)
        else:
            self.write_config(1, self.options, 0)

    def read_state(self):
        self.read_config()
        return {"effect": self.options, "brightness": self.level, "speed": self.enabled, "hue": 0, "sat": 0}

    def alert(self, hue, sat, brightness):
        self.set(ID_BRIGHTNESS, brightness)

    def restore(self, s):
        self.write_config(s["speed"], s["effect"], s["brightness"])


class MacBacklight:
    def __init__(self, objc):
        self.objc = objc
        self.client, self.keyboard = None, None

    def send(self, target, selector, restype, argtypes=(), *args):
        import ctypes
        call = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(("objc_msgSend", self.objc))
        return call(target, self.objc.sel_registerName(selector), *args)

    @classmethod
    def find(cls):
        if sys.platform != "darwin":
            return None
        import ctypes
        try:
            objc = ctypes.CDLL(OBJC_LIBRARY)
            ctypes.CDLL(COREBRIGHTNESS)
        except OSError:
            return None
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.class_getInstanceMethod.restype = ctypes.c_void_p
        objc.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        klass = objc.objc_getClass(b"KeyboardBrightnessClient")
        if not klass or not all(objc.class_getInstanceMethod(klass, objc.sel_registerName(s)) for s in MAC_SELECTORS):
            return None
        self = cls(objc)
        pointer, ull = ctypes.c_void_p, ctypes.c_ulonglong
        self.client = self.send(self.send(klass, b"alloc", pointer), b"init", pointer)
        ids = self.send(self.client, b"copyKeyboardBacklightIDs", pointer) if self.client else None
        for i in range(self.send(ids, b"count", ctypes.c_ulong) if ids else 0):
            item = self.send(ids, b"objectAtIndex:", pointer, (ctypes.c_ulong,), i)
            keyboard = self.send(item, b"unsignedLongLongValue", ull)
            if self.send(self.client, b"isKeyboardBuiltIn:", ctypes.c_bool, (ull,), keyboard):
                self.keyboard = keyboard
                return self
        return None

    def brightness(self):
        import ctypes
        return self.send(self.client, b"brightnessForKeyboard:", ctypes.c_float, (ctypes.c_ulonglong,), self.keyboard)

    def set_brightness(self, value):
        import ctypes
        if not self.send(self.client, b"setBrightness:fadeSpeed:commit:forKeyboard:", ctypes.c_bool,
                         (ctypes.c_float, ctypes.c_int, ctypes.c_bool, ctypes.c_ulonglong),
                         value, 0, False, self.keyboard):
            raise KeyboardError("macOS refused the keyboard brightness change")


class MacKeyboard:
    id, name, lighting = "apple:builtin", "MacBook built-in keyboard", "backlight"
    details = "macOS backlight, white only"

    def __init__(self, backlight):
        self.backlight = backlight

    @classmethod
    def open(cls):
        backlight = MacBacklight.find()
        return cls(backlight) if backlight else None

    def close(self):
        pass

    def read_state(self):
        return {"effect": 0, "brightness": round(self.backlight.brightness() * 255), "speed": 0, "hue": 0, "sat": 0}

    def set(self, value_id, *values):
        if value_id == ID_BRIGHTNESS:
            self.backlight.set_brightness(values[0] / 255)

    def alert(self, hue, sat, brightness):
        self.set(ID_BRIGHTNESS, brightness)

    def restore(self, s):
        self.set(ID_BRIGHTNESS, s["brightness"])


class HapticMouse:
    def __init__(self, dev, info=None, number=HIDPP_DIRECT):
        self.dev = dev
        self.info = info or {}
        self.number = number
        self.feature, self.waveforms = 0, 0

    @property
    def id(self):
        return device_id(self.info)

    @property
    def name(self):
        return device_name(self.info)

    @classmethod
    def probe(cls, hid_module, info):
        dev = hid_module.device()
        set_open_exclusive(hid_module, False)
        try:
            dev.open_path(info["path"])
        except (OSError, ValueError, KeyError):
            return None
        finally:
            set_open_exclusive(hid_module, True)
        mouse = cls(dev, info)
        slots = (HIDPP_DIRECT,) if info.get("usage_page") == HIDPP_BLE_PAGE else HIDPP_RECEIVER_SLOTS
        for number in slots:
            mouse.number = number
            try:
                if mouse.find_haptics():
                    return mouse
            except OSError:
                pass
        mouse.close()
        return None

    @classmethod
    def open(cls, hid_module=None):
        hid_module = hid_module or load_hid()
        for info in hidpp_interfaces(hid_module):
            mouse = cls.probe(hid_module, info)
            if mouse:
                return mouse
        return None

    def close(self):
        try:
            self.dev.close()
        except OSError:
            pass

    def request(self, feature, function, *params):
        return hidpp_exchange(self, feature, function, *params)

    def find_haptics(self):
        index = self.request(0, 0, HAPTIC_HIGH, HAPTIC_LOW)[0]
        if not index or not self.request(index, HAPTIC_CONFIGURATION)[0] & 0x01:
            return False
        self.feature = index
        self.waveforms = int.from_bytes(self.request(index, HAPTIC_CAPABILITIES)[4:8], "big")
        return True

    def supports(self, name):
        return bool(self.waveforms >> WAVEFORMS[name] & 1)

    def play(self, name):
        self.request(self.feature, HAPTIC_PLAY, WAVEFORMS[name])


def same_lighting(live, wanted):
    return (all(live[k] == wanted[k] for k in ("effect", "speed", "hue", "sat"))
            and abs(live["brightness"] - wanted["brightness"]) <= BRIGHTNESS_SLACK)


# ---------------------------------------------------------------- daemon

def agent_order(name):
    return (AGENTS.index(name) if name in AGENTS else len(AGENTS), name)


def waveform(raw, default):
    return raw if isinstance(raw, str) and raw in WAVEFORMS else default


def hue_sat(raw, default):
    raw = raw if isinstance(raw, dict) else {}
    return int(num(raw.get("hue"), default[0], 0, 255)), int(num(raw.get("sat"), default[1], 0, 255))


class Config:
    def __init__(self, raw=None):
        raw = raw if isinstance(raw, dict) else {}
        kb = raw.get("keyboard") if isinstance(raw.get("keyboard"), dict) else {}
        ni = raw.get("needs_input") if isinstance(raw.get("needs_input"), dict) else {}
        dn = raw.get("done") if isinstance(raw.get("done"), dict) else {}
        colors = ni.get("colors") if isinstance(ni.get("colors"), dict) else {}
        hp = raw.get("haptics") if isinstance(raw.get("haptics"), dict) else {}
        waveforms = hp.get("waveforms") if isinstance(hp.get("waveforms"), dict) else {}
        agents = raw.get("agents")
        self.vid, self.pid = usb_id(kb.get("vid")), usb_id(kb.get("pid"))
        self.lighting = kb.get("lighting") if kb.get("lighting") in (*LIGHTING, MACBOOK) else "auto"
        self.agents = tuple(a for a in agents if isinstance(a, str)) if isinstance(agents, list) else AGENTS
        self.blink = num(raw.get("blink_seconds"), 0.4, 0.1, 5)
        self.brightness = int(num(raw.get("brightness"), 255, 1, 255))
        self.colors = {name: hue_sat(colors.get(name), AGENT_COLORS.get(name, FALLBACK_COLOR))
                       for name in {*AGENT_COLORS, *colors}}
        self.needs_input_ttl = num(ni.get("ttl_seconds"), 600, 5)
        self.done_enabled = bool(dn.get("enabled", False))
        self.done_color = hue_sat(dn, (85, 255))
        self.done_seconds = num(dn.get("seconds"), 5, 1, 300)
        self.haptics_enabled = hp.get("enabled") is True
        self.haptic_repeat = num(hp.get("repeat_seconds"), 30, 0, 3600)
        self.haptic_waveforms = {name: waveform(waveforms.get(name), AGENT_WAVEFORMS.get(name, FALLBACK_WAVEFORM))
                                 for name in {*AGENT_WAVEFORMS, *waveforms}}
        self.haptic_done = waveform(waveforms.get("done"), DONE_WAVEFORM)

    def needs_input_color(self, agent):
        return self.colors.get(agent, FALLBACK_COLOR)

    def waveform_for(self, kind, agent):
        return self.haptic_done if kind == DONE else self.haptic_waveforms.get(agent, FALLBACK_WAVEFORM)

    def describe_keyboard(self):
        which = f"{self.vid:04x}:{self.pid:04x}" if self.vid or self.pid else "first usable keyboard found"
        return f"{which}, lighting {self.lighting}"


def load_config(root):
    return Config(read_json(os.path.join(root, "config.json")))


def open_configured(config):
    if config.lighting == MACBOOK:
        return MacKeyboard.open()
    hid_module = load_hid()
    try:
        kb = Keyboard.open(hid_module, config.vid, config.pid, config.lighting)
        via_error = None
    except KeyboardError as e:
        kb, via_error = None, e
    if kb is None and config.lighting == "auto":
        try:
            kb = LogiKeyboard.open(hid_module, config.vid, config.pid)
        except KeyboardError as e:
            via_error = KeyboardError(f"{via_error}; {e}") if via_error else e
    if kb is None and config.lighting == "auto" and not config.vid and not config.pid:
        kb = MacKeyboard.open()
    if kb is None and via_error:
        raise via_error
    return kb


class Daemon:
    def __init__(self, store, open_keyboard=open_configured, wall=time.time, mono=time.monotonic,
                 logger=log, open_mouse=HapticMouse.open):
        self.store = store
        self.open_keyboard = open_keyboard
        self.open_mouse = open_mouse
        self.mouse = None
        self.mouse_retry_at = 0.0
        self.mouse_logged = False
        self.buzzed = {}
        self.buzz_at = None
        self.wall, self.mono, self.log = wall, mono, logger
        self.config_path = os.path.join(store.root, "config.json")
        self.config_mtime, self.config = None, Config()
        self.kb = None
        # While the alert lighting is applied: kind, the agents behind it, their colors, which color
        # is current ("i"/"color") and the blink phase ("on"/"toggled").
        self.alert = None
        self.retry_at = 0.0
        self.waiting_logged = False
        self.stop = False
        self.load_config()

    def load_config(self):
        try:
            mtime = os.stat(self.config_path).st_mtime
        except OSError:
            mtime = None
        if mtime != self.config_mtime:
            self.config_mtime = mtime
            self.config = Config(read_json(self.config_path) if mtime else None)

    def desired(self):
        """(kind, agents, colors) of the alert that should be showing now, or None."""
        now, cfg, who = self.wall(), self.config, {NEEDS_INPUT: set(), DONE: set()}
        for path, m in self.store.markers():
            state = m.get("state") if m else None
            if state not in who:
                self.store.expire(path, m)
                continue
            limit = cfg.needs_input_ttl if state == NEEDS_INPUT else cfg.done_seconds
            if not -5 <= now - num(m.get("ts"), 0) < limit:
                self.store.expire(path, m)
                continue
            agent = m.get("agent") if isinstance(m.get("agent"), str) else ""
            if (agent in AGENTS and agent not in cfg.agents) or (state == DONE and not cfg.done_enabled):
                continue
            who[state].add(agent)
        for kind in (NEEDS_INPUT, DONE):  # someone waiting for you beats a finished turn
            if who[kind]:
                agents = tuple(sorted(who[kind], key=agent_order))
                return kind, agents, self.palette(kind, agents)
        return None

    def palette(self, kind, agents):
        if kind == DONE:
            return (self.config.done_color,)
        colors = []
        for agent in agents:
            color = self.config.needs_input_color(agent)
            if color not in colors:  # agents sharing a color must not make it "cycle"
                colors.append(color)
        return tuple(colors)

    def keyboard(self):
        if self.kb:
            return self.kb
        now = self.mono()
        if now < self.retry_at:
            return None
        try:
            kb = self.open_keyboard(self.config)
        except (OSError, ImportError) as e:
            kb = None
            if not self.waiting_logged:
                self.log(f"cannot use the keyboard: {e}")
        if kb is None:
            self.retry_at = now + RETRY_SECONDS
            if not self.waiting_logged:
                self.log("keyboard not available, will keep retrying")
                self.waiting_logged = True
            return None
        self.waiting_logged = False
        self.kb = kb
        self.log(f"keyboard connected: {kb.id} {kb.name} ({kb.lighting})")
        return kb

    def close_keyboard(self):
        if self.kb:
            self.kb.close()
        self.kb = None

    def fail(self, error):
        self.log(f"keyboard error: {error}")
        self.close_keyboard()
        self.retry_at = self.mono() + RETRY_SECONDS
        self.alert = None  # re-apply the alert lighting after reconnecting

    def haptic_mouse(self):
        if self.mouse:
            return self.mouse
        now = self.mono()
        if now < self.mouse_retry_at:
            return None
        try:
            self.mouse = self.open_mouse()
        except (OSError, ImportError) as e:
            self.mouse = None
            if not self.mouse_logged:
                self.log(f"cannot use the haptic mouse: {e}")
        if self.mouse is None:
            self.mouse_retry_at = now + RETRY_SECONDS
            if not self.mouse_logged:
                self.log("haptic mouse not available, will keep retrying")
                self.mouse_logged = True
            return None
        self.mouse_logged = False
        self.log(f"haptic mouse connected: {self.mouse.id} {self.mouse.name}")
        return self.mouse

    def close_mouse(self):
        if self.mouse:
            self.mouse.close()
        self.mouse = None

    def haptics(self, want):
        cfg = self.config
        if want is None or not cfg.haptics_enabled:
            self.buzzed = {}
            self.close_mouse()
            return
        kind, agents, _colors = want
        keys = [(kind, agent) for agent in agents]
        self.buzzed = {key: at for key, at in self.buzzed.items() if key in keys}
        now = self.mono()
        due = [key for key in keys if key not in self.buzzed or (
            kind == NEEDS_INPUT and cfg.haptic_repeat and now - self.buzzed[key] >= cfg.haptic_repeat)]
        if not due or (self.buzz_at is not None and now - self.buzz_at < HAPTIC_GAP):
            return
        mouse = self.haptic_mouse()
        if mouse is None:
            return
        name = cfg.waveform_for(*due[0])
        try:
            if mouse.supports(name):
                mouse.play(name)
                self.buzz_at = now
        except OSError as e:
            self.log(f"haptic mouse error: {e}")
            self.close_mouse()
            self.mouse_retry_at = now + RETRY_SECONDS
            return
        self.buzzed[due[0]] = now

    def describe(self):
        a = self.alert
        self.log(f"alert {a['kind']} ({', '.join(safe(name, 20) for name in a['agents'])})")

    def begin(self, kb, want):
        kind, agents, colors = want
        snap = self.store.load_snapshot()
        # An existing snapshot of this keyboard is its real lighting (we crashed mid-alert), not our alert.
        if snap is None or snap.get("device", kb.id) != kb.id:
            self.store.save_snapshot(dict(kb.read_state(), device=kb.id))
        kb.alert(*colors[0], self.config.brightness)
        self.alert = {"kind": kind, "agents": agents, "colors": colors, "i": 0, "color": colors[0],
                      "on": True, "toggled": self.mono()}
        self.describe()

    def paint(self, kb, color):
        if color != self.alert["color"]:
            kb.set(ID_COLOR, *color)
            self.alert["color"] = color

    def retarget(self, kb, want):
        """Someone started or stopped waiting (or the colors were reconfigured) mid-alert."""
        a = self.alert
        a["kind"], a["agents"], a["colors"] = want
        a["i"] = a["colors"].index(a["color"]) if a["color"] in a["colors"] else 0
        self.paint(kb, a["colors"][a["i"]])
        self.describe()

    def blink(self, kb):
        a, now = self.alert, self.mono()
        if now - a["toggled"] >= self.config.blink:
            a["on"], a["toggled"] = not a["on"], now
            if a["on"]:  # with several agents waiting, each flash is the next agent's color
                a["i"] = (a["i"] + 1) % len(a["colors"])
                self.paint(kb, a["colors"][a["i"]])  # still dark here, so the switch is never seen
            kb.set(ID_BRIGHTNESS, self.config.brightness if a["on"] else 0)

    def settle(self):
        self.store.drop_snapshot()
        self.alert = None
        self.close_keyboard()

    def finish(self):
        snap = self.store.load_snapshot()
        if snap is None:
            self.settle()
            return
        kb = self.keyboard()
        if kb is None:
            return
        if snap.get("device", kb.id) != kb.id:
            self.log(f"the saved lighting belongs to keyboard {snap['device']}, not {kb.id}; leaving it alone")
            self.settle()
            return
        try:
            kb.restore(snap)
            live = kb.read_state()
            if not same_lighting(live, snap):
                kb.restore(snap)
                live = kb.read_state()
                if not same_lighting(live, snap):
                    self.log(f"restore mismatch: wanted {snap}, keyboard reports {live}")
        except OSError as e:
            self.fail(e)
            return
        self.settle()
        self.log("lighting restored")

    def tick(self):
        """One poll step; returns how long to sleep before the next one."""
        self.load_config()
        want = self.desired()
        self.haptics(want)
        if want is None:
            if self.alert is not None or self.store.has_snapshot():
                self.finish()
                return POLL_ACTIVE
            return POLL_IDLE
        kb = self.keyboard()
        if kb is None:
            return POLL_ACTIVE
        try:
            if self.alert is None:
                self.begin(kb, want)
            elif tuple(self.alert[k] for k in ("kind", "agents", "colors")) != want:
                self.retarget(kb, want)
            self.blink(kb)
        except OSError as e:
            self.fail(e)
        return POLL_ACTIVE

    def shutdown(self):
        if self.alert is not None or self.store.has_snapshot():
            self.finish()
        self.close_keyboard()
        self.close_mouse()


# ---------------------------------------------------------------- single instance

def _try_lock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def lock_paths(root):
    return os.path.join(root, "daemon.lock"), os.path.join(root, "daemon.pid")


def acquire_lock(root):
    """A held lock fd that marks this process as the daemon, or None if one is already running."""
    lock_path, pid_path = lock_paths(root)
    os.makedirs(root, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if not _try_lock(fd):
        os.close(fd)
        return None
    # Windows cannot read a locked byte range, so the pid goes in a file of its own.
    write_text(pid_path, str(os.getpid()))
    return fd


def daemon_pid(root):
    """PID of the running daemon (-1 if it is running but unknown), or None."""
    lock_path, pid_path = lock_paths(root)
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return None
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
    finally:
        os.close(fd)
    try:
        with open(pid_path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        text = ""
    return int(text) if text.isdigit() else -1


def cmd_daemon(_args):
    import signal
    root = state_root()
    os.makedirs(root, exist_ok=True)
    logger = Logger(os.path.join(root, "daemon.log"), echo=isatty(sys.stdout))
    lock = acquire_lock(root)
    if lock is None:
        # Exit 0 so a service manager does not keep restarting a second copy.
        logger(f"another agent-glow daemon is already running (pid {daemon_pid(root)})")
        return 0
    stop_path = os.path.join(root, "stop")
    remove(stop_path)
    daemon = Daemon(Store(root), logger=logger)

    def request_stop(_signum, _frame):
        daemon.stop = True

    for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), request_stop)
            except (OSError, ValueError):
                pass
    logger(f"agent-glow {__version__} daemon started (pid {os.getpid()}), state in {root}")
    logger(f"keyboard: {daemon.config.describe_keyboard()}")
    last_error = None
    try:
        while not daemon.stop and not os.path.exists(stop_path):
            try:
                delay = daemon.tick()
            except Exception as e:  # keep running; a restart loop could leave the lights stuck
                if repr(e) != last_error:
                    logger(f"unexpected error: {e!r}")
                    last_error = repr(e)
                delay = 1.0
            time.sleep(delay)
    finally:
        daemon.shutdown()
        remove(stop_path)
        remove(lock_paths(root)[1])
        os.close(lock)
        logger("daemon stopped")
    return 0


def cmd_stop(_args):
    root = state_root()
    if not daemon_pid(root):
        print("daemon is not running")
        return 0
    write_text(os.path.join(root, "stop"), "")
    end = time.monotonic() + STOP_WAIT
    while time.monotonic() < end:
        if not daemon_pid(root):
            print("daemon stopped, lighting restored")
            return 0
        time.sleep(0.2)
    print(f"the daemon did not stop within {STOP_WAIT:g}s; see {os.path.join(root, 'daemon.log')}")
    return 1


# ---------------------------------------------------------------- other commands

def cmd_status(_args):
    root = state_root()
    store, cfg = Store(root), load_config(root)
    pid = daemon_pid(root)
    print(f"agent-glow {__version__}")
    print(f"state dir : {root}")
    print("daemon    : " + (f"running (pid {pid})" if pid else "not running"))
    print(f"keyboard  : {cfg.describe_keyboard()}  (run `agent-glow devices` to check it)")
    print("agents    : " + (", ".join(a for a in AGENTS if a in cfg.agents) or "none"))
    print("haptics   : " + ("on" if cfg.haptics_enabled else "off"))
    print("snapshot  : " + ("saved, lighting not restored yet" if store.has_snapshot() else "none"))
    markers, now = store.markers(), time.time()
    print(f"markers   : {len(markers)}")
    for _path, m in markers:
        if m:
            print(f"  {m.get('agent')}  {m.get('state')}  tool={m.get('tool')}  "
                  f"agent_id={m.get('agent_id')}  session={m.get('session')}  "
                  f"{now - num(m.get('ts'), 0):.0f}s ago")
    return 0


def cmd_devices(_args, hid_module=None):
    try:
        hid_module = hid_module or load_hid()
    except ImportError as e:
        print(f"hidapi is not available ({e}); run install.py, or `pip install hidapi`")
        return 1
    cfg = load_config(state_root())
    mouse = HapticMouse.open(hid_module)
    if mouse:
        state = "on" if cfg.haptics_enabled else 'off, set "haptics": {"enabled": true} in config.json to use it'
        print(f"Haptic mouse:\n    {mouse.id}  {mouse.name}: ready (haptics {state})\n")
        mouse.close()
    found = [(Keyboard, info) for info in via_interfaces(hid_module)] if cfg.lighting != MACBOOK else []
    if cfg.lighting == "auto":
        found += [(LogiKeyboard, info) for info in hidpp_interfaces(hid_module)]
    mac = MacKeyboard.open() if cfg.lighting in ("auto", MACBOOK) else None
    if not found and not mac:
        print("No VIA keyboard found. Connect it with a USB cable (wireless modes usually do not expose VIA)")
        print("and make sure it runs VIA-enabled QMK firmware.")
        print("No Logitech keyboard found either. Turn it on and connect it by Bluetooth or a Bolt receiver.")
        return 1
    chosen, cannot_open = None, False
    print("Keyboards:")
    for driver, info in found:
        pinned = (not cfg.vid or info.get("vendor_id") == cfg.vid) and (not cfg.pid or info.get("product_id") == cfg.pid)
        kb, reason = driver.probe(hid_module, info, cfg.lighting)
        if kb:
            use = pinned and chosen is None
            chosen = chosen or (kb.id if use else None)
            print(f"  {'*' if use else ' '} {kb.id}  {kb.name}: ready ({kb.details})")
            kb.close()
        else:
            cannot_open = cannot_open or reason.startswith("cannot open")
            print(f"    {device_id(info)}  {device_name(info)}: {reason}")
    if mac:
        use = chosen is None and not cfg.vid and not cfg.pid
        chosen = mac.id if use else chosen
        print(f"  {'*' if use else ' '} {mac.id}  {mac.name}: ready ({mac.details})")
    if chosen:
        print(f"\nagent-glow uses {chosen} (marked *).")
    elif cfg.vid or cfg.pid:
        print(f"\nThe keyboard in config.json ({cfg.describe_keyboard()}) is not usable.")
    if cannot_open and sys.platform.startswith("linux"):
        print("\nLinux needs a udev rule before agent-glow may talk to the keyboard:")
        print("  agent-glow udev-rule | sudo tee /etc/udev/rules.d/60-agent-glow.rules")
        print("  sudo udevadm control --reload-rules && sudo udevadm trigger")
    return 0 if chosen else 1


def udev_rule(ids):
    lines = ["# agent-glow: let the logged-in user drive these keyboards' lighting over raw HID"]
    for vid, pid in ids:
        lines.append(f'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{{idVendor}}=="{vid:04x}", '
                     f'ATTRS{{idProduct}}=="{pid:04x}", MODE="0660", TAG+="uaccess"')
    if any(vid == LOGITECH_VID for vid, _pid in ids):
        lines.append(f'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", KERNELS=="0005:{LOGITECH_VID:04X}:*", '
                     'MODE="0660", TAG+="uaccess"')
    return "\n".join(lines) + "\n"


def cmd_udev_rule(args, hid_module=None):
    ids = []
    for arg in args:
        parsed = parse_vid_pid(arg)
        if not parsed:
            print("usage: agent-glow udev-rule [VID:PID ...]   e.g. agent-glow udev-rule 4d4c:6505",
                  file=sys.stderr)
            return 2
        ids.append(parsed)
    if not ids:
        try:
            hid_module = hid_module or load_hid()
            for info in via_interfaces(hid_module) + hidpp_interfaces(hid_module):
                pair = (info.get("vendor_id") or 0, info.get("product_id") or 0)
                if pair not in ids:
                    ids.append(pair)
        except ImportError:
            pass
    if not ids:
        print("No VIA keyboard found; pass its VID:PID (see `lsusb`), e.g. agent-glow udev-rule 4d4c:6505",
              file=sys.stderr)
        return 1
    sys.stdout.write(udev_rule(ids))
    return 0


def run_ticks(daemon, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(daemon.tick())


def cmd_test(args):
    what = args[0] if args else "all"
    if what not in (*AGENTS, "all", "done"):
        print("usage: agent-glow test [claude|codex|copilot|all|done] [seconds]")
        return 2
    seconds = num(args[1], 5, 1, 300) if len(args) > 1 else 5
    root = state_root()
    store, cfg = Store(root), load_config(root)
    enabled = [name for name in AGENTS if name in cfg.agents]
    if what == "done":
        if not cfg.done_enabled:
            print('the "done" flash is off; set "done": {"enabled": true} in config.json to try it')
            return 1
        agents, state, label = enabled[:1], DONE, "done"
    else:
        agents, state = [name for name in (AGENTS if what == "all" else (what,)) if name in enabled], NEEDS_INPUT
        label = ", ".join(agents) + " waiting"
    if not agents:
        print("nothing to test: that agent is switched off in config.json")
        return 1
    for name in agents:
        store.put(name, TEST_SESSION, MAIN, state, "test")
    lock = acquire_lock(root)
    try:
        if lock is None:
            print(f"daemon is running; blinking {label} for {seconds:g}s")
            time.sleep(seconds)
            return 0
        print(f"no daemon running; blinking {label} for {seconds:g}s from this process")
        daemon = Daemon(store)
        run_ticks(daemon, seconds)
        for name in agents:
            store.drop(name, TEST_SESSION)
        run_ticks(daemon, 2)
        daemon.shutdown()
        return 0
    finally:
        if lock is not None:
            remove(lock_paths(root)[1])
            os.close(lock)
        for name in agents:
            store.drop(name, TEST_SESSION)


def cmd_restore(_args):
    root = state_root()
    store = Store(root)
    if not store.has_snapshot():
        print("nothing to restore")
        return 0
    lock = acquire_lock(root)
    if lock is None:
        print("the daemon is running and restores the lighting by itself")
        return 0
    try:
        daemon = Daemon(store)
        run_ticks(daemon, 3)
        daemon.shutdown()
    finally:
        remove(lock_paths(root)[1])
        os.close(lock)
    done = not store.has_snapshot()
    print("restored" if done else "could not restore (keyboard missing?)")
    return 0 if done else 1


def cmd_version(_args):
    print(f"agent-glow {__version__}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"hook": cmd_hook, "daemon": cmd_daemon, "status": cmd_status, "test": cmd_test,
                "restore": cmd_restore, "stop": cmd_stop, "devices": cmd_devices,
                "udev-rule": cmd_udev_rule, "version": cmd_version, "--version": cmd_version}
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not argv or argv[0] not in commands:
        print(__doc__)
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
