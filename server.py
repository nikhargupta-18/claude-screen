#!/usr/bin/env python3
"""claude-screen: an MCP server that lets Claude use GUI apps without taking over the user's.

Two modes:
  * Claude's screen - a separate nested X display (Xephyr) shown as one window on the user's
    desktop, with Claude's own cursor and keyboard focus. Any app works, input is real
    (xdotool), and nothing touches the user's pointer, focus or windows.
  * Your desktop    - the user's own open apps, driven only through the accessibility API
    (AT-SPI): read their UI, press buttons, set field text. Like Codex on macOS, it never moves
    the user's cursor or focus, but it only works for apps that expose accessibility.

Perception is text-first (AT-SPI snapshot with refs, like the macOS AX tree); screenshots are
1280x800 and 1:1 with Claude's screen. Speaks MCP (JSON-RPC, one message per line) over stdio.
Needs: Xephyr, xdotool, openbox, PyGObject with Atspi, Pillow.
"""
import base64
import ctypes
import ctypes.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402
from PIL import ImageGrab  # noqa: E402

DISPLAY_NUM = 73
DISPLAY = f":{DISPLAY_NUM}"
WIDTH, HEIGHT = 1280, 800  # Anthropic's recommended size for web work; screenshots are 1:1
DATA_DIR = os.path.expanduser("~/.local/share/claude-screen")
CHROME_PROFILE = os.path.join(DATA_DIR, "chrome-profile")
SETTLE = 0.6       # seconds to let the UI settle before observing
STEP_GAP = 0.12    # pause between batched actions
UI_MAX_LINES, UI_MAX_NODES, UI_BUDGET = 300, 8000, 8.0
# Never drive these on the user's desktop: the shell (system menus, polkit password dialogs)
# and the portals (permission prompts Claude must not approve for itself).
DESKTOP_EXCLUDE = {"gnome-shell", "mutter-x11-frames", "xdg-desktop-portal-gnome",
                   "xdg-desktop-portal-gtk", "xdg-desktop-portal-kde", "polkit-gnome-authentication-agent-1"}
PRESS_ACTIONS = ("click", "press", "activate", "jump", "toggle", "open", "select")
KEY_ALIASES = {"enter": "Return", "esc": "Escape", "del": "Delete", "backspace": "BackSpace",
               "pgup": "Prior", "pgdn": "Next", "pageup": "Prior", "pagedown": "Next",
               "win": "super", "cmd": "super", "meta": "super", "control": "ctrl"}
BUTTON = {"click": 1, "double_click": 1, "triple_click": 1, "middle_click": 2, "right_click": 3}
CLICKS = {"click": 1, "double_click": 2, "triple_click": 3, "middle_click": 1, "right_click": 1}
SCROLL = {"up": 4, "down": 5, "left": 6, "right": 7}


def _roles(*names):
    return {getattr(Atspi.Role, n) for n in names if hasattr(Atspi.Role, n)}


R = Atspi.Role
S = Atspi.StateType
ROLE_NAMES = {  # how roles are printed in read_ui
    R.PUSH_BUTTON: "button", R.TOGGLE_BUTTON: "toggle", R.CHECK_BOX: "checkbox",
    R.RADIO_BUTTON: "radio", R.LINK: "link", R.ENTRY: "textbox", R.PASSWORD_TEXT: "password",
    R.COMBO_BOX: "combobox", R.MENU_ITEM: "menuitem", R.CHECK_MENU_ITEM: "menuitem",
    R.RADIO_MENU_ITEM: "menuitem", R.PAGE_TAB: "tab", R.SPIN_BUTTON: "spinbutton",
    R.SLIDER: "slider", R.LIST_ITEM: "option", R.TEXT: "textbox", R.MENU: "menu",
}
INTERACTIVE = set(ROLE_NAMES) - {R.LIST_ITEM, R.TEXT, R.MENU}
CONTAINERS = _roles("APPLICATION", "FRAME", "WINDOW", "DOCUMENT_WEB", "DOCUMENT_FRAME",
                    "INTERNAL_FRAME", "SCROLL_PANE", "PANEL", "FILLER", "SECTION", "GROUPING",
                    "TOOL_BAR", "STATUS_BAR", "LIST", "LIST_BOX", "TABLE", "TREE", "FORM",
                    "LANDMARK", "PAGE_TAB_LIST", "MENU_BAR", "ARTICLE", "BLOCK_QUOTE")
TEXTUAL = _roles("HEADING", "LABEL", "STATIC", "PARAGRAPH", "CAPTION", "ALERT", "NOTIFICATION")
TEXT_INPUTS = {R.ENTRY, R.PASSWORD_TEXT, R.TEXT, R.COMBO_BOX, R.SPIN_BUTTON}
_A11Y_BUS = []


def a11y_bus():
    """Address of the session's accessibility bus (apps on Claude's display can't find it alone)."""
    if not _A11Y_BUS:
        r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path",
                            "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"'([^']+)'", r.stdout)
        _A11Y_BUS.append(m.group(1) if m else None)
    return _A11Y_BUS[0]


def x_env():
    """Environment that sends a program to Claude's screen instead of the user's desktop."""
    env = dict(os.environ, DISPLAY=DISPLAY, GDK_BACKEND="x11", QT_QPA_PLATFORM="xcb",
               ACCESSIBILITY_ENABLED="1")  # makes Chrome expose its accessibility tree
    env.pop("WAYLAND_DISPLAY", None)
    if a11y_bus():
        env["AT_SPI_BUS_ADDRESS"] = a11y_bus()
    return env


def spawn(cmd, env):
    """Start a long-lived program detached, so it outlives this server (Claude's screen persists)."""
    return subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def xdo(*args):
    r = subprocess.run(["xdotool", *map(str, args)], env=x_env(), capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"xdotool {args[0]} failed: {r.stderr.strip() or r.returncode}")
    return r.stdout


_X11 = ctypes.CDLL(ctypes.util.find_library("X11"))
_XTST = ctypes.CDLL(ctypes.util.find_library("Xtst"))
_X11.XOpenDisplay.restype = ctypes.c_void_p
_X11.XOpenDisplay.argtypes = [ctypes.c_char_p]
_X11.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char * 32]
_X11.XFlush.argtypes = _X11.XCloseDisplay.argtypes = [ctypes.c_void_p]
_XTST.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]


def release_held_keys():
    """Release every key the X server thinks is down on Claude's screen.

    Xephyr mirrors the host keyboard while its window has focus, so e.g. an Alt+Tab away from
    it can leave Alt held forever, turning Claude's typing into shortcuts.
    """
    dpy = _X11.XOpenDisplay(DISPLAY.encode())
    if not dpy:
        return
    try:
        keys = (ctypes.c_char * 32)()
        _X11.XQueryKeymap(dpy, keys)
        for i, byte in enumerate(keys.raw):
            for bit in range(8):
                if byte >> bit & 1:
                    _XTST.XTestFakeKeyEvent(dpy, i * 8 + bit, 0, 0)
        _X11.XFlush(dpy)
    finally:
        _X11.XCloseDisplay(dpy)


def safe(fn, default=None):
    try:
        return fn()
    except (GLib.Error, TypeError, AttributeError, NotImplementedError):
        return default


class Screen:
    def __init__(self):
        # read_ui ref -> {"target", "acc", "xy"}; valid until the next read_ui
        self.refs = {}
        self.desktop_app = None  # app filter of the last desktop read_ui

    # -- Claude's screen lifecycle ---------------------------------------
    def alive(self):
        return bool(shutil.which("xdotool")) and subprocess.run(
            ["xdotool", "getdisplaygeometry"], env=x_env(), capture_output=True).returncode == 0

    def ensure(self):
        missing = [t for t in ("Xephyr", "xdotool", "openbox") if not shutil.which(t)]
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)}; install with: "
                               "sudo dnf install xorg-x11-server-Xephyr xdotool openbox")
        if self.alive():
            return
        self._clear_stale_lock()
        # Xephyr is a client of the user's desktop, so it gets the inherited environment.
        spawn(["Xephyr", DISPLAY, "-screen", f"{WIDTH}x{HEIGHT}", "-title", "Claude's screen",
               "-no-host-grab", "-sw-cursor", "-br", "-nolisten", "tcp"], os.environ.copy())
        deadline = time.time() + 10
        while not self.alive():
            if time.time() > deadline:
                raise RuntimeError(f"Claude's screen ({DISPLAY}) did not start")
            time.sleep(0.2)
        spawn(["openbox", "--sm-disable"], x_env())
        time.sleep(0.8)

    @staticmethod
    def _clear_stale_lock():
        lock = f"/tmp/.X{DISPLAY_NUM}-lock"
        try:
            pid = int(open(lock).read().strip())
            os.kill(pid, 0)
            return  # a live server owns it; let Xephyr report the conflict
        except (OSError, ValueError):
            pass
        for path in (lock, f"/tmp/.X11-unix/X{DISPLAY_NUM}"):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    # -- perception ------------------------------------------------------
    def screenshot(self):
        img = ImageGrab.grab(xdisplay=DISPLAY)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _screen_pids(self):
        """PIDs owning visible windows on Claude's screen."""
        if not self.alive():
            return set()
        pids = set()
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""], env=x_env(),
                             capture_output=True, text=True).stdout
        for wid in out.split():
            r = subprocess.run(["xdotool", "getwindowpid", wid], env=x_env(),
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                pids.add(int(r.stdout))
        return pids

    @staticmethod
    def _apps():
        desktop = Atspi.get_desktop(0)
        return [a for a in (safe(lambda i=i: desktop.get_child_at_index(i))
                            for i in range(desktop.get_child_count())) if a is not None]

    def _desktop_apps(self):
        mine = self._screen_pids()
        return [a for a in self._apps()
                if safe(a.get_process_id) not in mine and safe(a.get_name, "") not in DESKTOP_EXCLUDE]

    @staticmethod
    def _window_titles(app):
        titles = (safe(lambda i=i: app.get_child_at_index(i).get_name(), "")
                  for i in range(safe(app.get_child_count, 0)))
        return [t for t in titles if t]

    def list_desktop_apps(self):
        lines = ["Apps on the user's desktop (read one with read_ui target=desktop "
                 "app=<app name or window title>):"]
        for a in self._desktop_apps():
            wins = self._window_titles(a)
            lines.append(f"- {safe(a.get_name, '?')}" + (f": {'; '.join(wins)[:200]}" if wins else ""))
        return "\n".join(lines)

    def read_ui(self, target="screen", app=None):
        """Accessibility snapshot: Claude's screen, or one app on the user's desktop."""
        if target == "screen":
            pids = self._screen_pids()
            apps = [a for a in self._apps() if safe(a.get_process_id) in pids]
        else:
            if not app:
                return self.list_desktop_apps()
            want = app.lower()
            apps = [a for a in self._desktop_apps()
                    if want in safe(a.get_name, "").lower()
                    or any(want in t.lower() for t in self._window_titles(a))]
            if not apps:
                raise ValueError(f"no accessible desktop app matches {app!r}; call read_ui with "
                                 "target=desktop and no app to list them")
            self.desktop_app = app
        geo = target == "screen"  # Wayland gives no usable global coordinates for desktop apps
        lines, refs, st = [], {}, {"nodes": 0, "last": None}
        deadline = time.time() + UI_BUDGET

        def emit(line):
            if len(lines) < UI_MAX_LINES:
                lines.append(line)

        def walk(acc):
            if st["nodes"] >= UI_MAX_NODES or time.time() > deadline or len(lines) >= UI_MAX_LINES:
                return
            st["nodes"] += 1
            try:
                role, states = acc.get_role(), acc.get_state_set()
                if role != R.APPLICATION and not states.contains(S.SHOWING):
                    return  # hidden subtree
                name = " ".join((acc.get_name() or "").split())
                editable = role in TEXT_INPUTS and states.contains(S.EDITABLE)
                interactive = editable or role in INTERACTIVE or (
                    name and role not in CONTAINERS and role not in TEXTUAL
                    and states.contains(S.FOCUSABLE))
                if interactive or (role in TEXTUAL and name):
                    xy = None
                    if geo:
                        e = acc.get_extents(Atspi.CoordType.SCREEN)
                        xy = (e.x + e.width // 2, e.y + e.height // 2)
                        if not (e.width > 0 and e.height > 0 and 0 <= xy[0] < WIDTH and 0 <= xy[1] < HEIGHT):
                            xy = False  # off-screen: skip the node itself, keep its children
                    if xy is not False and interactive:
                        n = len(refs) + 1
                        refs[n] = {"target": target, "acc": acc, "xy": xy}
                        extra = []
                        if editable and role != R.PASSWORD_TEXT:
                            text = safe(lambda: Atspi.Text.get_text(
                                acc, 0, Atspi.Text.get_character_count(acc)), "") or ""
                            extra.append(f"value={json.dumps(text[:80])}")
                        for s, word in ((S.FOCUSED, "focused"), (S.CHECKED, "checked"),
                                        (S.SELECTED, "selected"), (S.EXPANDED, "expanded")):
                            if states.contains(s):
                                extra.append(word)
                        if not states.contains(S.ENABLED) and not states.contains(S.SENSITIVE):
                            extra.append("disabled")
                        label = ROLE_NAMES.get(role) or acc.get_role_name()
                        where = f" @{xy[0]},{xy[1]}" if xy else ""
                        emit(f"[{n}] {label} {json.dumps(name[:100])}"
                             f"{' ' + ' '.join(extra) if extra else ''}{where}")
                    elif xy is not False and name != st["last"]:
                        emit(("# " if role == R.HEADING else "") + name[:160])
                    st["last"] = name
                for i in range(acc.get_child_count()):
                    child = acc.get_child_at_index(i)
                    if child is not None:
                        walk(child)
            except GLib.Error:
                return  # element vanished mid-walk

        for a in apps:
            emit(f"== app: {safe(a.get_name, '?')}")
            walk(a)
        self.refs = refs
        if not apps:
            lines.append("(no accessible apps on Claude's screen; use screenshot)")
        if st["nodes"] >= UI_MAX_NODES or time.time() > deadline or len(lines) >= UI_MAX_LINES:
            lines.append("... (truncated)")
        return "\n".join(lines)

    def observe(self, mode):
        out = []
        if mode in ("ui", "both"):
            out.append({"type": "text", "text": self.read_ui()})
        if mode in ("screenshot", "both"):
            out.append({"type": "image", "data": self.screenshot(), "mimeType": "image/png"})
        return out

    # -- actions on Claude's screen (real input, Claude's own cursor) -----
    def _ref(self, a, target):
        ref = self.refs.get(a.get("ref"))
        if ref is None:
            raise ValueError(f"unknown ref {a.get('ref')}; call read_ui again")
        if ref["target"] != target:
            raise ValueError(f"ref {a['ref']} belongs to target={ref['target']}")
        return ref

    def _point(self, a, kx="x", ky="y"):
        if kx == "x" and "ref" in a:
            return self._ref(a, "screen")["xy"]
        if kx not in a or ky not in a:
            raise ValueError(f"{a['action']} needs {kx},{ky}" + (" or ref" if kx == "x" else ""))
        return min(max(int(a[kx]), 0), WIDTH - 1), min(max(int(a[ky]), 0), HEIGHT - 1)

    def do(self, a):
        act = a["action"]
        if act in BUTTON:
            x, y = self._point(a)
            xdo("mousemove", x, y, "sleep", 0.05, "click", "--repeat", CLICKS[act],
                "--delay", 90, BUTTON[act])
        elif act == "move":
            xdo("mousemove", *self._point(a))
        elif act == "drag":
            (x1, y1), (x2, y2) = self._point(a), self._point(a, "to_x", "to_y")
            xdo("mousemove", x1, y1, "mousedown", 1, "sleep", 0.1,
                "mousemove", (x1 + x2) // 2, (y1 + y2) // 2, "sleep", 0.05,
                "mousemove", x2, y2, "sleep", 0.1, "mouseup", 1)
        elif act == "scroll":
            x, y = self._point(a)
            xdo("mousemove", x, y, "click", "--repeat", int(a.get("amount", 3)), "--delay", 40,
                SCROLL[a.get("direction", "down")])
        elif act == "type":
            # xdotool types characters missing from the keymap by briefly remapping a spare key;
            # apps miss the remap when several arrive back to back, so send those slowly.
            for chunk in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]", a["text"]):
                if chunk.isascii():
                    xdo("type", "--delay", 10, "--", chunk)
                else:
                    xdo("type", "--delay", 60, "--", chunk)
                    time.sleep(0.15)
        elif act == "key":
            combos = ["+".join(KEY_ALIASES.get(p.lower(), p) for p in c.split("+"))
                      for c in a["keys"].split()]
            xdo("key", "--delay", 40, "--", *combos)
        elif act == "wait":
            time.sleep(min(float(a.get("seconds", 1)), 30))
        else:
            raise ValueError(f"unknown action {act!r}")

    def open_url(self, url):
        if not re.match(r"^https?://", url):
            raise ValueError("url must start with http:// or https://")
        chrome = shutil.which("google-chrome") or shutil.which("chromium-browser")
        if not chrome:
            raise RuntimeError("Google Chrome is not installed")
        # Same profile dir => a running instance just opens a new tab; otherwise it starts here.
        spawn([chrome, f"--user-data-dir={CHROME_PROFILE}", "--no-first-run",
               "--no-default-browser-check", "--force-renderer-accessibility",
               "--ozone-platform=x11", "--window-position=0,0",
               f"--window-size={WIDTH},{HEIGHT}", "--start-maximized", url], x_env())

    # -- actions on the user's desktop (accessibility only) ---------------
    def desktop_do(self, a):
        acc = self._ref(a, "desktop")["acc"]
        act = a["action"]
        if act == "press":
            n = safe(lambda: Atspi.Action.get_n_actions(acc), 0)
            names = [safe(lambda i=i: Atspi.Action.get_name(acc, i), "") for i in range(n)]
            idx = next((i for i, nm in enumerate(names) if nm in PRESS_ACTIONS), 0 if n else None)
            if idx is None or not Atspi.Action.do_action(acc, idx):
                raise RuntimeError(f"ref {a['ref']} has no press action; it cannot be driven "
                                   "without the pointer (use Claude's screen for this)")
        elif act == "set_text":
            if not safe(lambda: Atspi.EditableText.set_text_contents(acc, a["text"]), False):
                raise RuntimeError(f"ref {a['ref']} does not accept text via accessibility")
        else:
            raise ValueError(f"unknown desktop action {act!r}")


# -- MCP layer ------------------------------------------------------------
OBSERVE = {"type": "string", "enum": ["screenshot", "ui", "both", "none"], "default": "screenshot",
           "description": "What to return afterwards. 'ui' (accessibility text) is far cheaper "
                          "than a screenshot; use 'screenshot' when layout or images matter."}
ACTION = {"type": "object", "required": ["action"], "properties": {
    "action": {"type": "string", "enum": [*BUTTON, "move", "drag", "scroll", "type", "key", "wait"]},
    "ref": {"type": "integer", "description": "element ref from the latest read_ui (instead of x,y)"},
    "x": {"type": "integer"}, "y": {"type": "integer"},
    "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
    "text": {"type": "string", "description": "for type; newline presses Enter"},
    "keys": {"type": "string", "description": "for key: e.g. 'ctrl+l', 'Tab Tab Return'"},
    "direction": {"type": "string", "enum": list(SCROLL)},
    "amount": {"type": "integer", "description": "scroll notches, default 3"},
    "seconds": {"type": "number", "description": "for wait, max 30"}}}
DESKTOP_ACTION = {"type": "object", "required": ["action", "ref"], "properties": {
    "action": {"type": "string", "enum": ["press", "set_text"]},
    "ref": {"type": "integer", "description": "ref from the latest read_ui with target=desktop"},
    "text": {"type": "string", "description": "for set_text: replaces the field's contents"}}}
TOOLS = [
    ("screenshot", "Screenshot of Claude's own screen (1280x800). Coordinates in act are pixels "
     "of this image.", {"type": "object", "properties": {}}),
    ("read_ui", "Accessibility snapshot as text, in reading order: buttons, links, fields (with "
     "current values), checkboxes and visible text, each interactive element with a [ref]. "
     "target=screen (default): apps on Claude's own screen, refs usable in act. target=desktop: "
     "the user's own open apps; without app it lists them, with app (name substring) it reads "
     "that app, refs usable in desktop_act. Much cheaper than a screenshot.",
     {"type": "object", "properties": {
         "target": {"type": "string", "enum": ["screen", "desktop"], "default": "screen"},
         "app": {"type": "string", "description": "desktop app name substring, e.g. 'Firefox'"}}}),
    ("act", "Run a batch of real input steps (Claude's own cursor and keyboard) in order on "
     "Claude's screen, then observe once. Batch whole sequences (click field, type, Tab, type, "
     "click Submit) in one call. Stops at the first failing step. Never affects the user's screen.",
     {"type": "object", "required": ["actions"], "properties": {
         "actions": {"type": "array", "items": ACTION, "minItems": 1}, "observe": OBSERVE}}),
    ("desktop_act", "Act on the user's own open apps through accessibility only: press buttons, "
     "links, checkboxes and menu items, or replace a text field's contents. Never moves the "
     "user's cursor or keyboard focus, so there is no typing of keys, clicking by coordinates, "
     "dragging or hovering. If an element can't be driven this way, do the task on Claude's "
     "screen instead. Returns a fresh read_ui of the same app unless observe='none'.",
     {"type": "object", "required": ["actions"], "properties": {
         "actions": {"type": "array", "items": DESKTOP_ACTION, "minItems": 1},
         "observe": {"type": "string", "enum": ["ui", "none"], "default": "ui"}}}),
    ("open_url", "Open a URL in Claude's own Chrome (separate profile) on Claude's screen; "
     "starts the screen and the browser if needed.", {"type": "object", "required": ["url"],
     "properties": {"url": {"type": "string"}, "observe": OBSERVE}}),
]
INSTRUCTIONS = (
    "claude-screen lets you use GUI apps without taking over the user's computer. Claude's screen "
    f"is your own display (X {DISPLAY}, shown to the user as the 'Claude's screen' window) with "
    "your own cursor and keyboard: use it for anything you drive yourself (open_url, act). "
    "read_ui target=desktop plus desktop_act work on the user's already-open apps through "
    "accessibility only. Work efficiently: read_ui first for structure and refs, batch steps "
    "with act, observe with 'ui' unless you need pixels. To run another GUI app on your screen, "
    f"start it from the shell with: DISPLAY={DISPLAY} GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb "
    "WAYLAND_DISPLAY= ACCESSIBILITY_ENABLED=1 <app> &. If you hit a CAPTCHA, login password or "
    "identity check, ask the user to complete it in the Claude's screen window.")


def step_failure(i, steps, err):
    rest = f"; steps {i + 1}-{len(steps)} not run" if i < len(steps) else ""
    return f"step {i} ({steps[i - 1].get('action')}) failed: {err}{rest}"


def call_tool(scr, name, a):
    if name == "read_ui":
        target = a.get("target", "screen")
        if target == "screen":
            scr.ensure()
        return [{"type": "text", "text": scr.read_ui(target, a.get("app"))}]
    if name == "desktop_act":
        steps = a["actions"]
        for i, step in enumerate(steps, 1):
            try:
                scr.desktop_do(step)
            except Exception as e:
                raise RuntimeError(step_failure(i, steps, e)) from e
            time.sleep(STEP_GAP)
        out = [{"type": "text", "text": f"ran {len(steps)} step(s)"}]
        if a.get("observe", "ui") == "ui" and scr.desktop_app:
            time.sleep(SETTLE)
            out.append({"type": "text", "text": scr.read_ui("desktop", scr.desktop_app)})
        return out
    scr.ensure()
    if name == "screenshot":
        return scr.observe("screenshot")
    if name == "open_url":
        scr.open_url(a["url"])
        time.sleep(3)
        return [{"type": "text", "text": f"opened {a['url']}"}, *scr.observe(a.get("observe", "screenshot"))]
    if name == "act":
        steps = a["actions"]
        release_held_keys()
        for i, step in enumerate(steps, 1):
            try:
                scr.do(step)
            except Exception as e:
                raise RuntimeError(step_failure(i, steps, e)) from e
            time.sleep(STEP_GAP)
        time.sleep(SETTLE)
        return [{"type": "text", "text": f"ran {len(steps)} step(s)"}, *scr.observe(a.get("observe", "screenshot"))]
    raise ValueError(f"unknown tool {name}")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    Atspi.init()
    scr = Screen()
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            continue
        if method == "initialize":
            result = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS,
                      "serverInfo": {"name": "claude-screen", "version": "2.1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
        elif method == "tools/call":
            p = msg["params"]
            try:
                result = {"content": call_tool(scr, p["name"], p.get("arguments") or {})}
            except Exception as e:  # report to the model, keep serving
                print(f"claude-screen: {p['name']} failed: {e!r}", file=sys.stderr)
                result = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": result})


if __name__ == "__main__":
    main()
