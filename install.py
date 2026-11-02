#!/usr/bin/env python3
"""Install, update or remove agent-glow.

  python3 install.py                           install or update
  python3 install.py --uninstall               remove everything it installed
  python3 install.py --agents claude,copilot   register hooks for these agents only
  python3 install.py --keyboard 4d4c:6505      choose the keyboard when several keyboards are connected
  python3 install.py --no-service              do not run the daemon in the background at login

Installs into ~/.agent-glow (agent_glow.py and a private Python venv with hidapi), registers
lifecycle hooks for Claude Code, Codex and GitHub Copilot, and runs the daemon in the background:
a LaunchAgent on macOS, a systemd user service on Linux, a Run registry entry on Windows.

Agent settings are merged, never replaced: only hook entries that run agent_glow.py are added or
removed, and a pre-existing settings file is backed up once as <file>.agent-glow.bak.
"""
import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent_glow  # noqa: E402  (stdlib only at import time)

SCRIPT = "agent_glow.py"
HIDAPI = "hidapi==0.15.0"
AGENTS = ("claude", "codex", "copilot")
BACKUP_SUFFIX = ".agent-glow.bak"
LAUNCHD_LABEL = "io.github.f.agent-glow"
SYSTEMD_UNIT = "agent-glow.service"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "agent-glow"
DAEMON_WAIT = 8.0
MIN_PYTHON = (3, 9)

# (event, matcher, action, timeout in seconds). Only events that fire when the agent is about
# to show a prompt are used for "needs-input"; see agent_glow.handle_hook for what each action does.
CLAUDE_HOOKS = (
    ("PermissionRequest", None, "needs-input", 5),
    ("PreToolUse", "AskUserQuestion|ExitPlanMode", "needs-input", 5),
    ("Elicitation", None, "needs-input", 5),
    ("PostToolUse", None, "resume", 5),
    ("PostToolUseFailure", None, "resume", 5),
    ("ElicitationResult", None, "resume", 5),
    ("UserPromptSubmit", None, "clear", 5),
    ("SessionEnd", None, "clear", 2),
    ("Stop", None, "done", 5),
)
CODEX_HOOKS = (
    ("PermissionRequest", None, "needs-input", 5),
    ("PreToolUse", "request_user_input", "needs-input", 5),
    ("PostToolUse", None, "resume", 5),
    ("UserPromptSubmit", None, "clear", 5),
    ("Interrupt", None, "clear", 2),
    ("SessionEnd", None, "clear", 2),
    ("Stop", None, "done", 5),
)
COPILOT_HOOKS = (
    ("notification", "permission_prompt|elicitation_dialog", "needs-input", 5),
    ("preToolUse", "ask_user|exit_plan_mode", "needs-input", 5),
    ("postToolUse", None, "resume", 5),
    ("postToolUseFailure", None, "resume", 5),
    ("userPromptSubmitted", None, "clear", 5),
    ("sessionEnd", None, "clear", 2),
    ("agentStop", None, "done", 5),
)


class InstallError(Exception):
    pass


class ServiceError(Exception):
    """The background service could not be set up; everything else still works."""


def platform_name(value):
    if value.startswith("win"):
        return "windows"
    if value == "darwin":
        return "macos"
    if value.startswith("linux"):
        return "linux"
    return value


class Paths:
    def __init__(self, home, env, platform="macos", use_venv=True):
        self.home, self.platform = home, platform
        self.windows = platform == "windows"
        self.root = os.path.join(home, ".agent-glow")
        self.script = os.path.join(self.root, SCRIPT)
        self.venv = os.path.join(self.root, "venv")
        if use_venv:
            bindir = os.path.join(self.venv, "Scripts" if self.windows else "bin")
            self.python = os.path.join(bindir, "python.exe" if self.windows else "python")
            self.pythonw = os.path.join(bindir, "pythonw.exe") if self.windows else self.python
        else:
            self.python = sys.executable
            windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            self.pythonw = windowless if self.windows and os.path.exists(windowless) else sys.executable
        self.launcher = os.path.join(self.root, "bin", "agent-glow.cmd" if self.windows else "agent-glow")
        self.config = os.path.join(self.root, "config.json")
        self.log = os.path.join(self.root, "daemon.log")
        self.service_log = os.path.join(self.root, "service.log")
        self.plist = os.path.join(home, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
        config_home = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        self.unit = os.path.join(config_home, "systemd", "user", SYSTEMD_UNIT)
        # A convenience symlink, only where it is already on PATH.
        local_bin = os.path.join(home, ".local", "bin")
        on_path = {os.path.normcase(os.path.abspath(p)) for p in env.get("PATH", "").split(os.pathsep) if p}
        self.link = (os.path.join(local_bin, "agent-glow")
                     if not self.windows and os.path.normcase(os.path.abspath(local_bin)) in on_path else None)
        claude = env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
        codex = env.get("CODEX_HOME") or os.path.join(home, ".codex")
        copilot = env.get("COPILOT_HOME") or os.path.join(home, ".copilot")
        self.claude = os.path.join(claude, "settings.json")
        self.codex = os.path.join(codex, "hooks.json")
        self.copilot = os.path.join(copilot, "hooks", "agent-glow.json")

    def env(self):
        """Environment for the installer's own agent_glow.py runs. Hooks and the service use the
        default ~/.agent-glow, which is this root unless --home is used for testing."""
        return dict(os.environ, AGENT_GLOW_HOME=self.root)


def say(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- hook definitions

def sh_command(python, script, args):
    # Whatever goes wrong the hook must exit 0 and print JSON: exit code 2 from a PreToolUse
    # hook blocks the tool call, and Codex's Stop hook rejects empty output.
    return f"{shlex.quote(python)} {shlex.quote(script)} {args} 2>/dev/null || echo '{{}}'; exit 0"


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


def hook_command(paths, agent, action):
    args = f"hook {agent} {action}"
    if not paths.windows:
        return sh_command(paths.python, paths.script, args)
    if agent == "codex":  # Codex runs cmd.exe /C "<command>"
        return f'"{paths.python}" "{paths.script}" {args} 2>NUL || echo {{}}'
    if agent == "copilot":  # the "powershell" handler
        return f"& {ps_quote(paths.python)} {ps_quote(paths.script)} {args} 2>$null; exit 0"
    # Claude Code runs hooks with Git Bash on Windows.
    return sh_command(paths.python.replace("\\", "/"), paths.script.replace("\\", "/"), args)


def grouped_hooks(paths, agent, specs):
    """Claude Code / Codex format: event -> [{matcher?, hooks: [handler]}]."""
    out = {}
    for event, matcher, action, timeout in specs:
        group = {"hooks": [{"type": "command", "command": hook_command(paths, agent, action),
                            "timeout": timeout}]}
        if matcher:
            group = {"matcher": matcher, **group}
        out.setdefault(event, []).append(group)
    return out


def copilot_config(paths):
    """Copilot format: event -> [handler]. The whole file belongs to agent-glow."""
    shell = "powershell" if paths.windows else "bash"
    hooks = {}
    for event, matcher, action, timeout in COPILOT_HOOKS:
        handler = {"type": "command", shell: hook_command(paths, "copilot", action), "timeoutSec": timeout}
        if matcher:
            handler["matcher"] = matcher
        hooks.setdefault(event, []).append(handler)
    return {"version": 1, "hooks": hooks}


def is_ours(handler):
    if not isinstance(handler, dict):
        return False
    for key in ("command", "bash", "powershell"):
        command = handler.get(key)
        if isinstance(command, str) and SCRIPT in command and " hook " in command:
            return True
    return False


def strip_ours(hooks):
    """Remove our handlers from an event -> [group] mapping, keeping everything else."""
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list):
                left = [h for h in inner if not is_ours(h)]
                if len(left) != len(inner):
                    if not left:
                        continue
                    group = {**group, "hooks": left}
            kept.append(group)
        if kept:
            hooks[event] = kept
        elif groups:
            del hooks[event]


def add_hooks(data, ours, path):
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"'hooks' in {path} is not an object; nothing was changed")
    strip_ours(hooks)
    for event, groups in ours.items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise InstallError(f"hooks.{event} in {path} is not a list; nothing was changed")
        current.extend(groups)


def remove_hooks(data):
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        strip_ours(hooks)
        if not hooks:
            del data["hooks"]


# ---------------------------------------------------------------- files

def read_config(path):
    """Parsed JSON object and raw text of a config file; ({}, None) when it does not exist."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, None
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InstallError(f"{path} is not valid JSON ({e}); nothing was changed") from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not hold a JSON object; nothing was changed")
    return data, text


def render_json(data, original):
    """Serialize like the original file did (indent, trailing newline)."""
    indent = "  "
    match = re.search(r'^([ \t]+)"', original or "", re.M)
    if match:
        indent = match.group(1)
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    return text + "\n" if original is None or original.endswith("\n") else text


def write_file(path, content, mode=None):
    """Atomic write that follows symlinks and keeps the permissions of an existing file."""
    real = os.path.realpath(path)
    folder = os.path.dirname(real)
    os.makedirs(folder, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(real).st_mode)
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".agent-glow-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def remove_file(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def backup_once(path, text):
    backup = path + BACKUP_SUFFIX
    if text is not None and not os.path.exists(backup):
        write_file(backup, text, mode=0o600)


# ---------------------------------------------------------------- runtime (script, venv, command)

def venv_ok(paths, run):
    if not os.path.exists(paths.python):
        return False
    return run([paths.python, "-c", "import hid"], capture_output=True).returncode == 0


def ensure_venv(paths, run):
    if venv_ok(paths, run):
        return
    base = getattr(sys, "_base_executable", None) or sys.executable
    if os.path.isdir(paths.venv):
        shutil.rmtree(paths.venv)
    uv = shutil.which("uv")
    if uv:
        steps = [[uv, "venv", "--quiet", "--python", base, paths.venv],
                 [uv, "pip", "install", "--quiet", "--python", paths.python, HIDAPI]]
    else:
        steps = [[base, "-m", "venv", paths.venv],
                 [paths.python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", HIDAPI]]
    for cmd in steps:
        result = run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise InstallError(f"`{' '.join(cmd)}` failed:\n{(result.stderr or result.stdout or '').strip()}")
    if not venv_ok(paths, run):
        raise InstallError(f"{paths.python} cannot import hid after installing {HIDAPI}")


def launcher_text(paths):
    if paths.windows:
        return f'@"{paths.python}" "{paths.script}" %*\r\n'
    return f'#!/bin/sh\nexec {shlex.quote(paths.python)} {shlex.quote(paths.script)} "$@"\n'


def link_is_ours(paths):
    return bool(paths.link) and os.path.islink(paths.link) and os.readlink(paths.link) == paths.launcher


def install_runtime(paths, use_venv, run):
    os.makedirs(paths.root, mode=0o700, exist_ok=True)
    with open(os.path.join(HERE, SCRIPT), "rb") as f:
        write_file(paths.script, f.read(), mode=0o755)
    if use_venv:
        ensure_venv(paths, run)
    write_file(paths.launcher, launcher_text(paths), mode=0o755)
    if paths.link and (link_is_ours(paths) or not os.path.lexists(paths.link)):
        tmp = paths.link + ".agent-glow.tmp"
        remove_file(tmp)
        os.symlink(paths.launcher, tmp)
        os.replace(tmp, paths.link)
    return paths.link if link_is_ours(paths) else paths.launcher


def set_keyboard(paths, keyboard):
    vid, pid = keyboard
    data, text = read_config(paths.config)
    current = data.get("keyboard") if isinstance(data.get("keyboard"), dict) else {}
    data["keyboard"] = {**current, "vid": f"0x{vid:04X}", "pid": f"0x{pid:04X}"}
    write_file(paths.config, render_json(data, text), mode=0o644)


def probe_keyboard(paths, run):
    """(usable, report) from `agent_glow.py devices`."""
    try:
        r = run([paths.python, paths.script, "devices"], capture_output=True, text=True, env=paths.env())
    except OSError as e:
        return False, str(e)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()


# ---------------------------------------------------------------- background service

def run_text(run, cmd):
    return run(cmd, capture_output=True, text=True)


def output_of(result):
    return (result.stderr or result.stdout or "").strip()


class Launchd:
    def __init__(self, run):
        self.run = run

    @staticmethod
    def domain():
        return f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}"

    @staticmethod
    def plist(paths):
        return {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [paths.python, paths.script, "daemon"],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardOutPath": paths.service_log,
            "StandardErrorPath": paths.service_log,
        }

    def stop(self, paths):
        # SIGTERM makes the daemon put the keyboard lighting back before it exits.
        run_text(self.run, ["launchctl", "bootout", f"{self.domain()}/{LAUNCHD_LABEL}"])

    def start(self, paths):
        # install() has already booted out any running instance.
        write_file(paths.plist, plistlib.dumps(self.plist(paths)), mode=0o644)
        result = None
        for _attempt in range(5):  # bootstrap can fail while the old instance is still shutting down
            result = run_text(self.run, ["launchctl", "bootstrap", self.domain(), paths.plist])
            if result.returncode == 0:
                return f"LaunchAgent {LAUNCHD_LABEL}"
            time.sleep(1)
        raise InstallError(f"launchctl bootstrap failed: {output_of(result)}")

    def remove(self, paths):
        if not os.path.exists(paths.plist):
            return None
        self.stop(paths)
        remove_file(paths.plist)
        return f"LaunchAgent {LAUNCHD_LABEL}"


def systemd_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


class Systemd:
    def __init__(self, run):
        self.run = run

    @staticmethod
    def unit(paths):
        command = " ".join(systemd_quote(a) for a in (paths.python, paths.script, "daemon"))
        return ("[Unit]\n"
                "Description=agent-glow: blink the keyboard when an AI coding agent is waiting for you\n"
                "\n[Service]\n"
                f"ExecStart={command}\n"
                "Restart=on-failure\n"
                "RestartSec=5\n"
                "\n[Install]\n"
                "WantedBy=default.target\n")

    def ctl(self, *args):
        return run_text(self.run, ["systemctl", "--user", *args])

    def stop(self, paths):
        if shutil.which("systemctl") and os.path.exists(paths.unit):
            self.ctl("stop", SYSTEMD_UNIT)

    def start(self, paths):
        if not shutil.which("systemctl"):
            raise ServiceError("systemctl was not found (no systemd)")
        write_file(paths.unit, self.unit(paths), mode=0o644)
        for args in (("daemon-reload",), ("enable", SYSTEMD_UNIT), ("restart", SYSTEMD_UNIT)):
            result = self.ctl(*args)
            if result.returncode != 0:
                raise ServiceError(f"`systemctl --user {' '.join(args)}` failed: {output_of(result)}")
        return f"systemd user service {SYSTEMD_UNIT}"

    def remove(self, paths):
        if not os.path.exists(paths.unit):
            return None
        if shutil.which("systemctl"):
            self.ctl("disable", "--now", SYSTEMD_UNIT)
        remove_file(paths.unit)
        if shutil.which("systemctl"):
            self.ctl("daemon-reload")
        return f"systemd user service {SYSTEMD_UNIT}"


class WinRegistry:
    """HKEY_CURRENT_USER\\...\\Run: programs started at login, no admin rights needed."""

    def get(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name, value):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def spawn_detached(cmd, env=None):
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    subprocess.Popen(cmd, env=env, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WindowsRun:
    def __init__(self, run, spawn, registry):
        self.run, self.spawn, self.registry = run, spawn, registry

    @staticmethod
    def command(paths):
        return f'"{paths.pythonw}" "{paths.script}" daemon'

    def stop(self, paths):
        # There are no signals to ask a windowless process to stop; `stop` leaves a file it watches.
        if os.path.exists(paths.script) and os.path.exists(paths.python):
            self.run([paths.python, paths.script, "stop"], capture_output=True, text=True, env=paths.env())

    def start(self, paths):
        self.registry.set(RUN_VALUE, self.command(paths))
        self.spawn([paths.pythonw, paths.script, "daemon"], env=paths.env())
        return "started now and at every login (HKCU Run key)"

    def remove(self, paths):
        if self.registry.get(RUN_VALUE) is None:
            return None
        self.stop(paths)
        self.registry.delete(RUN_VALUE)
        return "login entry (HKCU Run key)"


def service_for(platform, run, spawn=spawn_detached, registry=None):
    if platform == "macos":
        return Launchd(run)
    if platform == "linux":
        return Systemd(run)
    if platform == "windows":
        return WindowsRun(run, spawn, registry or WinRegistry())
    return None


def wait_for_daemon(paths):
    end = time.monotonic() + DAEMON_WAIT
    while True:
        pid = agent_glow.daemon_pid(paths.root)
        if pid:
            return pid
        if time.monotonic() >= end:
            return None
        time.sleep(0.25)


# ---------------------------------------------------------------- install / uninstall

def udev_help(command):
    return [
        "  Linux needs a udev rule before agent-glow may talk to the keyboard:",
        f"    {command} udev-rule | sudo tee /etc/udev/rules.d/60-agent-glow.rules",
        "    sudo udevadm control --reload-rules && sudo udevadm trigger",
        "  then unplug and replug the keyboard.",
    ]


def install(paths, agents, keyboard=None, use_venv=True, service=None, run=subprocess.run):
    if sys.version_info < MIN_PYTHON:
        raise InstallError(f"agent-glow needs Python {'.'.join(map(str, MIN_PYTHON))} or newer")
    # Parse and validate every config first so a bad file aborts before anything is written.
    edits = []
    for agent, path, ours in (
        ("claude", paths.claude, grouped_hooks(paths, "claude", CLAUDE_HOOKS)),
        ("codex", paths.codex, grouped_hooks(paths, "codex", CODEX_HOOKS)),
    ):
        if agent in agents:
            data, text = read_config(path)
            add_hooks(data, ours, path)
            edits.append((agent, path, text, render_json(data, text)))
    if "copilot" in agents:
        edits.append(("copilot", paths.copilot, None, render_json(copilot_config(paths), None)))
    if keyboard:
        read_config(paths.config)

    if service:
        service.stop(paths)  # an old daemon must not hold files that are about to be replaced

    # The runtime goes in before any hook can point at it.
    command = install_runtime(paths, use_venv, run)
    say(f"runtime : {paths.root}" + (" (python venv with hidapi)" if use_venv else ""))
    say(f"command : {command}")
    if keyboard:
        set_keyboard(paths, keyboard)
        say(f"keyboard: {keyboard[0]:04x}:{keyboard[1]:04x} chosen in {paths.config}")

    usable, report = probe_keyboard(paths, run)
    say("keyboard: " + ("ready" if usable else "not usable yet"))
    for line in report.splitlines():
        say(f"          {line}".rstrip())
    needs_udev = paths.platform == "linux" and "cannot open" in report

    for agent, path, text, new in edits:
        backup_once(path, text)
        write_file(path, new)
        say(f"{agent:8}: hooks registered in {path}")

    if service:
        try:
            what = service.start(paths)
        except ServiceError as e:
            say(f"daemon  : not started automatically: {e}")
            say(f"          run `{command} daemon` at login yourself")
        else:
            pid = wait_for_daemon(paths)
            say(f"daemon  : {what}, " + (f"running (pid {pid})" if pid else
                                          f"not running yet, see {paths.log}"))
    else:
        say(f"daemon  : no background service (--no-service); run `{command} daemon` yourself")

    say("")
    say("Next steps:")
    if needs_udev:
        for line in udev_help(command):
            say(line)
    if "codex" in agents:
        say("  * Codex: run /hooks once and trust the agent-glow hooks (Codex skips untrusted hooks).")
    say("  * Restart running Claude Code / Codex / Copilot sessions; they load hooks when a session starts.")
    say(f"  * Try it: {command} test")


def uninstall(paths, service=None, run=subprocess.run):
    if service:
        what = service.remove(paths)
        if what:
            say(f"daemon  : {what} removed")

    for agent, path, delete_if_empty in (("claude", paths.claude, False), ("codex", paths.codex, True)):
        data, text = read_config(path)
        if text is None:
            continue
        remove_hooks(data)
        if delete_if_empty and not data:
            remove_file(path)
        else:
            new = render_json(data, text)
            if new != text:
                write_file(path, new)
        backup = path + BACKUP_SUFFIX
        current = read_config(path)[0] if os.path.exists(path) else {}
        if os.path.exists(backup) and read_config(backup)[0] == current:
            remove_file(backup)  # nothing but our hooks differed, so the backup has no value left
        say(f"{agent:8}: hooks removed from {path}")
    if os.path.exists(paths.copilot):
        remove_file(paths.copilot)
        say(f"copilot : removed {paths.copilot}")

    if (os.path.exists(os.path.join(paths.root, "snapshot.json")) and os.path.exists(paths.python)
            and os.path.exists(paths.script)):
        # The lighting was left changed (daemon killed mid-alert); put it back before the script goes.
        run([paths.python, paths.script, "restore"], capture_output=True, text=True, env=paths.env())
    if link_is_ours(paths):
        remove_file(paths.link)
    if os.path.isdir(paths.root):
        try:
            shutil.rmtree(paths.root)
        except OSError as e:
            raise InstallError(f"could not remove {paths.root}: {e}") from None
        say(f"runtime : removed {paths.root}")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uninstall", action="store_true", help="remove everything this installed")
    parser.add_argument("--agents", default=",".join(AGENTS),
                        help="comma separated agents to register hooks for (default: all)")
    parser.add_argument("--keyboard", metavar="VID:PID",
                        help="use this keyboard (see `agent-glow devices`) instead of the first one found")
    parser.add_argument("--no-service", action="store_true",
                        help="do not run the daemon in the background at login")
    parser.add_argument("--no-venv", action="store_true",
                        help="run hooks and daemon with this python (needs hidapi) instead of a private venv")
    parser.add_argument("--home", help="install below this directory instead of the real home (for testing)")
    args = parser.parse_args(argv)
    agents = tuple(a.strip() for a in args.agents.split(",") if a.strip())
    unknown = [a for a in agents if a not in AGENTS]
    if unknown or not agents:
        parser.error(f"--agents takes {', '.join(AGENTS)}; got {args.agents!r}")
    args.agents = agents
    if args.keyboard is not None:
        parsed = agent_glow.parse_vid_pid(args.keyboard)
        if not parsed:
            parser.error(f"--keyboard takes VID:PID in hex, e.g. 4d4c:6505; got {args.keyboard!r}")
        args.keyboard = parsed
    return args


def main(argv=None, run=subprocess.run, platform=None, env=None, spawn=spawn_detached, registry=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    home = os.path.abspath(args.home) if args.home else os.path.expanduser("~")
    if env is None:
        env = {} if args.home else dict(os.environ)
    platform = platform_name(platform or sys.platform)
    paths = Paths(home, env, platform, use_venv=not args.no_venv)
    service = None if args.no_service else service_for(platform, run, spawn, registry)
    try:
        if args.uninstall:
            uninstall(paths, service=service, run=run)
        else:
            install(paths, args.agents, keyboard=args.keyboard, use_venv=not args.no_venv,
                    service=service, run=run)
    except InstallError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
