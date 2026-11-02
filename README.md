# agent-glow

Blink your keyboard's RGB lighting when **Claude Code**, **Codex** or **GitHub Copilot** is waiting for you.

Agents ask for permission, ask a question or present a plan, then sit there until you notice. agent-glow
turns the keyboard into the notification: it blinks in the agent's color until you answer, then puts
your own lighting back exactly as it was.

| Agent waiting | Blinks |
| --- | --- |
| Claude Code | red |
| Codex | blue |
| GitHub Copilot (CLI and app) | green |

If several agents are waiting, each flash shows the next one's color. Colors, speed and brightness are
configurable, and an optional short "turn finished" flash is available.

Built for Omnitype keyboards. It works with any QMK keyboard that has VIA and RGB lighting, and with
Logitech MX Keys keyboards (white backlight only, see [Logitech MX Keys](#logitech-mx-keys)) and MacBook
built-in keyboards (white backlight only, see [MacBook keyboards](#macbook-keyboards)).
A Logitech MX Master 4 can also vibrate when an agent is waiting, see [Mouse vibration](#mouse-vibration).

## Requirements

- **A QMK keyboard with VIA enabled** and RGB Light or RGB Matrix lighting, connected **by USB cable**.
  Wireless connections usually do not carry VIA.
  - Its firmware must speak **VIA protocol 12 or newer** (QMK from 2023 on). `agent-glow devices`
    tells you if it doesn't.
  - Tested on an Omnitype keyboard (Mechlovin Zed65 Rev1 PCB, `4d4c:6505`).
- **Or a Logitech MX Keys keyboard**, connected by Bluetooth or a Bolt/Unifying receiver. See
  [Logitech MX Keys](#logitech-mx-keys).
- **Or a MacBook built-in keyboard.** See [MacBook keyboards](#macbook-keyboards).
- **Python 3.9+** and git.
- **macOS, Linux or Windows.** Only macOS has been tested on real hardware so far. Linux and Windows
  pass the automated tests, but please open an issue if anything misbehaves.

## Install

```sh
git clone https://github.com/f/agent-glow
cd agent-glow
python3 install.py          # on Windows: py install.py
```

The installer:

1. Copies `agent_glow.py` to `~/.agent-glow` and creates a private Python venv there with
   [hidapi](https://pypi.org/project/hidapi/). Your system Python is left alone.
2. Checks that it can drive your keyboard.
3. Adds hooks to each agent's settings. Existing settings and hooks are kept, and a one-time
   `*.agent-glow.bak` backup is made. The files it touches are:
   - `~/.claude/settings.json`
   - `~/.codex/hooks.json`
   - `~/.copilot/hooks/agent-glow.json`
4. Starts a small background daemon at login:
   - **macOS:** a LaunchAgent (`io.github.f.agent-glow`).
   - **Linux:** a systemd user service (`agent-glow.service`).
   - **Windows:** an HKCU `Run` entry.

Then:

- **Restart** any running Claude Code, Codex or Copilot sessions. Agents load hooks when a session starts.
- **Codex:** run `/hooks` once and trust the agent-glow hooks. Codex skips hooks you haven't reviewed.
- Try it:

```sh
~/.agent-glow/bin/agent-glow test            # every agent's color for 5 seconds
~/.agent-glow/bin/agent-glow test codex 3
```

If `~/.local/bin` is on your `PATH`, the installer links `agent-glow` there, so plain `agent-glow`
works. On Windows the command is `%USERPROFILE%\.agent-glow\bin\agent-glow.cmd`.

### Installer options

| Option | |
| --- | --- |
| `--agents claude,codex` | Only register hooks for these agents. The default is all three. |
| `--keyboard 4d4c:6505` | Use this keyboard instead of the first usable keyboard found. |
| `--no-service` | Don't start the daemon at login. Run `agent-glow daemon` yourself. |
| `--no-venv` | Use the Python running the installer, which must have `hidapi` installed. |
| `--uninstall` | Remove everything the installer added. |

To update, run `git pull && python3 install.py` again. Reinstalling is safe and doesn't duplicate hooks.

### Linux: allow access to the keyboard

Linux only lets root open raw HID devices unless a udev rule says otherwise. The installer reminds you
if this is needed:

```sh
~/.agent-glow/bin/agent-glow udev-rule | sudo tee /etc/udev/rules.d/60-agent-glow.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the keyboard. The rule covers your keyboard's VID:PID only (for a Logitech keyboard, also
every Logitech Bluetooth device) and uses `TAG+="uaccess"`, so it grants access to the logged-in user.

### Logitech MX Keys

MX Keys keyboards connected by Bluetooth or a Bolt/Unifying receiver are driven over Logitech's HID++
protocol (feature `0x1982`). Tested on an MX Keys Mac (`046d:b361`) over Bluetooth on macOS. No firmware
or VIA setup is needed, but they differ from RGB keyboards:

- **The backlight is white only.** Every agent blinks the same way, and the color settings are ignored.
- **It only blinks while the backlight is lit.** If the backlight has gone dark, whether from idling or the
  brightness keys, agent-glow cannot turn it back on, so the alert is not visible.
- **Blinking may write to the keyboard's flash.** Logitech's backlight command is reported to save its
  settings, so long alerts could add wear. Lower `needs_input.ttl_seconds` to limit how long they run.
- **Some models only switch on and off.** On these, including the MX Keys Mac, `brightness` has no effect.
- **Short pauses can happen.** The keyboard sometimes stops answering over Bluetooth. agent-glow
  reconnects by itself after a few seconds.
- **Lighting changes are not guaranteed to be temporary.** agent-glow reads your backlight mode and level
  before an alert and writes them back afterwards. If a crash interrupts this, the next daemon start or
  `agent-glow restore` puts them back.
- **A VIA keyboard takes priority** when both are connected. Pin one with `keyboard.vid` and `keyboard.pid`.
- **Logi Options+ may also change the backlight** while it is running.

### MacBook keyboards

A MacBook's built-in backlight is used when no VIA keyboard is usable. To use it even with a VIA keyboard
connected, set `"keyboard": { "lighting": "macbook" }`. Tested on a MacBook Pro with M4 (`Mac16,1`) on
macOS 27.2.

- **The backlight is white only.** Every agent blinks the same way, and the color settings are ignored.
- **It can light up from dark,** so the alert still shows when the backlight is off.
- **It relies on a private macOS API** (`KeyboardBrightnessClient`). If an update removes it, agent-glow
  reports no MacBook keyboard instead of failing. Writes do not save, and your brightness is put back
  after each alert.
- **Apple's external Magic Keyboards are not supported,** because they have no backlight.

### Mouse vibration

A Logitech mouse with haptics, such as the MX Master 4, can vibrate when an agent is waiting, with or without
a keyboard connected. It's off by default:

```json
{ "haptics": { "enabled": true } }
```

Each agent has its own vibration, so you can tell them apart by feel:

| Agent waiting | Vibration |
| --- | --- |
| Claude Code | `happy_alert` |
| Codex | `angry_alert` |
| GitHub Copilot | `ringing` |

It vibrates when an agent starts waiting, then again every `repeat_seconds` until you answer. Tested on an
MX Master 4 (`046d:b042`) over Bluetooth on macOS. The haptic command is reverse-engineered, so a Logitech
firmware update could change it. It doesn't change your mouse's settings, and the mouse must have haptics
turned on in Logi Options+.

## Commands

```
agent-glow devices                  list usable keyboards and whether agent-glow can drive them
agent-glow test [AGENT|all|done] [SECONDS]
agent-glow status                   daemon, keyboard and pending alerts
agent-glow stop                     stop the daemon (the lighting is put back)
agent-glow restore                  put the lighting back if a crash left it changed
agent-glow udev-rule [VID:PID ...]  print the Linux udev rule
agent-glow daemon                   run the daemon in the foreground
```

## Configuration

Create `~/.agent-glow/config.json`. Every key is optional, and the daemon picks up changes without a restart.

```json
{
  "keyboard": { "vid": "0x4D4C", "pid": "0x6505", "lighting": "auto" },
  "agents": ["claude", "codex", "copilot"],
  "blink_seconds": 0.4,
  "brightness": 255,
  "needs_input": {
    "ttl_seconds": 600,
    "colors": {
      "claude":  { "hue": 0,   "sat": 255 },
      "codex":   { "hue": 170, "sat": 255 },
      "copilot": { "hue": 85,  "sat": 255 }
    }
  },
  "done": { "enabled": false, "hue": 85, "sat": 255, "seconds": 5 },
  "haptics": {
    "enabled": false,
    "repeat_seconds": 30,
    "waveforms": { "claude": "happy_alert", "codex": "angry_alert", "copilot": "ringing", "done": "completed" }
  }
}
```

| Key | Meaning |
| --- | --- |
| `keyboard.vid` / `pid` | Which keyboard to use, as hex strings. Without them, the first usable keyboard is used (VIA first, then Logitech, then a MacBook's built-in keyboard). |
| `keyboard.lighting` | `auto`, `rgblight`, `rgb_matrix` or `macbook`. Set it if the keyboard has both and `auto` picks the wrong one. Logitech keyboards are only used with `auto`. |
| `agents` | Agents that may blink the keyboard. |
| `blink_seconds` | Time on and time off per blink, from 0.1 to 5. |
| `brightness` | Alert brightness, from 1 to 255. |
| `needs_input.colors` | Color per agent, as QMK hue and saturation (0–255). Red is 0, yellow 43, green 85, cyan 128, blue 170, magenta 213. |
| `needs_input.ttl_seconds` | Stop blinking for a question nobody answered after this long. |
| `done` | Flash this color for `seconds` when an agent finishes its turn. Off by default. |
| `haptics.enabled` | Vibrate a Logitech haptic mouse when an agent is waiting. Off by default. |
| `haptics.repeat_seconds` | Vibrate again this often while an agent is waiting, from 0 (never) to 3600. |
| `haptics.waveforms` | Vibration per agent, plus `done` for the optional "turn finished" alert. One of `sharp_state_change`, `damp_state_change`, `sharp_collision`, `damp_collision`, `subtle_collision`, `happy_alert`, `angry_alert`, `completed`, `square`, `wave`, `firework`, `mad`, `knock`, `jingle`, `ringing`. |

## How it works

```
agent hook ──► ~/.agent-glow/sessions/*.json ──► daemon ──VIA or HID++ raw HID──► keyboard
```

- **Hooks never touch the keyboard.** A hook only writes or deletes a tiny marker file and always
  exits 0 with `{}`. That means it can't slow an agent down, block it or change its decisions, even
  if the keyboard is unplugged or agent-glow is broken.
- **The hooks used:**
  - A **permission prompt**, a **question** (`AskUserQuestion`, `request_user_input`, `ask_user`) or a
    **plan to approve** marks the session as waiting.
  - The next tool result, a new prompt, an interrupt or the session ending clears it.
  - The end of a turn (`Stop` / `agentStop`) feeds the optional `done` flash.
- **The daemon:**
  - Watches the markers.
  - Saves the keyboard's current effect, color, brightness and speed.
  - Blinks the alert color.
  - Restores your lighting when nothing is waiting any more.
- **Your saved lighting is never overwritten (QMK).** Every write uses VIA's non-saving "set" command, so
  the keyboard's EEPROM is untouched, and unplugging it always brings your own lighting back.
- **Crash recovery.** If the daemon dies mid-alert, the saved snapshot is restored the next time it
  starts, or when you run `agent-glow restore`.

## Troubleshooting

- **Nothing blinks.**
  - Run `agent-glow devices`. It lists every usable keyboard and why one can't be used.
  - Then run `agent-glow test` and check `~/.agent-glow/daemon.log`.
- **`test` works but an agent doesn't trigger it.** Restart that agent's session. For Codex, trust the
  hooks in `/hooks`. `agent-glow status` shows the markers the hooks have written.
- **`devices` does not list the MacBook keyboard.** It needs a Mac with a backlit built-in keyboard, and
  a macOS version that still has the private API it uses.
- **"VIA protocol N is too old".** Flash a current QMK build of your keyboard's `via` keymap.
- **"does not expose RGB Light or RGB Matrix over VIA".** The firmware doesn't include VIA lighting
  control, so it can't be driven without a firmware change.
- **macOS: the VIA app can't connect.** On macOS only one program can hold the keyboard's VIA
  interface. agent-glow holds it only while an alert is showing.
- **Windows: Claude Code hooks.** Claude Code runs hooks with Git Bash, which it already requires on
  Windows.

## Uninstall

```sh
python3 install.py --uninstall
```

This removes:

- the daemon,
- the hooks (your other settings and hooks are kept),
- `~/.agent-glow`.

If you uninstall mid-alert, your lighting is restored first. The Linux udev rule, if you added one,
stays until you delete it.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests run without a keyboard or mouse. They use fake Logitech devices and a fake VIA device that behaves like
QMK's `via.c`.

## License

MIT
