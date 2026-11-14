"""Virtual desktop switcher for Fenn.

Shows which Windows virtual desktop you are on and jumps to another with a click.
The current desktop, the list and the names are read from the registry, where Explorer keeps
them, so no undocumented Windows interface is used. Switching presses Win+Ctrl+Left/Right,
the same keys you would, one step at a time, checking after each step that it worked.
"""

import ctypes
import os
import sys
import threading
import time
import uuid
from ctypes import wintypes

import fenn

try:
    import winreg
except ImportError:  # not Windows
    winreg = None

BASE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"
SESSION = r"Software\Microsoft\Windows\CurrentVersion\Explorer\SessionInfo\{}\VirtualDesktops"
MAX_BUTTONS = 20   # desktops listed on the page
WIDE_CELL, WIDE_MAX = 26, 6      # wide tile: one cell per desktop, up to this many
LARGE_COLS, LARGE_ROWS = 3, 3    # large tile grid
STEP_TIMEOUT = 0.8               # how long to wait for one Win+Ctrl+arrow to take effect

VK_LWIN, VK_LCONTROL, VK_LEFT, VK_RIGHT, VK_D = 0x5B, 0xA2, 0x25, 0x27, 0x44
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
EXTENDED_KEYS = {VK_LWIN, VK_LEFT, VK_RIGHT}


class MOUSEINPUT(ctypes.Structure):  # only here so INPUT gets its real size
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


# --- Reading the desktops from the registry --------------------------------------------------

def reg_value(path, name):
    """A value from HKEY_CURRENT_USER, or None."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def session_id():
    try:
        sid = wintypes.DWORD(0)
        ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return sid.value
    except (AttributeError, OSError):
        return 1


def guids(blob):
    """The 16-byte GUIDs packed one after another in a registry value."""
    if not blob or len(blob) % 16:
        return []
    blob = bytes(blob)
    return [uuid.UUID(bytes_le=blob[i:i + 16]) for i in range(0, len(blob), 16)]


def parse_state(ids_blob, current_blob, name_of):
    ids = guids(ids_blob)
    if not ids:  # one desktop and Windows hasn't written a list yet
        return {"count": 1, "current": 0, "names": ["Desktop 1"]}
    now = guids(current_blob)
    current = ids.index(now[0]) if now and now[0] in ids else 0  # unset until the first switch
    names = [str(name_of(g) or "").strip() or f"Desktop {i + 1}" for i, g in enumerate(ids)]
    return {"count": len(ids), "current": current, "names": names}


def read_state():
    if winreg is None:
        return None
    ids = reg_value(BASE, "VirtualDesktopIDs")
    current = (reg_value(BASE, "CurrentVirtualDesktop")  # newer Windows 11 builds
               or reg_value(SESSION.format(session_id()), "CurrentVirtualDesktop"))  # older ones
    return parse_state(ids, current,
                       lambda g: reg_value(BASE + "\\Desktops\\{" + str(g).upper() + "}", "Name"))


# --- Switching: the keys you would press yourself -----------------------------------------------

def press(vk):
    """Win+Ctrl+<key> through SendInput."""
    sequence = [(VK_LWIN, False), (VK_LCONTROL, False), (vk, False), (vk, True),
                (VK_LCONTROL, True), (VK_LWIN, True)]
    inputs = (INPUT * len(sequence))()
    for i, (key, up) in enumerate(sequence):
        inputs[i].type = 1  # INPUT_KEYBOARD
        inputs[i].ki.wVk = key
        inputs[i].ki.dwFlags = (KEYEVENTF_EXTENDEDKEY if key in EXTENDED_KEYS else 0) | (KEYEVENTF_KEYUP if up else 0)
    ctypes.windll.user32.SendInput(len(sequence), inputs, ctypes.sizeof(INPUT))


def step(direction, before):
    """One desktop left or right. True once Windows reports the change."""
    press(VK_RIGHT if direction == "right" else VK_LEFT)
    deadline = time.monotonic() + STEP_TIMEOUT
    while time.monotonic() < deadline:
        s = read_state()
        if s and s["current"] != before:
            return True
        time.sleep(0.03)
    return False


def go_to(target):
    """Walks to desktop `target` (0-based), checking each step; gives up after two misses."""
    misses = 0
    for _ in range(64):
        s = read_state()
        if s is None or not 0 <= target < s["count"] or s["current"] == target:
            break
        if step("right" if target > s["current"] else "left", s["current"]):
            misses = 0
            refresh()
        else:
            misses += 1
            if misses >= 2:
                break


switching = False
pending = None


def request_switch(target):
    """Asks for a switch on a worker thread; if one is running, the latest request wins."""
    global pending, switching
    pending = target
    if switching:
        return
    switching = True
    threading.Thread(target=run_switch, daemon=True).start()


def run_switch():
    global pending, switching
    try:
        while pending is not None:
            target, pending = pending, None
            go_to(target)
    finally:
        switching = False
        refresh()


def new_desktop():
    press(VK_D)  # Windows makes it and switches to it


# --- Drawing -------------------------------------------------------------------------------------------

app = fenn.Plugin()
state = None
draw_lock = threading.Lock()
last_tile = last_page = None


def visible_window(count, current, capacity):
    """First desktop index of the `capacity` that fit, keeping the current one in view."""
    if count <= capacity:
        return 0
    return max(0, min(current - capacity // 2, count - capacity))


def wide_cells(count):
    """Click areas on the wide tile: [(what, x0, x1)]. what is a desktop index or prev/next."""
    if count <= WIDE_MAX:
        width = min(WIDE_CELL, 104 // count)
        left = (104 - width * count) // 2
        return [(i, left + i * width, left + (i + 1) * width) for i in range(count)]
    return [("prev", 0, 32), ("next", 72, 104)]


def large_cells(count, current):
    """Click areas on the large tile: [(desktop index, x, y, w, h)]."""
    capacity = LARGE_COLS * LARGE_ROWS
    start = visible_window(count, current, capacity)
    shown = range(start, min(count, start + capacity))
    return [(i, 1 + ((i - start) % LARGE_COLS) * 34, ((i - start) // LARGE_COLS) * 28, 34, 28) for i in shown]


def number_cell(shapes, index, current, x, y, w, h, size):
    active = index == current
    if active:
        shapes.append(fenn.rect(x + 2, y + 2, w - 4, h - 4, fill="accent", radius=8))
    shapes.append(fenn.label(x + w / 2, y + h / 2, str(index + 1), size=size, bold=active,
                             color="on_accent" if active else "dim", anchor="center"))


def tile_view(s, size):
    if s is None:
        return fenn.column([fenn.text("Windows only", size="small", color="dim")],
                           align="center", justify="center")
    count, current, names = s["count"], s["current"], s["names"]
    tip = f"Desktop {current + 1} of {count} \u00b7 {names[current]}"
    if size == "small":  # no room for buttons: the number, and a click opens the page
        return fenn.column([fenn.text(str(current + 1), size="big", bold=True),
                            fenn.text(f"of {count}", size="small", color="dim")],
                           gap=0, align="center", justify="center", tooltip=tip)
    shapes = []
    if size == "large":
        for i, x, y, w, h in large_cells(count, current):
            number_cell(shapes, i, current, x, y, w, h, 13)
        shapes.append(fenn.label(52, 95, names[current], size=11, color="dim", anchor="center"))
        return fenn.canvas(104, shapes, width=104, id="desks", tooltip=tip)
    if count <= WIDE_MAX:
        for i, x0, x1 in wide_cells(count):
            number_cell(shapes, i, current, x0, 0, x1 - x0, 36, 14)
    else:  # too many for one cell each: < 3/9 >
        shapes += [fenn.glyph(16, 18, "caret-left", size=16, color="text"),
                   fenn.label(52, 18, f"{current + 1}/{count}", size=14, bold=True, anchor="center"),
                   fenn.glyph(88, 18, "caret-right", size=16, color="text")]
    return fenn.canvas(36, shapes, width=104, id="desks", tooltip=tip)


def page_view(s):
    if s is None:
        return fenn.text("The desktop switcher only works on Windows 10 and 11.", color="dim", wrap=True)
    count, current, names = s["count"], s["current"], s["names"]
    rows, shown = [], list(range(min(count, MAX_BUTTONS)))
    for start in range(0, len(shown), 4):
        rows.append(fenn.row([fenn.button(f"desk:{i}", label=f"{i + 1}  {names[i]}"[:22], width=0,
                                          primary=(i == current)) for i in shown[start:start + 4]], gap=8))
    return fenn.column([
        fenn.text(f"Desktop {current + 1} of {count} \u00b7 {names[current]}", size="small", color="dim"),
        *rows,
        fenn.row([fenn.button("prev", label="Previous", icon="caret-left"),
                  fenn.button("next", label="Next", icon="caret-right"),
                  fenn.button("new", label="New desktop", icon="plus")], gap=8),
    ], gap=10)


def draw():
    global last_tile, last_page
    with draw_lock:
        s, size = state, app.tile_size or "wide"
        tile = tile_view(s, size)
        if tile != last_tile:
            last_tile = tile
            app.tile(tile)
        page = page_view(s)
        if page != last_page:
            last_page = page
            app.page(page)


def refresh():
    """Reads the desktops and redraws if anything changed."""
    global state
    try:
        new = read_state()
    except Exception as e:
        print(f"could not read the desktops: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return
    if new != state:
        state = new
    draw()


# --- Events ---------------------------------------------------------------------------------------------

@app.on("desks")
def on_tile_click(event):
    point = event.get("value") or {}
    x, y = point.get("x", -1), point.get("y", -1)
    if state is None:
        return
    size = app.tile_size or "wide"
    if size == "large":
        for i, cx, cy, w, h in large_cells(state["count"], state["current"]):
            if cx <= x < cx + w and cy <= y < cy + h:
                request_switch(i)
                return
        return
    for what, x0, x1 in wide_cells(state["count"]):
        if x0 <= x < x1:
            if what == "prev":
                request_switch(max(0, state["current"] - 1))
            elif what == "next":
                request_switch(min(state["count"] - 1, state["current"] + 1))
            else:
                request_switch(what)
            return


for _i in range(MAX_BUTTONS):
    app.on(f"desk:{_i}")(lambda event, i=_i: request_switch(i))


@app.on("prev")
def on_prev(event):
    if state:
        request_switch(max(0, state["current"] - 1))


@app.on("next")
def on_next(event):
    if state:
        request_switch(min(state["count"] - 1, state["current"] + 1))


@app.on("new")
def on_new(event):
    new_desktop()


@app.on_message("layout")
def on_layout(message):
    draw()


if __name__ == "__main__":
    refresh()
    app.every(0.5, refresh)  # also notices desktops changed with the keyboard or Task View
    app.run()
