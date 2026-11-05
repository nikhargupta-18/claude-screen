# claude-screen

An MCP server that lets Claude (or any MCP client) use GUI apps on Linux **without taking over your mouse and keyboard**.

Computer-use tools usually drive your real cursor, so you can't touch your machine while the agent works. claude-screen gives the agent its own screen instead, and a second, hands-off way to work with the apps you already have open.

## Two modes

**Claude's screen.** A separate nested X display (Xephyr) that appears as one window on your desktop. The agent gets its own cursor, keyboard focus and Chrome profile. Input is real (xdotool), so any app works, and nothing touches your pointer, focus or windows.

**Your desktop.** Your own open apps, driven only through the accessibility API (AT-SPI). The agent reads the UI as text, presses buttons and sets field text, but never moves your cursor or steals focus. It only works for apps that expose accessibility. The GNOME shell, polkit password dialogs and xdg portals are excluded on purpose, so the agent can't approve permission prompts for itself.

Perception is text-first: `read_ui` returns an accessibility snapshot with numbered refs, which is much cheaper than screenshots. Screenshots are 1280x800 and match Claude's screen pixel for pixel.

## Tools

| Tool | What it does |
| --- | --- |
| `read_ui` | Accessibility snapshot of Claude's screen (`target=screen`) or one of your apps (`target=desktop`, `app=<name>`) |
| `act` | Batch of real input steps on Claude's screen: click, double/right click, drag, scroll, type, key combos, wait |
| `desktop_act` | `press` or `set_text` on your own apps, through accessibility only |
| `open_url` | Opens a URL in Claude's own Chrome on Claude's screen |
| `screenshot` | PNG of Claude's screen |

## Requirements

Linux with an X11 or Wayland desktop (tested on Fedora, GNOME Wayland), Python 3, and:

```sh
sudo dnf install xorg-x11-server-Xephyr xdotool openbox python3-gobject at-spi2-core python3-pillow
# Debian/Ubuntu: sudo apt install xserver-xephyr xdotool openbox python3-gi gir1.2-atspi-2.0 python3-pil
```

Google Chrome (or Chromium) is needed for `open_url`.

For desktop mode, turn on accessibility (`gsettings set org.gnome.desktop.interface toolkit-accessibility true`). Chrome only exposes its page content when launched with `--force-renderer-accessibility`.

## Install

```sh
git clone https://github.com/nikhargupta-18/claude-screen.git
claude mcp add --scope user claude-screen -- python3 /path/to/claude-screen/server.py
```

Any MCP client that speaks stdio works the same way. Claude's screen starts on display `:73` the first time a tool needs it, and its Chrome profile lives in `~/.local/share/claude-screen/chrome-profile`.

## Tips

- Run other apps on Claude's screen with `DISPLAY=:73 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb WAYLAND_DISPLAY= ACCESSIBILITY_ENABLED=1 <app> &`.
- If the agent hits a CAPTCHA, password or identity check, complete it yourself in the "Claude's screen" window.
- Pass `observe: "ui"` to `act` to get text back instead of a screenshot.

## License

MIT
