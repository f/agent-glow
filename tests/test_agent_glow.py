import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import agent_glow as ag  # noqa: E402

SCRIPT = os.path.join(ROOT, "agent_glow.py")
REAL_DAEMON = ag.Daemon
REAL_MAC_FIND = ag.MacBacklight.__dict__["find"].__func__
ORIGINAL = {"effect": 24, "brightness": 255, "speed": 3, "hue": 170, "sat": 255}
ZED65 = "4d4c:6505"
SNAP = dict(ORIGINAL, device=ZED65)
RED, BLUE, GREEN = (0, 255), (170, 255), (85, 255)
NAMES = {ag.ID_BRIGHTNESS: "brightness", ag.ID_EFFECT: "effect", ag.ID_SPEED: "speed"}


def setUpModule():
    patcher = mock.patch.object(ag.MacBacklight, "find", return_value=None)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeVia:
    """Emulates QMK's VIA handlers: the protocol version and lighting get/set on its channels.

    Like real firmware, a channel-aware keyboard (protocol 12+) answers 0xFF for a channel it does
    not have, while very old firmware (echo_unknown) sends unknown requests back unchanged.
    max_brightness emulates RGB_MATRIX_MAXIMUM_BRIGHTNESS, which makes brightness round-trip lossy.
    """

    def __init__(self, state=None, protocol=12, channels=(ag.CH_RGBLIGHT,), max_brightness=255,
                 echo_unknown=False):
        self.state = dict(state or ORIGINAL)
        self.protocol = protocol
        self.channels = set(channels)
        self.max_brightness = max_brightness
        self.echo_unknown = echo_unknown
        self.writes = []  # ("effect", 1), ("color", (0, 255)), ...
        self.raw = []  # first two bytes of every command seen
        self.alive = True
        self.closed = False
        self.refuse_open = None
        self.on_set = None
        self.ignore = {}  # name -> number of writes to silently ignore
        self.bad_echo = False
        self.reply = None

    def write(self, data):
        if not self.alive:
            raise OSError("unplugged")
        assert data[0] == 0 and len(data) == 33
        cmd = bytes(data[1:])
        self.raw.append(cmd[:2])
        out = bytearray(cmd)
        lighting = cmd[0] in (ag.CMD_GET, ag.CMD_SET)
        if cmd[0] == ag.CMD_PROTOCOL:
            out[1], out[2] = self.protocol >> 8, self.protocol & 0xFF
        elif lighting and cmd[1] in self.channels:
            (self.get if cmd[0] == ag.CMD_GET else self.set)(cmd, out)
        elif not (lighting and self.echo_unknown):
            out[0] = ag.UNHANDLED
        if self.bad_echo:
            out[0] = 0x55
        self.reply = bytes(out)
        return len(data)

    def get(self, cmd, out):
        if cmd[2] == ag.ID_COLOR:
            out[3], out[4] = self.state["hue"], self.state["sat"]
        elif cmd[2] == ag.ID_BRIGHTNESS:  # via.c: val * 255 / RGB_MATRIX_MAXIMUM_BRIGHTNESS
            out[3] = self.state["brightness"] * 255 // self.max_brightness
        else:
            out[3] = self.state[NAMES[cmd[2]]]

    def set(self, cmd, out):
        if self.on_set:
            self.on_set()
        if cmd[2] == ag.ID_COLOR:
            self.writes.append(("color", (cmd[3], cmd[4])))
            self.state["hue"], self.state["sat"] = cmd[3], cmd[4]
            return
        name, value = NAMES[cmd[2]], cmd[3]
        self.writes.append((name, value))
        if name == "brightness":  # via.c: scale8(val, RGB_MATRIX_MAXIMUM_BRIGHTNESS)
            value = value * (1 + self.max_brightness) >> 8
        if self.ignore.get(name):
            self.ignore[name] -= 1
        else:
            self.state[name] = value

    def read(self, size, timeout):
        if not self.alive:
            raise OSError("unplugged")
        reply, self.reply = self.reply, None
        return list(reply) if reply else []


class FakeHid:
    """The hid / hidraw module: every keyboard shows a keyboard interface and a VIA interface."""

    def __init__(self, via=None):
        self.keyboards = []  # (info, FakeVia or None)
        self.present = True
        if via:
            self.add(via)

    def add(self, via, vid=0x4D4C, pid=0x6505, product="Zed65 Rev1"):
        n = len(self.keyboards)
        base = {"vendor_id": vid, "product_id": pid, "manufacturer_string": "Maker", "product_string": product}
        self.keyboards.append((dict(base, path=b"kbd%d" % n, usage_page=0x0001, usage=0x06), None))
        self.keyboards.append((dict(base, path=b"via%d" % n, usage_page=0xFF60, usage=0x61), via))
        return via

    def enumerate(self, vendor_id=0, product_id=0):
        if not self.present:
            return []
        return [dict(info) for info, _via in self.keyboards
                if (not vendor_id or info["vendor_id"] == vendor_id)
                and (not product_id or info["product_id"] == product_id)]

    def device(self):
        return FakeDevice(self)


class FakeDevice:
    def __init__(self, hid):
        self.hid, self.via = hid, None

    def open_path(self, path):
        for info, via in self.hid.keyboards:
            if info["path"] == path and via is not None and self.hid.present:
                if via.refuse_open:
                    raise OSError(via.refuse_open)
                via.closed = False
                self.via = via
                return
        raise OSError("open failed")

    def write(self, data):
        return self.via.write(data)

    def read(self, size, timeout):
        return self.via.read(size, timeout)

    def close(self):
        self.via.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def patch(case, *args, **kwargs):
    patcher = mock.patch.object(*args, **kwargs)
    patcher.start()
    case.addCleanup(patcher.stop)


def temp_dir(case):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    return tmp.name


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.store = ag.Store(self.root)
        self.clock = Clock()
        self.via = FakeVia()
        self.hid = FakeHid(self.via)
        self.opens = 0
        self.logs = []
        self.config({"blink_seconds": 0.5})

    def write_config(self, data):
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump(data, f)

    def config(self, data):
        self.write_config(data)
        self.d = ag.Daemon(self.store, self.open_keyboard, wall=self.clock, mono=self.clock,
                           logger=self.logs.append)

    def open_keyboard(self, cfg):
        self.opens += 1
        return ag.Keyboard.open(self.hid, cfg.vid, cfg.pid, cfg.lighting)

    def need(self, agent="claude", session="s1", agent_id="main", tool="Bash"):
        self.store.put(agent, session, agent_id, ag.NEEDS_INPUT, tool, now=self.clock())

    def done(self, agent="claude", session="s1"):
        self.store.put(agent, session, "main", ag.DONE, now=self.clock())

    def advance(self, seconds):
        for _ in range(int(seconds / 0.25)):
            self.clock.t += 0.25
            self.d.tick()

    def brightness_writes(self):
        return [v for k, v in self.via.writes if k == "brightness"]

    def color_writes(self):
        return [v for k, v in self.via.writes if k == "color"]


class AlertCycle(DaemonCase):
    def test_idle_never_touches_the_keyboard(self):
        self.advance(5)
        self.assertEqual(self.opens, 0)
        self.assertEqual(self.via.raw, [])

    def test_needs_input_blinks_red_and_restores_exactly(self):
        self.need()
        self.d.tick()
        self.assertEqual(self.via.writes[:3], [("effect", 1), ("color", (0, 255)), ("brightness", 255)])
        self.advance(2)
        self.assertEqual(self.brightness_writes()[:5], [255, 0, 255, 0, 255])
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual(self.via.state, ORIGINAL)
        self.assertFalse(self.store.has_snapshot())
        self.assertIsNone(self.d.alert)
        self.assertTrue(self.via.closed)

    def test_snapshot_is_saved_with_the_keyboard_id_before_the_first_lighting_change(self):
        seen = []
        self.via.on_set = lambda: seen.append(self.store.load_snapshot())
        self.need()
        self.d.tick()
        self.assertEqual(seen[0], SNAP)

    def test_only_non_saving_rgb_writes_are_sent(self):
        self.need()
        self.advance(2)
        self.store.drop("claude", "s1")
        self.advance(1)
        self.assertTrue(self.via.raw)
        for cmd in self.via.raw:
            self.assertIn(cmd, (bytes([0x01, 0x00]), bytes([0x07, 2]), bytes([0x08, 2])))

    def test_restore_order_ends_with_effect_then_speed(self):
        self.need()
        self.d.tick()
        n = len(self.via.writes)
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual(self.via.writes[n:], [
            ("color", (170, 255)), ("brightness", 255), ("effect", 24), ("speed", 3)])

    def test_rgb_that_was_off_is_turned_off_again(self):
        self.via.state["effect"] = 0
        self.need()
        self.d.tick()
        self.assertEqual(self.via.state["effect"], 1)
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual(self.via.state["effect"], 0)

    def test_restore_is_retried_once_when_the_keyboard_does_not_match(self):
        self.need()
        self.d.tick()
        self.via.ignore["effect"] = 1  # the first restore write is lost
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual([v for k, v in self.via.writes if k == "effect"], [1, 24, 24])
        self.assertEqual(self.via.state, ORIGINAL)
        self.assertFalse(self.store.has_snapshot())
        self.assertFalse(any("mismatch" in m for m in self.logs))

    def test_restore_gives_up_and_says_so_when_the_keyboard_keeps_ignoring_writes(self):
        self.need()
        self.d.tick()
        self.via.ignore["effect"] = 99
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertTrue(any("mismatch" in m for m in self.logs))
        self.assertFalse(self.store.has_snapshot())

    def test_done_flashes_green_then_expires(self):
        self.config({"blink_seconds": 0.5, "done": {"enabled": True}})
        self.done()
        self.d.tick()
        self.assertIn(("color", GREEN), self.via.writes)
        self.advance(4)
        self.assertIsNotNone(self.d.alert)
        self.advance(2)
        self.assertEqual(self.via.state, ORIGINAL)
        self.assertEqual(self.store.markers(), [])

    def test_needs_input_beats_done_and_done_resumes_afterwards(self):
        self.config({"blink_seconds": 0.5, "done": {"enabled": True}})
        self.done()
        self.d.tick()
        self.need(agent="codex", session="s2")
        self.d.tick()
        self.assertEqual(self.via.writes[-1], ("color", BLUE))
        self.assertEqual(self.d.alert["kind"], ag.NEEDS_INPUT)
        self.store.drop("codex", "s2")
        self.d.tick()
        self.assertEqual(self.d.alert["kind"], ag.DONE)
        self.assertEqual(self.via.state["hue"], 85)

    def test_two_sessions_waiting_alert_until_the_last_is_answered(self):
        self.need(session="a")
        self.need(session="b")
        self.d.tick()
        self.store.drop("claude", "a")
        self.advance(1)
        self.assertIsNotNone(self.d.alert)
        self.store.drop("claude", "b")
        self.d.tick()
        self.assertIsNone(self.d.alert)

    def test_stale_needs_input_marker_expires_after_ttl(self):
        self.need()
        self.d.tick()
        self.clock.t += 599
        self.d.tick()
        self.assertIsNotNone(self.d.alert)
        self.clock.t += 2
        self.d.tick()
        self.assertEqual(self.via.state, ORIGINAL)
        self.assertEqual(self.store.markers(), [])

    def test_corrupt_and_unknown_markers_are_cleaned_up_without_alerting(self):
        os.makedirs(self.store.sessions)
        with open(os.path.join(self.store.sessions, "bad.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.store.sessions, "list.json"), "w") as f:
            f.write("[]")
        self.store.put("claude", "x", "main", "mystery", now=self.clock())
        self.advance(1)
        self.assertEqual(self.opens, 0)
        self.assertEqual(self.store.markers(), [])

    def test_done_flash_is_off_unless_enabled(self):
        self.done()
        self.advance(1)
        self.assertEqual(self.opens, 0)
        self.config({"blink_seconds": 0.5, "done": {"enabled": False}})
        self.advance(1)
        self.assertEqual(self.opens, 0)
        self.config({"blink_seconds": 0.5, "done": {"enabled": True}})
        self.d.tick()
        self.assertIsNotNone(self.d.alert)

    def test_disabled_agents_are_ignored(self):
        self.config({"agents": ["claude"]})
        self.need(agent="codex")
        self.advance(1)
        self.assertEqual(self.opens, 0)
        self.need(agent="claude")
        self.d.tick()
        self.assertIsNotNone(self.d.alert)

    def test_config_changes_are_picked_up_while_running(self):
        self.d.tick()
        path = os.path.join(self.root, "config.json")
        with open(path, "w") as f:
            json.dump({"blink_seconds": 0.5, "needs_input": {"colors": {"claude": {"hue": 21, "sat": 200}}}}, f)
        os.utime(path, (self.clock.t + 10, self.clock.t + 10))
        self.need()
        self.d.tick()
        self.assertIn(("color", (21, 200)), self.via.writes)

    def test_bad_config_values_fall_back_to_defaults(self):
        c = ag.Config({"blink_seconds": "fast", "brightness": None, "needs_input": {"colors": "x"},
                       "done": "nope", "agents": 5, "keyboard": "x"})
        self.assertEqual((c.blink, c.brightness, c.done_color, c.done_enabled, c.agents),
                         (0.4, 255, GREEN, False, ag.AGENTS))
        self.assertEqual((c.vid, c.pid, c.lighting), (0, 0, "auto"))
        self.assertEqual([c.needs_input_color(a) for a in ag.AGENTS], [RED, BLUE, GREEN])


class AgentColors(DaemonCase):
    def color_for(self, agent):
        """Start an alert for one agent, return the color writes it made, then answer it."""
        self.via.writes.clear()
        self.need(agent=agent, session=agent)
        self.d.tick()
        colors = self.color_writes()
        self.store.drop(agent, agent)
        self.d.tick()
        self.assertEqual(self.via.state, ORIGINAL)
        return colors

    def test_default_colors_are_claude_red_codex_blue_copilot_green(self):
        c = ag.Config()
        self.assertEqual({a: c.needs_input_color(a) for a in ag.AGENTS},
                         {"claude": (0, 255), "codex": (170, 255), "copilot": (85, 255)})
        self.assertEqual(c.needs_input_color("somebody-else"), (0, 255))

    def test_each_agent_blinks_in_its_own_color(self):
        for agent, color in (("codex", BLUE), ("claude", RED), ("copilot", GREEN)):
            with self.subTest(agent=agent):
                self.via.writes.clear()
                self.need(agent=agent, session=agent)
                self.d.tick()
                self.assertEqual(self.via.writes, [("effect", 1), ("color", color), ("brightness", 255)])
                self.store.drop(agent, agent)
                self.d.tick()
                self.assertEqual(self.via.state, ORIGINAL)

    def test_colors_can_be_overridden_per_agent(self):
        self.config({"blink_seconds": 0.5,
                     "needs_input": {"colors": {"codex": {"hue": 200}, "claude": {"hue": 10, "sat": 100}}}})
        self.assertEqual([self.color_for(a) for a in ("codex", "claude", "copilot")],
                         [[(200, 255)], [(10, 100)], [GREEN]])

    def test_bad_per_agent_colors_fall_back_to_that_agents_default(self):
        c = ag.Config({"needs_input": {"colors": {"codex": {"hue": "x"}, "claude": 5,
                                                  "copilot": {"hue": 999, "sat": -4}, "extra": {"hue": 7}}}})
        self.assertEqual([c.needs_input_color(a) for a in ("codex", "claude", "copilot", "extra")],
                         [BLUE, RED, (255, 0), (7, 255)])
        self.assertEqual(ag.Config({"needs_input": {"colors": []}}).needs_input_color("codex"), BLUE)

    def test_a_single_waiting_agent_never_rewrites_its_color(self):
        self.need()
        self.advance(5)
        self.assertEqual(self.color_writes(), [RED])

    def test_several_waiting_agents_take_turns_in_agent_order(self):
        for agent in ("copilot", "claude", "codex"):
            self.need(agent=agent, session=agent)
        self.advance(2.5)
        self.assertEqual(self.via.writes, [
            ("effect", 1), ("color", RED), ("brightness", 255),
            ("brightness", 0),
            ("color", BLUE), ("brightness", 255),  # the next color is set while the LEDs are dark
            ("brightness", 0),
            ("color", GREEN), ("brightness", 255)])

    def test_the_cycle_wraps_around(self):
        self.need(agent="claude", session="a")
        self.need(agent="codex", session="b")
        self.advance(4)
        self.assertEqual(self.color_writes(), [RED, BLUE, RED, BLUE])

    def test_agents_sharing_a_color_do_not_cycle(self):
        self.config({"blink_seconds": 0.5, "needs_input": {"colors": {"codex": {"hue": 0}}}})
        self.need(agent="claude", session="a")
        self.need(agent="codex", session="b")
        self.advance(5)
        self.assertEqual(self.color_writes(), [RED])
        self.assertEqual(self.d.alert["agents"], ("claude", "codex"))

    def test_agents_sharing_a_color_take_one_turn_between_them(self):
        self.config({"blink_seconds": 0.5, "needs_input": {"colors": {"codex": {"hue": 0}}}})
        for agent in ag.AGENTS:
            self.need(agent=agent, session=agent)
        self.advance(2.5)
        self.assertEqual(self.color_writes(), [RED, GREEN, RED])

    def test_an_agent_that_starts_waiting_joins_at_the_next_flash(self):
        self.need()
        self.d.tick()
        self.need(agent="codex", session="b")
        self.d.tick()
        self.assertEqual(self.color_writes(), [RED])  # the flash in progress keeps its color
        self.assertEqual(self.d.alert["agents"], ("claude", "codex"))
        self.advance(1.5)
        self.assertEqual(self.color_writes(), [RED, BLUE])

    def test_an_agent_joining_mid_cycle_continues_the_cycle_instead_of_restarting_it(self):
        self.need(agent="claude", session="a")
        self.need(agent="codex", session="b")
        self.advance(1.5)
        self.assertEqual(self.color_writes(), [RED, BLUE])
        self.need(agent="copilot", session="c")
        self.advance(1.5)
        self.assertEqual(self.color_writes(), [RED, BLUE, GREEN])

    def test_when_the_shown_agent_stops_waiting_the_remaining_color_shows_at_once(self):
        self.need(agent="claude", session="a")
        self.need(agent="codex", session="b")
        self.d.tick()
        self.assertEqual(self.via.state["hue"], RED[0])
        self.store.drop("claude", "a")
        self.d.tick()
        self.assertEqual(self.via.state["hue"], BLUE[0])
        self.assertEqual(self.d.alert["agents"], ("codex",))
        self.advance(3)
        self.assertEqual(self.color_writes(), [RED, BLUE])

    def test_when_another_agent_stops_waiting_the_shown_color_stays(self):
        self.need(agent="claude", session="a")
        self.need(agent="codex", session="b")
        self.d.tick()
        self.store.drop("codex", "b")
        self.d.tick()
        self.advance(3)
        self.assertEqual(self.color_writes(), [RED])

    def test_a_color_change_in_config_reaches_a_running_alert(self):
        self.need()
        self.d.tick()
        self.write_config({"blink_seconds": 0.5, "needs_input": {"colors": {"claude": {"hue": 21, "sat": 200}}}})
        os.utime(os.path.join(self.root, "config.json"), (self.clock.t + 10, self.clock.t + 10))
        self.d.tick()
        self.assertEqual(self.via.writes[-1], ("color", (21, 200)))

    def test_the_log_names_the_waiting_agents_and_the_keyboard(self):
        self.need(agent="claude", session="a")
        self.need(agent="copilot", session="b")
        self.d.tick()
        self.assertIn("keyboard connected: 4d4c:6505 Maker Zed65 Rev1 (rgblight)", self.logs)
        self.assertIn("alert needs_input (claude, copilot)", self.logs)
        self.store.drop("claude", "a")
        self.d.tick()
        self.assertIn("alert needs_input (copilot)", self.logs)

    def test_markers_with_odd_agent_values_cannot_break_the_daemon(self):
        for agent in (["x"], {"a": 1}, 5, None):
            self.store.put(agent, "s", "main", ag.NEEDS_INPUT, now=self.clock.t)
        self.advance(1)
        self.assertEqual(self.d.alert["colors"], (RED,))


class OtherKeyboards(DaemonCase):
    def test_rgb_matrix_keyboards_blink_and_restore_despite_brightness_rounding(self):
        # RGB_MATRIX_MAXIMUM_BRIGHTNESS 200: brightness 127 reads back as 126 after a write.
        self.via = FakeVia(dict(ORIGINAL, brightness=100), channels=(ag.CH_RGB_MATRIX,), max_brightness=200)
        self.hid = FakeHid(self.via)
        self.need()
        self.d.tick()
        self.assertEqual(self.store.load_snapshot()["brightness"], 127)
        self.assertIn(bytes([0x07, ag.CH_RGB_MATRIX]), self.via.raw)
        self.assertNotIn(bytes([0x07, ag.CH_RGBLIGHT]), self.via.raw)
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual([v for k, v in self.via.writes if k == "effect"], [1, 24])  # no retry needed
        self.assertFalse(any("mismatch" in m for m in self.logs))
        self.assertFalse(self.store.has_snapshot())
        self.assertIn("keyboard connected: 4d4c:6505 Maker Zed65 Rev1 (rgb_matrix)", self.logs)

    def test_the_keyboard_named_in_config_is_used(self):
        other = self.hid.add(FakeVia(), 0x1234, 0x5678, "Other")
        self.config({"blink_seconds": 0.5, "keyboard": {"vid": "0x1234", "pid": "5678"}})
        self.need()
        self.d.tick()
        self.assertEqual((self.via.writes, other.writes[0]), ([], ("effect", 1)))
        self.assertEqual(self.store.load_snapshot()["device"], "1234:5678")

    def test_lighting_named_in_config_is_used(self):
        self.via.channels = {ag.CH_RGBLIGHT, ag.CH_RGB_MATRIX}
        self.config({"blink_seconds": 0.5, "keyboard": {"lighting": "rgb_matrix"}})
        self.need()
        self.d.tick()
        self.assertIn(bytes([0x07, ag.CH_RGB_MATRIX]), self.via.raw)

    def test_a_snapshot_of_another_keyboard_is_never_written_to_this_one(self):
        self.store.save_snapshot(dict(ORIGINAL, effect=5, device="1234:5678"))
        self.d.tick()
        self.assertEqual(self.via.writes, [])
        self.assertFalse(self.store.has_snapshot())
        self.assertTrue(any("belongs to keyboard 1234:5678" in m for m in self.logs))

    def test_an_alert_on_another_keyboard_snapshots_that_keyboard(self):
        self.store.save_snapshot(dict(ORIGINAL, effect=5, device="1234:5678"))
        self.need()
        self.d.tick()
        self.assertEqual(self.store.load_snapshot(), SNAP)
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual(self.via.state, ORIGINAL)

    def test_an_unusable_keyboard_is_reported_once_and_retried(self):
        self.via.protocol, self.via.echo_unknown = 9, True
        self.need()
        self.advance(5)
        self.assertEqual(sum("too old" in m for m in self.logs), 1)
        self.assertEqual(self.via.writes, [])
        self.assertTrue(self.via.closed)
        self.via.protocol, self.via.echo_unknown = 12, False  # firmware updated
        self.advance(3)
        self.assertIsNotNone(self.d.alert)


class AlertCommand(DaemonCase):
    """`agent_glow.py test`, with the fake keyboard and a fake clock instead of the real ones."""

    def run_test(self, *args, daemon_running=False):
        self.clock.t = time.time()  # the markers it writes are stamped with the real time
        made, out, slept = [], io.StringIO(), []

        def make_daemon(store):
            made.append(REAL_DAEMON(store, self.open_keyboard, wall=self.clock, mono=self.clock,
                                    logger=self.logs.append))
            return made[-1]

        def run_ticks(daemon, seconds):
            for _ in range(int(seconds / 0.25)):
                self.clock.t += 0.25
                daemon.tick()

        def sleep(seconds):
            slept.append((seconds, sorted(m["agent"] for _p, m in self.store.markers())))

        with mock.patch.dict(os.environ, {"AGENT_GLOW_HOME": self.root}), \
                mock.patch.object(ag, "Daemon", make_daemon), mock.patch.object(ag, "run_ticks", run_ticks), \
                mock.patch.object(ag.time, "sleep", sleep), contextlib.redirect_stdout(out):
            code = ag.cmd_test(list(args))
        self.assertEqual(self.store.markers(), [])  # the test never leaves a marker behind
        if not daemon_running:
            self.assertIsNone(ag.daemon_pid(self.root))  # its temporary lock is released
        return code, out.getvalue(), slept

    def test_default_shows_all_three_agents_taking_turns_and_restores_the_lighting(self):
        code, out, _ = self.run_test()
        self.assertEqual(code, 0)
        self.assertIn("claude, codex, copilot waiting", out)
        self.assertEqual(self.color_writes()[:4], [RED, BLUE, GREEN, RED])
        self.assertEqual(self.via.state, ORIGINAL)
        self.assertFalse(self.store.has_snapshot())

    def test_a_single_agent_can_be_tried(self):
        for agent, color in (("claude", RED), ("codex", BLUE), ("copilot", GREEN)):
            with self.subTest(agent=agent):
                self.via.writes.clear()
                code, out, _ = self.run_test(agent, "1")
                self.assertEqual(code, 0)
                self.assertIn(f"{agent} waiting", out)
                self.assertEqual(self.color_writes()[0], color)
                self.assertEqual(self.via.state, ORIGINAL)

    def test_with_a_running_daemon_it_only_leaves_markers_for_it_to_show(self):
        lock = ag.acquire_lock(self.root)
        try:
            code, out, slept = self.run_test("copilot", "3", daemon_running=True)
        finally:
            os.close(lock)
        self.assertEqual((code, slept), (0, [(3.0, ["copilot"])]))
        self.assertIn("daemon is running", out)
        self.assertEqual(self.opens, 0)

    def test_done_needs_to_be_switched_on_first(self):
        code, out, _ = self.run_test("done", "1")
        self.assertEqual(code, 1)
        self.assertIn("enabled", out)
        self.assertEqual(self.opens, 0)
        self.write_config({"blink_seconds": 0.5, "done": {"enabled": True}})
        code, out, _ = self.run_test("done", "1")
        self.assertEqual(code, 0)
        self.assertEqual(self.color_writes()[0], GREEN)
        self.assertEqual(self.via.state, ORIGINAL)

    def test_agents_switched_off_in_config_are_skipped(self):
        self.write_config({"blink_seconds": 0.5, "agents": ["codex"]})
        code, out, _ = self.run_test("claude")
        self.assertEqual((code, self.opens), (1, 0))
        self.assertIn("switched off", out)
        code, out, _ = self.run_test("all", "1")
        self.assertEqual(code, 0)
        self.assertIn("codex waiting", out)
        self.assertNotIn("claude", out)

    def test_unknown_argument_prints_usage(self):
        code, out, _ = self.run_test("nonsense")
        self.assertEqual(code, 2)
        self.assertIn("usage", out)
        self.assertEqual(self.opens, 0)


class Resilience(DaemonCase):
    def test_missing_keyboard_is_retried_with_backoff_and_logged_once(self):
        self.hid.present = False
        self.need()
        self.advance(5)
        self.assertLessEqual(self.opens, 3)
        self.assertEqual(sum("not available" in m for m in self.logs), 1)
        self.hid.present = True
        self.advance(3)
        self.assertIsNotNone(self.d.alert)

    def test_unplug_during_alert_recovers_and_keeps_the_original_snapshot(self):
        self.need()
        self.advance(1)
        self.via.alive = False
        self.advance(1)
        self.assertIsNone(self.d.alert)
        self.assertEqual(self.store.load_snapshot(), SNAP)
        self.via.alive = True  # keyboard kept its alert lighting (not a power cycle)
        self.advance(3)
        self.assertIsNotNone(self.d.alert)
        self.assertEqual(self.store.load_snapshot(), SNAP)
        self.store.drop("claude", "s1")
        self.advance(1)
        self.assertEqual(self.via.state, ORIGINAL)

    def test_leftover_snapshot_from_a_crash_is_restored_on_start(self):
        for snapshot in (SNAP, ORIGINAL):  # ORIGINAL: written before snapshots named their keyboard
            with self.subTest(snapshot=snapshot):
                self.store.save_snapshot(snapshot)
                self.via.state.update(effect=1, hue=0, brightness=0)
                self.d.tick()
                self.assertEqual(self.via.state, ORIGINAL)
                self.assertFalse(self.store.has_snapshot())

    def test_leftover_snapshot_is_reused_when_an_alert_is_still_wanted(self):
        self.store.save_snapshot(SNAP)
        self.via.state.update(effect=1, hue=0, brightness=0)
        self.need()
        self.d.tick()
        self.assertEqual(self.store.load_snapshot(), SNAP)
        self.store.drop("claude", "s1")
        self.d.tick()
        self.assertEqual(self.via.state, ORIGINAL)

    def test_corrupt_snapshot_is_discarded(self):
        with open(self.store.snapshot_path, "w") as f:
            f.write("{oops")
        self.d.tick()
        self.assertFalse(self.store.has_snapshot())

    def test_shutdown_restores_lighting(self):
        self.need()
        self.advance(1)
        self.d.shutdown()
        self.assertEqual(self.via.state, ORIGINAL)
        self.assertFalse(self.store.has_snapshot())

    def test_shutdown_without_keyboard_keeps_snapshot_for_next_start(self):
        self.need()
        self.advance(1)
        self.via.alive = False
        self.d.shutdown()
        self.assertEqual(self.store.load_snapshot(), SNAP)

    def test_alert_after_a_failed_restore_is_applied_again_on_reconnect(self):
        self.need()
        self.advance(1)
        self.hid.present = False
        self.via.alive = False
        self.store.drop("claude", "s1")
        self.advance(1)  # restore cannot reach the keyboard
        self.assertTrue(self.store.has_snapshot())
        self.via.alive = True
        self.hid.present = True
        self.need()
        self.advance(4)
        self.assertEqual(self.via.state["effect"], 1)
        self.assertEqual(self.via.state["hue"], 0)
        self.assertEqual(self.store.load_snapshot(), SNAP)

    def test_missing_hidapi_is_logged_and_retried(self):
        def no_hid(_cfg):
            raise ImportError("No module named 'hid'")
        self.d.open_keyboard = no_hid
        self.need()
        self.advance(3)
        self.assertEqual(sum("No module named" in m for m in self.logs), 1)
        self.assertIsNone(self.d.alert)


class KeyboardProtocol(unittest.TestCase):
    def open(self, *vias, **kwargs):
        hid = FakeHid()
        for i, via in enumerate(vias):
            hid.add(via, 0x4D4C + i, 0x6505 + i)
        return ag.Keyboard.open(hid, **kwargs)

    def assertUnusable(self, via, message, **kwargs):
        with self.assertRaises(ag.KeyboardError) as caught:
            self.open(via, **kwargs)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(via.writes, [])
        return str(caught.exception)

    def test_open_skips_non_via_interfaces(self):
        kb = self.open(FakeVia())
        self.assertIsNotNone(kb)
        self.assertEqual((kb.id, kb.name, kb.protocol, kb.lighting), (ZED65, "Maker Zed65 Rev1", 12, "rgblight"))
        self.assertEqual(kb.read_state(), ORIGINAL)

    def test_open_returns_none_without_a_keyboard(self):
        self.assertIsNone(self.open())

    def test_rgb_matrix_is_used_when_there_is_no_rgblight(self):
        via = FakeVia(channels=(ag.CH_RGB_MATRIX,))
        kb = self.open(via)
        self.assertEqual((kb.lighting, kb.read_state()), ("rgb_matrix", ORIGINAL))
        kb.alert(0, 255, 255)
        self.assertEqual(via.writes, [("effect", 1), ("color", (0, 255)), ("brightness", 255)])

    def test_rgblight_wins_when_both_exist_unless_configured(self):
        via = FakeVia(channels=(ag.CH_RGBLIGHT, ag.CH_RGB_MATRIX))
        self.assertEqual(self.open(via).lighting, "rgblight")
        self.assertEqual(self.open(via, lighting="rgb_matrix").lighting, "rgb_matrix")

    def test_configured_lighting_must_exist(self):
        self.assertUnusable(FakeVia(), "does not expose rgb_matrix", lighting="rgb_matrix")

    def test_keyboard_without_rgb_is_reported(self):
        self.assertUnusable(FakeVia(channels=()), "does not expose RGB Light or RGB Matrix")

    def test_old_firmware_is_refused_before_any_write(self):
        message = self.assertUnusable(FakeVia(protocol=9, echo_unknown=True), "too old")
        self.assertIn("4d4c:6505 Maker Zed65 Rev1", message)

    def test_protocol_11_is_accepted_only_if_it_has_lighting_channels(self):
        self.assertEqual(self.open(FakeVia(protocol=11)).lighting, "rgblight")
        self.assertUnusable(FakeVia(protocol=11, echo_unknown=True), "too old")

    def test_a_device_that_does_not_speak_via_is_refused(self):
        via = FakeVia()
        via.bad_echo = True
        self.assertUnusable(via, "does not answer VIA commands")

    def test_permission_problems_name_the_cause(self):
        via = FakeVia()
        via.refuse_open = "open failed: Permission denied"
        with mock.patch.object(ag.sys, "platform", "darwin"):
            message = self.assertUnusable(via, "cannot open it: open failed: Permission denied")
        self.assertNotIn("udev", message)
        with mock.patch.object(ag.sys, "platform", "linux"):
            self.assertIn("udev-rule", self.assertUnusable(via, "cannot open it"))

    def test_first_usable_keyboard_wins_and_unusable_ones_are_closed(self):
        old, good = FakeVia(protocol=9, echo_unknown=True), FakeVia()
        kb = self.open(old, good)
        self.assertEqual(kb.id, "4d4d:6506")
        self.assertTrue(old.closed)
        self.assertFalse(good.closed)

    def test_vid_and_pid_select_a_keyboard(self):
        first, second = FakeVia(), FakeVia()
        self.assertEqual(self.open(first, second, vid=0x4D4D, pid=0x6506).id, "4d4d:6506")
        self.assertEqual(self.open(first, second, vid=0x4D4C).id, "4d4c:6505")
        self.assertIsNone(self.open(first, second, vid=0xDEAD))

    def test_unexpected_reply_is_an_error(self):
        via = FakeVia()
        kb = self.open(via)
        via.bad_echo = True
        with self.assertRaises(ag.KeyboardError):
            kb.get(ag.ID_EFFECT)
        with self.assertRaises(ag.KeyboardError):
            kb.set(ag.ID_EFFECT, 1)

    def test_silent_keyboard_is_an_error(self):
        via = FakeVia()
        kb = self.open(via)
        via.read = lambda size, timeout: []
        with self.assertRaises(ag.KeyboardError):
            kb.get(ag.ID_EFFECT)

    def test_same_lighting_tolerates_only_brightness_rounding(self):
        self.assertTrue(ag.same_lighting(dict(ORIGINAL, brightness=253), ORIGINAL))
        self.assertFalse(ag.same_lighting(dict(ORIGINAL, brightness=252), ORIGINAL))
        for key in ("effect", "speed", "hue", "sat"):
            self.assertFalse(ag.same_lighting(dict(ORIGINAL, **{key: ORIGINAL[key] - 1}), ORIGINAL), key)


class ConfigValues(unittest.TestCase):
    def test_keyboard_section(self):
        c = ag.Config({"keyboard": {"vid": "0x4D4C", "pid": 25861, "lighting": "rgb_matrix"}})
        self.assertEqual((c.vid, c.pid, c.lighting), (0x4D4C, 0x6505, "rgb_matrix"))
        self.assertEqual(c.describe_keyboard(), "4d4c:6505, lighting rgb_matrix")
        c = ag.Config({"keyboard": {"vid": "nope", "pid": True, "lighting": "lasers"}})
        self.assertEqual((c.vid, c.pid, c.lighting), (0, 0, "auto"))
        self.assertEqual(c.describe_keyboard(), "first usable keyboard found, lighting auto")

    def test_usb_ids(self):
        self.assertEqual([ag.usb_id(v) for v in ("4d4c", "0x4D4C", " 4d4c ", 19788, 70000, -1, None, 1.5, True)],
                         [0x4D4C, 0x4D4C, 0x4D4C, 0x4D4C, 0, 0, 0, 0, 0])
        self.assertEqual(ag.parse_vid_pid("4d4c:6505"), (0x4D4C, 0x6505))
        self.assertEqual(ag.parse_vid_pid("0x4D4C : 0x6505"), (0x4D4C, 0x6505))
        for bad in ("4d4c", "4d4c:", "12345:1", "xyz:1", "", None):
            self.assertIsNone(ag.parse_vid_pid(bad), bad)


class Commands(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        patcher = mock.patch.dict(os.environ, {"AGENT_GLOW_HOME": self.root})
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(list(args), **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_devices_lists_keyboards_and_marks_the_one_in_use(self):
        hid = FakeHid(FakeVia())
        hid.add(FakeVia(protocol=9, echo_unknown=True), 0x1234, 0x5678, "Old")
        hid.add(FakeVia(channels=(ag.CH_RGB_MATRIX,)), 0xAAAA, 0xBBBB, "Matrix")
        code, out, _ = self.call(ag.cmd_devices, hid_module=hid)
        self.assertEqual(code, 0)
        self.assertIn("* 4d4c:6505  Maker Zed65 Rev1: ready (VIA protocol 12, rgblight)", out)
        self.assertIn("1234:5678  Maker Old: its VIA protocol 9 is too old", out)
        self.assertIn("  aaaa:bbbb  Maker Matrix: ready (VIA protocol 12, rgb_matrix)", out)
        self.assertIn("agent-glow uses 4d4c:6505", out)
        self.assertTrue(all(via.closed for _info, via in hid.keyboards if via))

    def test_devices_follows_the_keyboard_in_config(self):
        hid = FakeHid(FakeVia())
        hid.add(FakeVia(), 0xAAAA, 0xBBBB, "Second")
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"keyboard": {"vid": "aaaa", "pid": "bbbb"}}, f)
        code, out, _ = self.call(ag.cmd_devices, hid_module=hid)
        self.assertEqual(code, 0)
        self.assertIn("* aaaa:bbbb", out)
        self.assertIn("    4d4c:6505", out)
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"keyboard": {"vid": "dead", "pid": "beef"}}, f)
        code, out, _ = self.call(ag.cmd_devices, hid_module=hid)
        self.assertEqual(code, 1)
        self.assertIn("dead:beef, lighting auto) is not usable", out)

    def test_devices_without_a_keyboard_or_without_hidapi(self):
        code, out, _ = self.call(ag.cmd_devices, hid_module=FakeHid())
        self.assertEqual(code, 1)
        self.assertIn("No VIA keyboard found", out)

        def missing():
            raise ImportError("No module named 'hid'")
        with mock.patch.object(ag, "load_hid", missing):
            code, out, _ = self.call(ag.cmd_devices)
        self.assertEqual(code, 1)
        self.assertIn("hidapi is not available", out)

    def test_devices_explains_udev_on_linux(self):
        via = FakeVia()
        via.refuse_open = "open failed"
        with mock.patch.object(ag.sys, "platform", "linux"):
            code, out, _ = self.call(ag.cmd_devices, hid_module=FakeHid(via))
        self.assertEqual(code, 1)
        self.assertIn("agent-glow udev-rule | sudo tee /etc/udev/rules.d/60-agent-glow.rules", out)

    def test_udev_rule_from_arguments_or_from_connected_keyboards(self):
        code, out, _ = self.call(ag.cmd_udev_rule, "4D4C:6505", "0x1234:0xabcd")
        self.assertEqual(code, 0)
        rules = [line for line in out.splitlines() if not line.startswith("#")]
        self.assertEqual(rules, [
            'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="4d4c", ATTRS{idProduct}=="6505", '
            'MODE="0660", TAG+="uaccess"',
            'KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1234", ATTRS{idProduct}=="abcd", '
            'MODE="0660", TAG+="uaccess"'])
        hid = FakeHid(FakeVia())
        hid.add(FakeVia(), 0x1234, 0xABCD)
        code, auto, _ = self.call(ag.cmd_udev_rule, hid_module=hid)
        self.assertEqual((code, auto), (0, out))

    def test_udev_rule_errors(self):
        code, _out, err = self.call(ag.cmd_udev_rule, "nonsense")
        self.assertEqual(code, 2)
        self.assertIn("usage", err)
        code, _out, err = self.call(ag.cmd_udev_rule, hid_module=FakeHid())
        self.assertEqual(code, 1)
        self.assertIn("No VIA keyboard found", err)

    def test_status_lists_pending_markers(self):
        ag.Store(self.root).put("codex", "s9", "main", ag.NEEDS_INPUT, "Bash")
        code, out, _ = self.call(ag.cmd_status)
        self.assertEqual(code, 0)
        self.assertIn(f"state dir : {self.root}", out)
        self.assertIn("markers   : 1", out)
        self.assertIn("codex  needs_input  tool=Bash", out)

    def test_restore_without_a_snapshot_does_nothing(self):
        code, out, _ = self.call(ag.cmd_restore)
        self.assertEqual((code, out), (0, "nothing to restore\n"))

    def test_help_version_and_unknown_commands(self):
        self.assertEqual(self.call(lambda a: ag.main(a), "--help")[:2], (0, ag.__doc__ + "\n"))
        self.assertEqual(self.call(lambda a: ag.main(a), "version")[:2], (0, f"agent-glow {ag.__version__}\n"))
        self.assertEqual(self.call(lambda a: ag.main(a))[0], 2)
        self.assertEqual(self.call(lambda a: ag.main(a), "explode")[0], 2)


class DaemonLock(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)

    def test_only_one_daemon_holds_the_lock_and_its_pid_is_known(self):
        self.assertIsNone(ag.daemon_pid(self.root))
        lock = ag.acquire_lock(self.root)
        try:
            self.assertIsNotNone(lock)
            self.assertEqual(ag.daemon_pid(self.root), os.getpid())
            self.assertIsNone(ag.acquire_lock(self.root))
            with open(ag.lock_paths(self.root)[1], "w") as f:
                f.write("garbage")
            self.assertEqual(ag.daemon_pid(self.root), -1)
        finally:
            os.close(lock)
        self.assertIsNone(ag.daemon_pid(self.root))
        os.close(ag.acquire_lock(self.root))

    def test_stop_without_a_daemon(self):
        with mock.patch.dict(os.environ, {"AGENT_GLOW_HOME": self.root}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ag.cmd_stop([]), 0)
        self.assertIn("not running", out.getvalue())


class DaemonProcess(unittest.TestCase):
    """The real daemon as a process. It never touches a keyboard while no agent is waiting."""

    def setUp(self):
        self.root = temp_dir(self)
        self.env = dict(os.environ, AGENT_GLOW_HOME=self.root)

    def start(self):
        p = subprocess.Popen([sys.executable, SCRIPT, "daemon"], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        end = time.monotonic() + 15
        while time.monotonic() < end and ag.daemon_pid(self.root) != p.pid:
            self.assertIsNone(p.poll(), "daemon exited")
            time.sleep(0.05)
        self.assertEqual(ag.daemon_pid(self.root), p.pid)
        return p

    def log(self):
        with open(os.path.join(self.root, "daemon.log"), encoding="utf-8") as f:
            return f.read()

    def test_a_second_daemon_exits_and_stop_ends_the_first(self):
        p = self.start()
        second = subprocess.run([sys.executable, SCRIPT, "daemon"], env=self.env, capture_output=True, timeout=15)
        self.assertEqual(second.returncode, 0)
        self.assertIn("already running (pid %d)" % p.pid, self.log())
        r = subprocess.run([sys.executable, SCRIPT, "stop"], env=self.env, capture_output=True, text=True,
                           timeout=20)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("daemon stopped", r.stdout)
        self.assertEqual(p.wait(timeout=10), 0)
        self.assertIsNone(ag.daemon_pid(self.root))
        self.assertTrue(self.log().rstrip().endswith("daemon stopped"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "stop")))

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_stops_the_daemon_cleanly(self):
        p = self.start()
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=10), 0)
        self.assertIn("daemon stopped", self.log())


class StdinReading(unittest.TestCase):
    def pipe(self):
        r, w = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r, w) if self.is_open(fd)])
        return r, w

    @staticmethod
    def is_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    def readers(self):
        yield "threaded", ag.read_threaded
        if os.name != "nt":
            yield "select", ag.read_select

    def test_payload_is_read_until_end_of_file(self):
        for name, reader in self.readers():
            with self.subTest(reader=name):
                r, w = self.pipe()
                os.write(w, b'{"session_id": "s"}')
                os.close(w)
                self.assertEqual(reader(r, 1 << 20, 5), (b'{"session_id": "s"}', False))

    def test_an_agent_that_keeps_stdin_open_does_not_block_the_hook(self):
        for name, reader in self.readers():
            with self.subTest(reader=name):
                r, w = self.pipe()
                os.write(w, b"{}")
                start = time.monotonic()
                data, pending = reader(r, 1 << 20, 0.3)
                self.assertLess(time.monotonic() - start, 3)
                self.assertTrue(pending)
                self.assertIn(data, (b"", b"{}"))
                os.close(w)  # lets a blocked reader thread finish
                time.sleep(0.05)


class Logging(unittest.TestCase):
    def test_log_file_is_rotated(self):
        root = temp_dir(self)
        path = os.path.join(root, "daemon.log")
        logger = ag.Logger(path, echo=False)
        with mock.patch.object(ag, "LOG_MAX_BYTES", 50):
            for i in range(5):
                logger(f"line {i} " + "x" * 20)
        with open(path) as f:
            current = f.read()
        with open(path + ".1") as f:
            rotated = f.read()
        self.assertIn("line 4", current)
        self.assertIn("line 3", rotated)
        self.assertNotIn("line 3", current)
        self.assertNotIn("line 0", current + rotated)  # one old generation is kept, not more

    def test_a_missing_stdout_does_not_break_logging(self):
        with mock.patch.object(ag.sys, "stdout", None):
            ag.Logger(None, echo=True)("hello")


class HookEvents(unittest.TestCase):
    def setUp(self):
        self.store = ag.Store(temp_dir(self))

    def hook(self, agent, action, **payload):
        ag.handle_hook(self.store, agent, action, payload, now=1000.0)

    def states(self):
        return sorted((m["agent"], m["agent_id"], m["state"]) for _p, m in self.store.markers())

    def test_claude_permission_request_then_matching_post_tool_use_clears(self):
        self.hook("claude", "needs-input", session_id="s", tool_name="Bash", cwd="/x")
        self.assertEqual(self.states(), [("claude", "main", "needs_input")])
        self.hook("claude", "resume", session_id="s", tool_name="Bash")
        self.assertEqual(self.states(), [])

    def test_resume_for_a_different_tool_keeps_the_alert(self):
        self.hook("claude", "needs-input", session_id="s", tool_name="Bash")
        self.hook("claude", "resume", session_id="s", tool_name="Read")
        self.assertEqual(self.states(), [("claude", "main", "needs_input")])

    def test_resume_without_a_known_tool_clears(self):
        self.hook("claude", "needs-input", session_id="s")
        self.hook("claude", "resume", session_id="s", tool_name="Read")
        self.assertEqual(self.states(), [])
        self.hook("claude", "needs-input", session_id="s", tool_name="Bash")
        self.hook("claude", "resume", session_id="s")
        self.assertEqual(self.states(), [])

    def test_subagents_do_not_clear_each_other_or_the_main_agent(self):
        self.hook("claude", "needs-input", session_id="s", tool_name="Bash")
        self.hook("claude", "needs-input", session_id="s", agent_id="sub1", tool_name="Bash")
        self.hook("claude", "needs-input", session_id="s", agent_id="sub2", tool_name="Bash")
        self.hook("claude", "resume", session_id="s", agent_id="sub1", tool_name="Bash")
        self.assertEqual(self.states(), [("claude", "main", "needs_input"), ("claude", "sub2", "needs_input")])

    def test_sessions_are_independent(self):
        self.hook("claude", "needs-input", session_id="a", tool_name="Bash")
        self.hook("claude", "needs-input", session_id="b", tool_name="Bash")
        self.hook("claude", "clear", session_id="a")
        self.assertEqual([m["session"] for _p, m in self.store.markers()], ["b"])

    def test_session_ids_that_prefix_each_other_do_not_collide(self):
        self.hook("claude", "needs-input", session_id="abc", tool_name="Bash")
        self.hook("claude", "needs-input", session_id="abc__x", tool_name="Bash")
        self.hook("claude", "clear", session_id="abc")
        self.assertEqual([m["session"] for _p, m in self.store.markers()], ["abc__x"])

    def test_stop_replaces_pending_markers_with_done(self):
        self.hook("codex", "needs-input", session_id="s", tool_name="Bash")
        self.hook("codex", "needs-input", session_id="s", agent_id="sub", tool_name="Bash")
        self.hook("codex", "done", session_id="s")
        self.assertEqual(self.states(), [("codex", "main", "done")])

    def test_done_from_a_subagent_does_not_flash(self):
        self.hook("claude", "needs-input", session_id="s", agent_id="sub", tool_name="Bash")
        self.hook("claude", "done", session_id="s", agent_id="sub")
        self.assertEqual(self.states(), [])

    def test_resume_or_new_prompt_ends_a_done_flash(self):
        self.hook("claude", "done", session_id="s")
        self.hook("claude", "resume", session_id="s", tool_name="Bash")
        self.assertEqual(self.states(), [])
        self.hook("claude", "done", session_id="s")
        self.hook("claude", "clear", session_id="s")
        self.assertEqual(self.states(), [])

    def test_copilot_only_alerts_for_permission_prompts_and_questions(self):
        for kind in ("shell_completed", "agent_completed", "agent_idle", "shell_detached_completed"):
            self.hook("copilot", "needs-input", sessionId="c", notification_type=kind)
        self.assertEqual(self.states(), [])
        self.hook("copilot", "needs-input", sessionId="c", notification_type="permission_prompt")
        self.assertEqual(self.states(), [("copilot", "main", "needs_input")])
        self.hook("copilot", "resume", sessionId="c", toolName="bash")
        self.assertEqual(self.states(), [])
        self.hook("copilot", "needs-input", sessionId="c", notification_type="elicitation_dialog")
        self.assertEqual(self.states(), [("copilot", "main", "needs_input")])

    def test_copilot_app_questions_alert_via_pre_tool_use(self):
        self.hook("copilot", "needs-input", sessionId="c", toolName="bash")
        self.assertEqual(self.states(), [])
        self.hook("copilot", "needs-input", sessionId="c", toolName="ask_user")
        self.assertEqual(self.states(), [("copilot", "main", "needs_input")])
        self.hook("copilot", "resume", sessionId="c", toolName="ask_user")
        self.assertEqual(self.states(), [])
        self.hook("copilot", "needs-input", sessionId="c", toolName="exit_plan_mode")
        self.assertEqual(self.states(), [("copilot", "main", "needs_input")])

    def test_missing_session_id_still_works(self):
        self.hook("codex", "needs-input", tool_name="Bash")
        self.hook("codex", "resume", tool_name="Bash")
        self.assertEqual(self.states(), [])

    def test_unusual_ids_are_made_filename_safe(self):
        self.hook("claude", "needs-input", session_id="../../etc/passwd\n" + "x" * 500, tool_name="Bash")
        (path, marker), = self.store.markers()
        self.assertEqual(os.path.dirname(path), self.store.sessions)
        self.assertEqual(self.states(), [("claude", "main", "needs_input")])

    def test_truncated_payload_still_identifies_the_session(self):
        raw = b'{"session_id":"abc","hook_event_name":"PostToolUse","tool_name":"Bash","tool_response":"' + b"x" * 70000
        data = ag.parse_payload(raw)
        self.assertEqual((data["session_id"], data["tool_name"]), ("abc", "Bash"))
        self.assertEqual(ag.parse_payload(b"garbage"), {})
        self.assertEqual(ag.parse_payload(b"[1,2]"), {})


class HookCommand(unittest.TestCase):
    def run_hook(self, args, stdin=b"", env_home=None):
        env = dict(os.environ, AGENT_GLOW_HOME=env_home)
        return subprocess.run([sys.executable, SCRIPT, *args], input=stdin, capture_output=True, env=env,
                              timeout=15)

    def test_hook_writes_a_marker_prints_json_and_exits_zero(self):
        home = temp_dir(self)
        payload = json.dumps({"session_id": "s", "tool_name": "Bash", "hook_event_name": "PermissionRequest"})
        r = self.run_hook(["hook", "claude", "needs-input"], payload.encode(), home)
        self.assertEqual((r.returncode, r.stdout), (0, b"{}"))
        self.assertEqual(len(os.listdir(os.path.join(home, "sessions"))), 1)

    def test_hook_survives_garbage_missing_args_and_unwritable_state(self):
        home = temp_dir(self)
        for args, stdin in ((["hook", "claude", "needs-input"], b"\xff\xfe not json"),
                            (["hook"], b""), (["hook", "nobody", "explode"], b"{}"),
                            (["hook", "claude", "done"], b"")):
            r = self.run_hook(args, stdin, home)
            self.assertEqual((r.returncode, r.stdout), (0, b"{}"), args)
        blocker = os.path.join(home, "a-file")
        open(blocker, "w").close()
        r = self.run_hook(["hook", "claude", "needs-input"], b'{"session_id":"s"}', os.path.join(blocker, "x"))
        self.assertEqual((r.returncode, r.stdout), (0, b"{}"))

    def test_hook_returns_even_if_the_agent_never_closes_stdin(self):
        env = dict(os.environ, AGENT_GLOW_HOME=temp_dir(self))
        p = subprocess.Popen([sys.executable, SCRIPT, "hook", "claude", "clear"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        try:
            self.assertEqual(p.wait(timeout=10), 0)
            self.assertEqual(p.stdout.read(), b"{}")
        finally:
            p.kill()
            p.stdin.close()
            p.stdout.close()

    def test_status_and_unknown_command(self):
        home = temp_dir(self)
        r = self.run_hook(["status"], b"", home)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"daemon    : not running", r.stdout)
        self.assertEqual(self.run_hook(["bogus"], b"", home).returncode, 2)


class FakeLogi:
    INDEX = 7

    def __init__(self, options=0x08, level=0, enabled=1, supported=0x38, version=4, levels=8, feature=True,
                 number=0xFF, reply_number=None):
        self.config = dict(enabled=enabled, options=options, level=level)
        self.supported, self.version, self.levels, self.feature = supported, version, levels, feature
        self.number = number
        self.reply_number = number if reply_number is None else reply_number
        self.durations = (11, 11, 60, 42)
        self.requests = 0
        self.drop = 0
        self.writes = []
        self.reply = None
        self.alive = True
        self.closed = False
        self.refuse_open = None

    def write(self, data):
        if not self.alive:
            raise OSError("unplugged")
        assert data[0] == 0x11 and len(data) == 20
        self.requests += 1
        if self.drop:
            self.drop -= 1
            self.reply, self.pending = None, None
            return len(data)
        self.reply = [0x10, 0, 0, 0, 0, 0, 0]
        number, index, call, params = data[1], data[2], data[3], data[4:]
        head = [0x11, self.reply_number, index, call]
        if number != self.number:
            self.pending = [0x10, number, 0x8F, index, call, 0x01, 0]
            return len(data)
        if index == 0:
            found = self.feature and bytes(params[:2]) == b"\x19\x82"
            self.pending = head + [self.INDEX if found else 0, 0, self.version if found else 0]
        elif call >> 4 == ag.BACKLIGHT_GET:
            c = self.config
            self.pending = head + list(ag.struct.pack(
                "<BBBHBHHHH", c["enabled"], c["options"], self.supported, 0, c["level"], *self.durations))
        elif call >> 4 == ag.BACKLIGHT_SET:
            enabled, options, effect, level, *durations = ag.struct.unpack("<BBBBHHHH", bytes(params[:12]))
            assert effect == 0xFF and tuple(durations) == self.durations
            self.config = dict(enabled=enabled, options=options, level=level)
            self.writes.append((enabled, options, level))
            self.pending = head
        elif call >> 4 == ag.BACKLIGHT_RANGE:
            self.pending = head + [self.levels]
        else:
            self.pending = [0x11, self.number, 0xFF, index, call, 0x05]
        return len(data)

    def read(self, size, timeout):
        if not self.alive:
            raise OSError("unplugged")
        reply, self.reply = self.reply, None
        if reply:
            return reply
        reply, self.pending = self.pending, None
        return reply or []


def add_logi(hid, logi, page=0xFF43, usage=0x0202, pid=0xB35B, product="MX Keys"):
    n = len(hid.keyboards)
    info = {"vendor_id": 0x046D, "product_id": pid, "manufacturer_string": "Logitech", "product_string": product,
            "path": b"logi%d" % n, "usage_page": page, "usage": usage}
    hid.keyboards.append((info, logi))
    return logi


class LogitechKeyboard(unittest.TestCase):
    def open(self, logi, **kwargs):
        hid = FakeHid()
        add_logi(hid, logi, **kwargs)
        return ag.LogiKeyboard.open(hid)

    def test_bluetooth_keyboard_is_found_and_reports_its_state(self):
        kb = self.open(FakeLogi(options=0x18, level=5))
        self.assertEqual((kb.id, kb.name, kb.lighting), ("046d:b35b", "Logitech MX Keys", "backlight"))
        self.assertEqual(kb.read_state(), {"effect": 0x18, "brightness": 5, "speed": 1, "hue": 0, "sat": 0})

    def test_a_bluetooth_reply_may_use_device_number_zero(self):
        self.assertIsNotNone(self.open(FakeLogi(reply_number=0)))

    def test_receiver_slots_are_searched(self):
        kb = self.open(FakeLogi(number=3), page=0xFF00, usage=0x0002, pid=0xC548)
        self.assertEqual(kb.number, 3)

    def test_receiver_short_interface_is_skipped(self):
        self.assertIsNone(self.open(FakeLogi(number=1), page=0xFF00, usage=0x0001))

    def test_device_without_backlight_is_reported(self):
        with self.assertRaises(ag.KeyboardError) as caught:
            self.open(FakeLogi(feature=False))
        self.assertIn("no HID++ backlight", str(caught.exception))

    def test_a_pinned_other_vendor_skips_logitech(self):
        hid = FakeHid()
        add_logi(hid, FakeLogi())
        self.assertIsNone(ag.LogiKeyboard.open(hid, vid=0x4D4C))

    def test_blink_uses_manual_mode_and_restore_puts_automatic_back(self):
        logi = FakeLogi(options=0x08)
        kb = self.open(logi)
        before = kb.read_state()
        kb.alert(0, 255, 255)
        kb.set(ag.ID_COLOR, 170, 255)
        kb.set(ag.ID_BRIGHTNESS, 0)
        kb.set(ag.ID_BRIGHTNESS, 255)
        self.assertEqual(logi.writes, [(1, 0x18, 7), (0, 0x18, 7), (1, 0x18, 7)])
        kb.restore(before)
        self.assertEqual(logi.writes[-1], (1, 0x08, 0))
        self.assertTrue(ag.same_lighting(kb.read_state(), before))

    def test_older_firmware_toggles_the_backlight_without_changing_mode(self):
        logi = FakeLogi(options=0x08, supported=0, version=1)
        kb = self.open(logi)
        kb.alert(0, 255, 255)
        kb.set(ag.ID_BRIGHTNESS, 0)
        self.assertEqual(logi.writes, [(1, 0x08, 0), (0, 0x08, 0)])

    def test_device_errors_raise_without_a_retry(self):
        logi = FakeLogi()
        kb = self.open(logi)
        logi.requests = 0
        with self.assertRaises(ag.KeyboardError):
            kb.request(kb.feature, 9)
        self.assertEqual(logi.requests, 1)

    def test_a_timed_out_request_is_retried_once(self):
        logi = FakeLogi(options=0x08)
        kb = self.open(logi)
        logi.drop, logi.requests = 1, 0
        kb.set(ag.ID_BRIGHTNESS, 0)
        self.assertEqual((logi.requests, logi.writes), (2, [(0, 0x08, 0)]))
        logi.drop, logi.requests = 2, 0
        with self.assertRaises(ag.HidppTimeout):
            kb.set(ag.ID_BRIGHTNESS, 255)
        self.assertEqual(logi.requests, 2)

    def test_daemon_blinks_and_restores(self):
        logi = FakeLogi(options=0x08)
        hid = FakeHid()
        add_logi(hid, logi)
        now = [100.0]
        root = temp_dir(self)
        store = ag.Store(root)
        with mock.patch.object(ag, "load_hid", lambda: hid):
            daemon = REAL_DAEMON(store, mono=lambda: now[0], logger=lambda _msg: None)
            store.put("claude", "s", ag.MAIN, ag.NEEDS_INPUT)
            daemon.tick()
            self.assertEqual(logi.config, dict(enabled=1, options=0x18, level=7))
            now[0] += 1
            daemon.tick()
            self.assertEqual(logi.config["enabled"], 0)
            store.drop("claude", "s")
            daemon.tick()
        self.assertEqual(logi.config, dict(enabled=1, options=0x08, level=0))
        self.assertFalse(store.has_snapshot())

    def test_via_keyboard_wins_and_a_broken_one_falls_back_to_logitech(self):
        hid = FakeHid(FakeVia())
        add_logi(hid, FakeLogi())
        with mock.patch.object(ag, "load_hid", lambda: hid):
            self.assertEqual(ag.open_configured(ag.Config()).lighting, "rgblight")
            self.assertEqual(ag.open_configured(ag.Config({"keyboard": {"vid": "046d"}})).lighting, "backlight")
        broken = FakeHid(FakeVia(protocol=9))
        add_logi(broken, FakeLogi())
        with mock.patch.object(ag, "load_hid", lambda: broken):
            self.assertEqual(ag.open_configured(ag.Config()).lighting, "backlight")

    def test_pinned_lighting_mode_leaves_logitech_alone(self):
        hid = FakeHid()
        add_logi(hid, FakeLogi())
        with mock.patch.object(ag, "load_hid", lambda: hid):
            self.assertIsNone(ag.open_configured(ag.Config({"keyboard": {"lighting": "rgblight"}})))

    def test_macos_opens_the_interface_shared_then_restores_the_default(self):
        hid = FakeHid()
        add_logi(hid, FakeLogi())
        with mock.patch.object(ag.sys, "platform", "darwin"), mock.patch("ctypes.CDLL") as cdll:
            hid.__file__ = "hid.so"
            self.assertIsNotNone(ag.LogiKeyboard.open(hid))
        calls = cdll.return_value.hid_darwin_set_open_exclusive.call_args_list
        self.assertEqual([c.args for c in calls], [(0,), (1,)])

    def test_devices_and_udev_rule_include_logitech(self):
        hid = FakeHid()
        add_logi(hid, FakeLogi())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_GLOW_HOME": temp_dir(self)}), contextlib.redirect_stdout(out):
            self.assertEqual(ag.cmd_devices([], hid_module=hid), 0)
        self.assertIn("046d:b35b  Logitech MX Keys: ready (HID++ backlight v4, white only)", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ag.cmd_udev_rule([], hid_module=hid), 0)
        self.assertIn('ATTRS{idVendor}=="046d", ATTRS{idProduct}=="b35b"', out.getvalue())
        self.assertIn('KERNELS=="0005:046D:*"', out.getvalue())


class FakeMacBacklight:
    def __init__(self, value=0.0):
        self.value = value
        self.writes = []

    def brightness(self):
        return self.value

    def set_brightness(self, value):
        self.value = value
        self.writes.append(value)


class MacbookKeyboard(unittest.TestCase):
    def found(self, backlight):
        return mock.patch.object(ag.MacBacklight, "find", return_value=backlight)

    def test_blink_lights_up_from_dark_and_restores(self):
        light = FakeMacBacklight(0.0)
        kb = ag.MacKeyboard(light)
        before = kb.read_state()
        kb.alert(0, 255, 255)
        kb.set(ag.ID_COLOR, 170, 255)
        kb.set(ag.ID_BRIGHTNESS, 0)
        kb.set(ag.ID_BRIGHTNESS, 255)
        self.assertEqual(light.writes, [1.0, 0.0, 1.0])
        kb.restore(before)
        self.assertEqual(light.value, 0.0)
        self.assertTrue(ag.same_lighting(kb.read_state(), before))

    def test_daemon_blinks_and_restores_the_users_brightness(self):
        light = FakeMacBacklight(0.5)
        now = [100.0]
        store = ag.Store(temp_dir(self))
        with self.found(light):
            daemon = REAL_DAEMON(store, lambda _cfg: ag.MacKeyboard.open(), mono=lambda: now[0],
                                 logger=lambda _msg: None)
            store.put("codex", "s", ag.MAIN, ag.NEEDS_INPUT)
            daemon.tick()
            self.assertEqual(light.value, 1.0)
            now[0] += 1
            daemon.tick()
            self.assertEqual(light.value, 0.0)
            store.drop("codex", "s")
            daemon.tick()
        self.assertAlmostEqual(light.value, 0.5, delta=0.01)
        self.assertFalse(store.has_snapshot())

    def test_used_last_unless_chosen_or_another_keyboard_is_pinned(self):
        hid = FakeHid()
        light = FakeMacBacklight()
        with mock.patch.object(ag, "load_hid", lambda: hid), self.found(light):
            self.assertEqual(ag.open_configured(ag.Config()).id, "apple:builtin")
            self.assertIsNone(ag.open_configured(ag.Config({"keyboard": {"vid": "046d"}})))
            self.assertIsNone(ag.open_configured(ag.Config({"keyboard": {"lighting": "rgblight"}})))
            hid.add(FakeVia())
            self.assertEqual(ag.open_configured(ag.Config()).id, ZED65)
            chosen = ag.Config({"keyboard": {"lighting": "macbook"}})
            self.assertEqual(chosen.lighting, "macbook")
            self.assertEqual(ag.open_configured(chosen).id, "apple:builtin")

    def test_a_broken_via_keyboard_falls_back_to_the_macbook(self):
        hid = FakeHid(FakeVia(protocol=9))
        with mock.patch.object(ag, "load_hid", lambda: hid), self.found(FakeMacBacklight()):
            self.assertEqual(ag.open_configured(ag.Config()).id, "apple:builtin")

    def test_a_logitech_keyboard_wins_over_the_macbook(self):
        hid = FakeHid()
        add_logi(hid, FakeLogi())
        with mock.patch.object(ag, "load_hid", lambda: hid), self.found(FakeMacBacklight()):
            self.assertEqual(ag.open_configured(ag.Config()).id, "046d:b35b")
            self.assertEqual(ag.open_configured(ag.Config({"keyboard": {"lighting": "macbook"}})).id, "apple:builtin")

    def test_devices_lists_it(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_GLOW_HOME": temp_dir(self)}), self.found(FakeMacBacklight()), \
                contextlib.redirect_stdout(out):
            self.assertEqual(ag.cmd_devices([], hid_module=FakeHid()), 0)
        self.assertIn("* apple:builtin  MacBook built-in keyboard: ready", out.getvalue())
        self.assertIn("agent-glow uses apple:builtin", out.getvalue())

    def test_refused_write_raises(self):
        backlight = ag.MacBacklight(None)
        with mock.patch.object(backlight, "send", return_value=False), self.assertRaises(ag.KeyboardError):
            backlight.set_brightness(1.0)

    def test_not_found_on_other_platforms(self):
        with mock.patch.object(ag.sys, "platform", "linux"):
            self.assertIsNone(REAL_MAC_FIND(ag.MacBacklight))


class FakeHaptic:
    INDEX = 11

    def __init__(self, flags=0x03, mask=0x7FFF, feature=True, number=0xFF):
        self.flags, self.mask, self.feature, self.number = flags, mask, feature, number
        self.played = []
        self.reply = None
        self.pending = None
        self.alive = True
        self.closed = False
        self.refuse_open = None

    def write(self, data):
        if not self.alive:
            raise OSError("unplugged")
        assert data[0] == 0x11 and len(data) == 20
        self.reply = [0x10, 0, 0, 0, 0, 0, 0]
        number, index, call, params = data[1], data[2], data[3], data[4:]
        head = [0x11, self.number, index, call]
        if number != self.number:
            self.pending = [0x10, number, 0x8F, index, call, 0x01, 0]
        elif index == 0:
            found = self.feature and bytes(params[:2]) == b"\x19\xb0"
            self.pending = head + [self.INDEX if found else 0, 0, 0]
        elif call >> 4 == ag.HAPTIC_CAPABILITIES:
            self.pending = head + [0, 1, 0, 0x3C] + list(self.mask.to_bytes(4, "big"))
        elif call >> 4 == ag.HAPTIC_CONFIGURATION:
            self.pending = head + [self.flags, 0x3C]
        elif call >> 4 == ag.HAPTIC_PLAY:
            self.played.append(params[0])
            self.pending = head
        else:
            self.pending = [0x11, self.number, 0xFF, index, call, 0x07]
        return len(data)

    def read(self, size, timeout):
        if not self.alive:
            raise OSError("unplugged")
        reply, self.reply = self.reply, None
        if reply:
            return reply
        reply, self.pending = self.pending, None
        return reply or []


def add_mouse(hid, mouse, page=0xFF43, usage=0x0202, pid=0xB042):
    info = {"vendor_id": 0x046D, "product_id": pid, "manufacturer_string": "Logitech", "product_string": "MX Master 4",
            "path": b"mouse%d" % len(hid.keyboards), "usage_page": page, "usage": usage}
    hid.keyboards.append((info, mouse))
    return mouse


class HapticDevice(unittest.TestCase):
    def open(self, mouse, **kwargs):
        hid = FakeHid()
        add_mouse(hid, mouse, **kwargs)
        return ag.HapticMouse.open(hid)

    def test_bluetooth_mouse_is_found_and_plays(self):
        fake = FakeHaptic()
        mouse = self.open(fake)
        self.assertEqual((mouse.id, mouse.name), ("046d:b042", "Logitech MX Master 4"))
        mouse.play("angry_alert")
        self.assertEqual(fake.played, [0x06])

    def test_receiver_slots_are_searched(self):
        self.assertEqual(self.open(FakeHaptic(number=2), page=0xFF00, usage=0x0002, pid=0xC548).number, 2)

    def test_short_receiver_interface_and_other_devices_are_skipped(self):
        self.assertIsNone(self.open(FakeHaptic(number=1), page=0xFF00, usage=0x0001))
        self.assertIsNone(self.open(FakeHaptic(feature=False)))

    def test_haptics_turned_off_on_the_mouse_are_left_alone(self):
        self.assertIsNone(self.open(FakeHaptic(flags=0x02)))

    def test_refused_open_is_skipped(self):
        fake = FakeHaptic()
        fake.refuse_open = "busy"
        self.assertIsNone(self.open(fake))

    def test_supported_waveforms_come_from_the_mask(self):
        mouse = self.open(FakeHaptic(mask=1 << 0x05))
        self.assertTrue(mouse.supports("happy_alert"))
        self.assertFalse(mouse.supports("ringing"))

    def test_device_errors_raise(self):
        mouse = self.open(FakeHaptic())
        with self.assertRaises(ag.KeyboardError):
            mouse.request(mouse.feature, 9)

    def test_keyboard_and_mouse_are_told_apart(self):
        hid = FakeHid()
        add_mouse(hid, FakeHaptic())
        add_logi(hid, FakeLogi())
        with mock.patch.object(ag, "load_hid", lambda: hid):
            self.assertEqual(ag.open_configured(ag.Config()).id, "046d:b35b")
        self.assertEqual(ag.HapticMouse.open(hid).id, "046d:b042")

    def test_interface_is_opened_shared_on_macos(self):
        hid = FakeHid()
        add_mouse(hid, FakeHaptic())
        hid.__file__ = "hid.so"
        with mock.patch.object(ag.sys, "platform", "darwin"), mock.patch("ctypes.CDLL") as cdll:
            self.assertIsNotNone(ag.HapticMouse.open(hid))
        calls = cdll.return_value.hid_darwin_set_open_exclusive.call_args_list
        self.assertEqual([c.args for c in calls], [(0,), (1,)])


class HapticAlerts(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.store = ag.Store(self.root)
        self.clock = Clock()
        self.fake = FakeHaptic()
        self.hid = FakeHid()
        add_mouse(self.hid, self.fake)
        self.opens = 0
        self.logs = []
        self.configure()

    def configure(self, **haptics):
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"haptics": dict({"enabled": True}, **haptics)}, f)
        self.d = ag.Daemon(self.store, lambda _cfg: None, wall=self.clock, mono=self.clock,
                           logger=self.logs.append, open_mouse=self.open_mouse)

    def open_mouse(self):
        self.opens += 1
        return ag.HapticMouse.open(self.hid)

    def wait(self, agent="claude", state=ag.NEEDS_INPUT):
        self.store.put(agent, "s", ag.MAIN, state, now=self.clock())

    def advance(self, seconds):
        self.clock.t += seconds
        for marker in self.store.markers():
            if marker[1]:
                self.store.put(marker[1]["agent"], marker[1]["session"], marker[1]["agent_id"], marker[1]["state"],
                               now=self.clock())
        self.d.tick()

    def test_off_by_default_never_opens_the_mouse(self):
        os.remove(os.path.join(self.root, "config.json"))
        self.d.load_config()
        self.wait()
        self.d.tick()
        self.assertEqual((self.opens, self.fake.played), (0, []))

    def test_each_agent_has_its_own_vibration(self):
        self.wait("codex")
        self.d.tick()
        self.assertEqual(self.fake.played, [0x06])

    def test_custom_and_invalid_waveforms(self):
        self.configure(waveforms={"claude": "jingle", "codex": "nonsense"})
        self.wait("claude")
        self.d.tick()
        self.advance(2)
        self.wait("codex")
        self.d.tick()
        self.assertEqual(self.fake.played, [0x0D, 0x06])

    def test_repeats_while_waiting_and_stops_after(self):
        self.wait()
        self.d.tick()
        self.advance(10)
        self.assertEqual(self.fake.played, [0x05])
        self.advance(20)
        self.assertEqual(self.fake.played, [0x05, 0x05])
        self.store.drop("claude", "s")
        self.d.tick()
        self.assertTrue(self.fake.closed)
        self.clock.t += 100
        self.d.tick()
        self.assertEqual(len(self.fake.played), 2)

    def test_repeat_can_be_turned_off(self):
        self.configure(repeat_seconds=0)
        self.wait()
        self.d.tick()
        self.advance(100)
        self.assertEqual(self.fake.played, [0x05])

    def test_several_agents_take_turns(self):
        self.wait("claude")
        self.wait("codex")
        self.d.tick()
        self.assertEqual(self.fake.played, [0x05])
        self.advance(0.5)
        self.assertEqual(self.fake.played, [0x05])
        self.advance(1.5)
        self.assertEqual(self.fake.played, [0x05, 0x06])

    def test_finished_turn_plays_once_when_done_is_on(self):
        with open(os.path.join(self.root, "config.json"), "w") as f:
            json.dump({"haptics": {"enabled": True}, "done": {"enabled": True, "seconds": 60}}, f)
        os.utime(os.path.join(self.root, "config.json"), (self.clock.t + 10, self.clock.t + 10))
        self.d.load_config()
        self.wait(state=ag.DONE)
        self.d.tick()
        self.advance(40)
        self.assertEqual(self.fake.played, [0x07])

    def test_unsupported_waveform_is_skipped_once(self):
        self.fake.mask = 0
        self.wait()
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.fake.played, [])
        self.assertEqual(self.opens, 1)

    def test_missing_mouse_is_retried_then_vibrates(self):
        self.hid.present = False
        self.wait()
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.opens, 1)
        self.hid.present = True
        self.advance(ag.RETRY_SECONDS)
        self.assertEqual(self.fake.played, [0x05])
        self.assertEqual(sum("haptic mouse not available" in line for line in self.logs), 1)

    def test_an_unplugged_mouse_does_not_stop_the_daemon(self):
        self.wait()
        self.d.tick()
        self.fake.alive = False
        self.advance(30)
        self.assertTrue(any("haptic mouse error" in line for line in self.logs))
        self.assertIsNone(self.d.mouse)
        self.fake.alive = True
        self.advance(ag.RETRY_SECONDS)
        self.assertEqual(self.fake.played, [0x05, 0x05])

    def test_shutdown_closes_the_mouse(self):
        self.wait()
        self.d.tick()
        self.d.shutdown()
        self.assertTrue(self.fake.closed)

    def test_config_values(self):
        c = ag.Config({"haptics": {"enabled": "yes", "repeat_seconds": 99999}})
        self.assertFalse(c.haptics_enabled)
        self.assertEqual(c.haptic_repeat, 3600)
        self.assertEqual(c.waveform_for(ag.NEEDS_INPUT, "someone"), "happy_alert")
        self.assertEqual(c.waveform_for(ag.DONE, "claude"), "completed")

    def test_devices_status_and_udev_rule(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_GLOW_HOME": self.root}), contextlib.redirect_stdout(out):
            self.assertEqual(ag.cmd_devices([], hid_module=self.hid), 1)
            ag.cmd_status([])
            self.assertEqual(ag.cmd_udev_rule([], hid_module=self.hid), 0)
        self.assertIn("046d:b042  Logitech MX Master 4: ready (haptics on)", out.getvalue())
        self.assertIn("haptics   : on", out.getvalue())
        self.assertIn('KERNELS=="0005:046D:*"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
