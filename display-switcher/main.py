"""Display mode switcher for Fenn: PC screen only / Duplicate / Extend / Second screen only.

Switching runs Windows' own DisplaySwitch.exe, the program behind Win+P. The highlighted
mode is read back from Windows' display configuration every couple of seconds, so it stays
right when you change mode some other way (Win+P, Settings).
"""

import ctypes
import os
import subprocess
import sys
import time

import fenn

MODES = [  # (id, icon, short label, full label); same order as the Win+P menu
    ("internal", "laptop", "PC screen", "PC screen only"),
    ("clone", "copy", "Duplicate", "Duplicate"),
    ("extend", "split-horizontal", "Extend", "Extend"),
    ("external", "monitor", "Second screen", "Second screen only"),
]
ICON = {m: icon for m, icon, _, _ in MODES}
SHORT = {m: short for m, _, short, _ in MODES}
FULL = {m: full for m, _, _, full in MODES}

SWITCH_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                          "DisplaySwitch.exe")
TOPOLOGY = {1: "internal", 2: "clone", 4: "extend", 8: "external"}  # DISPLAYCONFIG_TOPOLOGY_*
QDC_DATABASE_CURRENT = 0x4
PATH_INFO_SIZE, MODE_INFO_SIZE = 72, 64  # sizes of DISPLAYCONFIG_PATH_INFO / _MODE_INFO
SETTLE_SECONDS = 4  # Windows needs a moment to apply a change; don't read it back sooner

app = fenn.Plugin()
mode = None            # the active mode id, or None if Windows won't say
settle_until = 0.0
last_tile = None
last_page = "unset"
_last_warning = None


def warn_once(message):
    global _last_warning
    if message != _last_warning:
        _last_warning = message
        print(message, file=sys.stderr, flush=True)


def current_mode():
    """The active display mode ('internal', 'clone', 'extend', 'external'), or None."""
    try:
        user32 = ctypes.windll.user32
        paths, modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
        if user32.GetDisplayConfigBufferSizes(QDC_DATABASE_CURRENT, ctypes.byref(paths),
                                              ctypes.byref(modes)):
            return None
        path_buf = ctypes.create_string_buffer(max(1, paths.value) * PATH_INFO_SIZE)
        mode_buf = ctypes.create_string_buffer(max(1, modes.value) * MODE_INFO_SIZE)
        topology = ctypes.c_uint32(0)
        if user32.QueryDisplayConfig(QDC_DATABASE_CURRENT, ctypes.byref(paths), path_buf,
                                     ctypes.byref(modes), mode_buf, ctypes.byref(topology)):
            return None
        return TOPOLOGY.get(topology.value)
    except (AttributeError, OSError):  # not Windows, or the call is unavailable
        return None


def apply(mode_id):
    try:
        subprocess.Popen([SWITCH_EXE, "/" + mode_id])  # returns at once; Windows does the rest
        return True
    except OSError as e:
        warn_once(f"could not run DisplaySwitch.exe: {e}")
        return False


def choose(mode_id):
    global mode, settle_until
    if apply(mode_id):
        mode = mode_id  # show the choice straight away; refresh() confirms it later
        settle_until = time.monotonic() + SETTLE_SECONDS
        draw()


def refresh():
    global mode
    if time.monotonic() < settle_until:
        return
    found = current_mode()
    if found and found != mode:
        mode = found
        draw()


def cells(size):
    """(mode id, x, y, w, h) of each button on the tile's canvas."""
    if size == "large":  # 2x2 grid with a caption underneath
        return [(m, (i % 2) * 52, (i // 2) * 40, 52, 40) for i, (m, *_) in enumerate(MODES)]
    return [(m, i * 26, 0, 26, 36) for i, (m, *_) in enumerate(MODES)]


def tile_view(size):
    tip = f"Display: {FULL[mode]}" if mode else "Display mode"
    if size == "small":  # no room for four buttons: show the mode, a click opens the page
        return fenn.column([fenn.icon(ICON.get(mode, "monitor"), size=24,
                                      color="accent" if mode else "dim")],
                           align="center", justify="center", tooltip=tip)
    large = size == "large"
    shapes = []
    for m, x, y, w, h in cells(size):
        active = m == mode
        if active:
            shapes.append(fenn.rect(x + 2, y + 3, w - 4, h - 6, fill="accent", radius=8))
        shapes.append(fenn.glyph(x + w / 2, y + h / 2, ICON[m], size=22 if large else 16,
                                 color="on_accent" if active else ("dim" if mode else "text")))
    if large:
        shapes.append(fenn.label(52, 94, SHORT[mode] if mode else "Display mode", size=11,
                                 color="dim", anchor="center"))
    return fenn.canvas(104 if large else 36, shapes, width=104, id="modes", tooltip=tip)


def page_view():
    caption = f"Now: {FULL[mode]}" if mode else "Pick a display mode"
    buttons = [fenn.button(m, label=short, icon=icon, primary=(m == mode), width=0)
               for m, icon, short, _ in MODES]
    return fenn.column([
        fenn.text(caption, size="small", color="dim"),
        fenn.row(buttons, gap=8),
        fenn.text("Same as Win+P. Second screen only turns off your PC screen.",
                  size="small", color="faint", wrap=True),
    ], gap=10)


def draw():
    """Sends the tile and page, each only when what it shows has changed."""
    global last_tile, last_page
    size = app.tile_size or "wide"
    if (size, mode) != last_tile:
        last_tile = (size, mode)
        app.tile(tile_view(size))
    if mode != last_page:
        last_page = mode
        app.page(page_view())


@app.on("modes")
def on_tile_click(event):
    point = event.get("value") or {}
    x, y = point.get("x", -1), point.get("y", -1)
    for m, cx, cy, w, h in cells(app.tile_size or "wide"):
        if cx <= x < cx + w and cy <= y < cy + h:
            choose(m)
            return


for _mode_id, *_ in MODES:
    app.on(_mode_id)(lambda event, m=_mode_id: choose(m))  # the page's buttons


@app.on_message("layout")
def on_layout(message):
    draw()  # the user resized the tile


if __name__ == "__main__":
    mode = current_mode()
    app.every(2.0, refresh)
    draw()
    app.run()
