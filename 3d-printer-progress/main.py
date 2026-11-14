"""3D print progress for Fenn (Flashforge Adventurer 5M / 5M Pro).

Talks only to your printer, on your own network. Two ways in, both standard library only:

* IP address only (default): the printer's text interface on port 8899. Each check opens a
  short session (~M601 S1 ... ~M602), reads status, layer and temperatures, and closes it.
  Time left is worked out from how fast the print is moving.
* IP + serial number + check code (optional): the printer's HTTP status endpoint on port
  8898, which also reports the exact layer count and the slicer's time estimate.

Also on the page: Pause / Resume for the print (sent over the same route, and only when the
printer's own state says it makes sense), and a Camera tab that shows the printer's MJPEG
camera (the Adventurer 5M Pro has one) or any other camera address you give it.

Enter the details on the plugin's page (Settings tab); they are kept in FENN_DATA_DIR.
"""

import base64
import http.client
import json
import re
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta

import fenn

POLL_ACTIVE, POLL_IDLE, POLL_OFFLINE = 5, 20, 15  # seconds between status requests
POLL_ACTIVE_TCP = 10  # the IP-only route holds the printer's control session briefly
OPEN_PANEL_WHEN_DONE = False  # True: open the notch by itself when a print finishes
FIELDS = ("ip", "serial", "code", "camera")
HOST = re.compile(r"^[A-Za-z0-9._-]+$")
CAMERA_URL = re.compile(r"^https?://\S+$")
TAB_STATUS, TAB_CAMERA, TAB_SETTINGS = 0, 1, 2
CAMERA_DELAY = 0.5  # seconds between pictures while the Camera tab is open

STATES = {  # the printer's `status` -> our state
    "printing": "printing", "working": "printing", "heating": "heating", "pausing": "paused",
    "paused": "paused", "completed": "done", "ready": "idle", "busy": "busy",
    "calibrate_doing": "busy", "canceling": "busy", "cancel": "cancelled", "error": "error",
}
LABELS = {
    "printing": "Printing", "heating": "Heating", "paused": "Paused", "done": "Done",
    "idle": "Idle", "busy": "Working", "cancelled": "Cancelled", "error": "Error",
    "offline": "Offline", "auth": "Check code", "setup": "Set up", "connecting": "Connecting\u2026",
    "inuse": "In use",
}
ICONS = {  # state -> (icon, colour)
    "printing": ("cube", "accent"), "heating": ("thermometer-simple", "orange"),
    "paused": ("pause", "orange"), "done": ("check-circle", "green"), "idle": ("cube", "dim"),
    "busy": ("clock", "dim"), "cancelled": ("cube", "dim"), "error": ("warning", "red"),
    "offline": ("wifi-slash", "dim"), "auth": ("warning", "orange"), "setup": ("cube", "dim"),
    "connecting": ("clock", "dim"), "inuse": ("lock", "orange"),
}
TCP_STATES = {  # MachineStatus from ~M119 -> our state
    "ready": "idle", "building_from_sd": "printing", "paused": "paused",
    "building_completed": "done", "busy": "busy", "heating": "heating",
}
PAGE_ACTIONS = [{"id": "refresh", "icon": "arrow-clockwise", "tooltip": "Check the printer now"}]


# --- Plain helpers (no network) ------------------------------------------------------

def clamp(value, low, high):
    return max(low, min(high, value))


def fmt_duration(seconds):
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def remaining_seconds(estimated, elapsed, progress):
    """Seconds left. Sources differ on whether `estimatedTime` is the total estimate or the
    time left, so both readings are checked against the progress made so far."""
    by_progress = elapsed * (1 - progress) / progress if progress >= 0.05 and elapsed > 0 else None
    if estimated <= 0:
        return by_progress
    as_total = max(0.0, estimated - elapsed)
    if by_progress is None:
        return as_total
    return min((as_total, estimated), key=lambda candidate: abs(candidate - by_progress))


def parse_detail(detail):
    raw = str(detail.get("status") or "").lower()
    st = STATES.get(raw, "busy")
    progress = clamp(float(detail.get("printProgress") or 0), 0.0, 1.0)
    if st == "done":
        progress = 1.0
    left = None
    if st in ("printing", "paused"):
        left = remaining_seconds(float(detail.get("estimatedTime") or 0),
                                 float(detail.get("printDuration") or 0), progress)
    eta = (datetime.now() + timedelta(seconds=left)).strftime("%H:%M") if left is not None else None
    return {
        "state": st, "raw": raw, "progress": progress, "left": left, "eta": eta, "source": "http",
        "camera_url": str(detail.get("cameraStreamUrl") or ""),
        "file": str(detail.get("printFileName") or ""),
        "layer": int(detail.get("printLayer") or 0), "layers": int(detail.get("targetPrintLayer") or 0),
        "nozzle": float(detail.get("rightTemp", detail.get("leftTemp")) or 0),
        "nozzle_target": float(detail.get("rightTargetTemp", detail.get("leftTargetTemp")) or 0),
        "bed": float(detail.get("platTemp") or 0), "bed_target": float(detail.get("platTargetTemp") or 0),
    }


class RateEstimator:
    """Time left from how fast the progress moves, for when the printer doesn't say."""

    def __init__(self):
        self.start = None
        self.key = None

    def reset(self):
        self.start = None

    def update(self, key, progress, now):
        if self.start is None or key != self.key or progress < self.start[1]:
            self.start, self.key = (now, progress), key  # (re)start watching this print
            return None
        t0, p0 = self.start
        if progress - p0 >= 0.02 and now - t0 >= 60:
            return (1 - progress) / ((progress - p0) / (now - t0))
        return None


def temps_from(text):
    """(nozzle, nozzle target, bed, bed target) from a ~M105 reply, old or new format."""
    number = r"(\d+(?:\.\d+)?)"
    nozzle = re.search(rf"T0:\s*{number}\s*/\s*{number}", text)
    bed = re.search(rf"B:\s*{number}\s*/\s*{number}", text)
    pair = lambda m: (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
    return (*pair(nozzle), *pair(bed))


def parse_tcp(status_text, progress_text, temp_text):
    """Builds the same dict as parse_detail from the replies to ~M119, ~M27 and ~M105."""
    machine = re.search(r"MachineStatus:\s*(\w+)", status_text)
    raw = machine.group(1).lower() if machine else ""
    st = TCP_STATES.get(raw, "busy")
    current = re.search(r"CurrentFile:[ \t]*(.*)", status_text)
    layer = re.search(r"Layer:\s*(\d+)\s*/\s*(\d+)", progress_text)
    sd = re.search(r"SD printing byte\s*(\d+)\s*/\s*(\d+)", progress_text)
    layers = int(layer.group(2)) if layer else 0
    done_layers = int(layer.group(1)) if layer else 0
    if layers:  # on the 5M the layer count is the reliable one; the byte count can run ahead
        progress = done_layers / layers
    elif sd and int(sd.group(2)):
        progress = int(sd.group(1)) / int(sd.group(2))
    else:
        progress = 0.0
    nozzle, nozzle_target, bed, bed_target = temps_from(temp_text)
    if st == "printing" and done_layers == 0 and nozzle_target - nozzle > 8:
        st = "heating"  # still warming up, nothing printed yet
    progress = 1.0 if st == "done" else (0.0 if st in ("idle", "heating") else clamp(progress, 0.0, 1.0))
    return {
        "state": st, "raw": raw, "progress": progress, "left": None, "eta": None, "source": "tcp",
        "file": (current.group(1).strip() if current else ""), "layer": done_layers, "layers": layers,
        "nozzle": nozzle, "nozzle_target": nozzle_target, "bed": bed, "bed_target": bed_target,
    }


# --- State shared between the poller thread and Fenn's event loop ---------------------

app = fenn.Plugin()
settings = {k: str(v) for k, v in (app.load("settings", {}) or {}).items()}
draft = {}          # what is typed in the Settings fields, until Save
form_error = ""
tab = TAB_STATUS
wake = threading.Event()
draw_lock = threading.Lock()
last_tile = last_page = last_badge = last_active = None
estimator = RateEstimator()
busy_streak = 0
camera = {"frame": None, "size": None, "error": ""}  # replaced wholesale by the camera thread
camera_on = False   # True while a camera thread should keep running
camera_gen = 0      # lets an old camera thread notice it has been replaced
page_visible = False
control_note = ""   # "Pausing...", or why a Pause/Resume didn't happen
job_busy = False    # one command to the printer at a time
_last_warning = None


def configured():
    return bool(settings.get("ip"))


def use_http():
    """True when a serial number and check code were entered; otherwise the IP-only route."""
    return bool(settings.get("serial") and settings.get("code"))


state = {"state": "connecting" if configured() else "setup"}
if not configured():
    tab = TAB_SETTINGS


def warn_once(message):
    global _last_warning
    if message != _last_warning:
        _last_warning = message
        print(message, file=sys.stderr, flush=True)


def http_post(path, **extra):
    body = {"serialNumber": settings["serial"], "checkCode": settings["code"], **extra}
    request = urllib.request.Request(f"http://{settings['ip']}:8898/{path}",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_detail():
    return http_post("detail")


class InUse(Exception):
    """Another program (FlashPrint, Orca...) holds the printer's control session."""


def tcp_command(sock, command):
    sock.sendall((command + "\r\n").encode("ascii"))
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        text = data.decode("utf-8", "replace").rstrip()
        if text.endswith("ok") or "Control Failed" in text:
            break
    return data.decode("utf-8", "replace")


def fetch_tcp():
    with socket.create_connection((settings["ip"], 8899), timeout=4) as sock:
        sock.settimeout(4)
        if "Control Success" not in tcp_command(sock, "~M601 S1"):
            raise InUse()
        try:
            status = tcp_command(sock, "~M119")
            progress = tcp_command(sock, "~M27")
            temps = tcp_command(sock, "~M105")
        finally:
            try:
                tcp_command(sock, "~M602")  # always give the session back
            except OSError:
                pass
    return parse_tcp(status, progress, temps)


def with_eta(new):
    """IP-only route: fill in time left from the rate of progress."""
    if new.get("source") != "tcp":
        return new
    if new["state"] == "printing":
        new["left"] = estimator.update(new["file"], new["progress"], time.time())
        if new["left"] is not None:
            new["eta"] = (datetime.now() + timedelta(seconds=new["left"])).strftime("%H:%M")
    else:
        estimator.reset()
    return new


class NotApplicable(Exception):
    """Pause/Resume asked for, but the printer's own state says it doesn't apply."""


class Refused(Exception):
    """The printer answered, but said no."""


def send_job(action):
    """action is 'pause' or 'continue'. The printer's state is checked first, so a stale
    click can never start a print or pause one that has already finished."""
    problem = "Nothing is printing." if action == "pause" else "The printer isn't paused."
    if use_http():
        detail = http_post("detail").get("detail") or {}
        status = str(detail.get("status", "")).lower()
        if status not in (("printing", "working") if action == "pause" else ("paused",)):
            raise NotApplicable(problem)
        reply = http_post("control", payload={"cmd": "jobCtl_cmd",
                                               "args": {"jobID": "", "action": action}})
        if reply.get("code") != 0:
            raise Refused(reply.get("message", reply))
        return
    with socket.create_connection((settings["ip"], 8899), timeout=4) as sock:
        sock.settimeout(4)
        if "Control Success" not in tcp_command(sock, "~M601 S1"):
            raise InUse()
        try:
            machine = re.search(r"MachineStatus:\s*(\w+)", tcp_command(sock, "~M119"))
            wanted = "BUILDING_FROM_SD" if action == "pause" else "PAUSED"
            if not machine or machine.group(1).upper() != wanted:
                raise NotApplicable(problem)
            tcp_command(sock, "~M25" if action == "pause" else "~M24")
        finally:
            try:
                tcp_command(sock, "~M602")
            except OSError:
                pass


def run_job(action):
    """Runs on its own thread so the network wait never freezes the notch."""
    global control_note, job_busy
    if job_busy:
        return
    job_busy = True
    control_note = "Pausing\u2026" if action == "pause" else "Resuming\u2026"
    draw()
    try:
        send_job(action)
        control_note = ""
        time.sleep(1.5)  # give the printer a moment before asking how it is
    except NotApplicable as e:
        control_note = str(e)
    except InUse:
        control_note = "Another program is connected to the printer. Try again in a moment."
    except Refused as e:
        control_note = f"The printer said: {e}"
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
        warn_once(f"printer command failed: {type(e).__name__}: {e}")
        control_note = "Couldn't reach the printer."
    job_busy = False
    wake.set()
    draw()


def jpeg_size(data):
    """(width, height) from a JPEG's header, or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
        elif data[i + 1] in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        elif data[i + 1] == 0xFF:
            i += 1
        else:
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def fit(size, max_w, max_h):
    width, height = size
    scale = min(max_w / width, max_h / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


def grab_frame(url, limit=2_000_000):
    """The first complete JPEG from a camera address (an MJPEG stream or a single picture)."""
    request = urllib.request.Request(url, headers={"User-Agent": "Fenn 3D Print plugin"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = b""
        while len(data) < limit:
            chunk = response.read1(8192)
            if not chunk:
                break
            data += chunk
            start = data.find(b"\xff\xd8")
            if start >= 0:
                end = data.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    return data[start:end + 2]
    raise ValueError("no picture in the camera's reply")


def camera_url():
    """Your own address if you set one, else what the printer reports, else the usual one."""
    return (settings.get("camera") or state.get("camera_url")
            or f"http://{settings['ip']}:8080/?action=stream")


def camera_loop(gen):
    global camera
    url = camera_url()
    if use_http() and not settings.get("camera"):
        try:  # the 5M Pro's camera can be switched off; ask it to turn on
            http_post("control", payload={"cmd": "streamCtrl_cmd", "args": {"action": "open"}})
        except (OSError, ValueError, http.client.HTTPException):
            pass
    while camera_on and camera_gen == gen:
        try:
            frame = grab_frame(url)
            camera = {"frame": frame, "size": jpeg_size(frame), "error": ""}
            pause = CAMERA_DELAY
        except (OSError, ValueError, http.client.HTTPException) as e:
            warn_once(f"camera: {type(e).__name__}: {e}")
            camera = {"frame": None, "size": None, "error": url}
            pause = 3  # try again, but gently
        if camera_on and camera_gen == gen:
            draw()
        time.sleep(pause)


def sync_camera():
    """The camera runs only while the page is open on the Camera tab."""
    global camera_on, camera_gen, camera
    want = page_visible and tab == TAB_CAMERA and configured()
    if want and not camera_on:
        camera_on, camera_gen = True, camera_gen + 1
        camera = {"frame": None, "size": None, "error": ""}
        threading.Thread(target=camera_loop, args=(camera_gen,), daemon=True).start()
    elif not want and camera_on:
        camera_on, camera_gen = False, camera_gen + 1


def poll_once():
    """One status check. Updates `state` and the screen; returns seconds to wait."""
    global state, busy_streak, control_note
    wait = 60
    try:
        if not configured():
            new = {"state": "setup"}
        elif use_http():
            reply = fetch_detail()
            if reply.get("code") == 3:
                new = {"state": "auth"}
            elif reply.get("code") not in (0, None) or "detail" not in reply:
                raise ValueError(f"printer said: {reply.get('message', reply)}")
            else:
                new = parse_detail(reply["detail"])
        else:
            new = with_eta(fetch_tcp())
        busy_streak = 0
        if new["state"] in ("printing", "heating", "paused", "busy"):
            wait = POLL_ACTIVE if new.get("source") == "http" else POLL_ACTIVE_TCP
        else:
            wait = POLL_IDLE
    except InUse:
        busy_streak += 1
        has_data = state["state"] in ("printing", "heating", "paused", "done", "idle", "busy",
                                      "cancelled", "error")
        new = state if has_data and busy_streak < 3 else {"state": "inuse"}  # ignore short clashes
        wait = POLL_ACTIVE
    except (OSError, ValueError, KeyError, TypeError) as e:  # no connection, bad reply
        warn_once(f"printer request failed: {type(e).__name__}: {e}")
        new, wait = {"state": "offline"}, POLL_OFFLINE
    finished = state["state"] in ("printing", "heating", "paused") and new["state"] == "done"
    state = new
    if control_note and not control_note.endswith("\u2026"):
        control_note = ""  # an old "couldn't pause" message has had its moment
    draw()
    if finished and OPEN_PANEL_WHEN_DONE:
        app.show()
    return wait


def poll_loop():
    while True:
        wait = poll_once()
        wake.wait(wait)
        wake.clear()


# --- Drawing --------------------------------------------------------------------------

def layer_text(s, short=False):
    if not s.get("layers"):
        return ""
    return f"L{s['layer']}/{s['layers']}" if short else f"Layer {s['layer']}/{s['layers']}"


def describe(s, short):
    """(headline, detail) for a state."""
    st = s["state"]
    if st == "printing":
        left = fmt_duration(s["left"]) if s.get("left") is not None else None
        return (left if short else f"{left} left") if left else "Printing", layer_text(s, short)
    if st == "paused":
        return "Paused", layer_text(s, short)
    if st == "heating":
        return "Heating", f"{s['nozzle']:.0f}\u00b0/{s['nozzle_target']:.0f}\u00b0"
    if st == "done":
        return "Done", s.get("file", "")
    if st == "idle":
        return "Idle", f"nozzle {s['nozzle']:.0f}\u00b0"
    if st == "busy":
        return "Working", s.get("raw", "")
    return {"error": ("Error", "check the printer"), "offline": ("Offline", "can't reach it"),
            "auth": ("Check code", "see Settings"), "setup": ("Set up", "click to begin"),
            "inuse": ("In use", "by another app"), "cancelled": ("Cancelled", "")}.get(st, (LABELS.get(st, ""), ""))


def tile_view(s, size):
    st = s["state"]
    running = st in ("printing", "paused")
    colour = "orange" if st == "paused" else None
    pct = round(s.get("progress", 0) * 100)
    icon_name, icon_colour = ICONS.get(st, ("cube", "dim"))
    tip = " \u00b7 ".join(x for x in (s.get("file"), LABELS.get(st)) if x) or None
    head, sub = describe(s, short=(size != "large"))

    if size == "small":
        lead = (fenn.ring(s["progress"], label=f"{pct}%", size=40, color=colour) if running
                else fenn.icon(icon_name, size=24, color=icon_colour))
        return fenn.column([lead], align="center", justify="center", tooltip=tip)
    if size == "large":
        if running:  # ring and Pause/Resume side by side, details underneath
            button = (fenn.button("pause", icon="pause") if st == "printing"
                      else fenn.button("resume", icon="play", primary=True))
            lead = fenn.row([fenn.ring(s["progress"], label=f"{pct}%", size=44, color=colour),
                             button], gap=8, align="center")
        else:
            lead = fenn.icon(icon_name, size=32, color=icon_colour)
        kids = [lead, fenn.text(head, bold=True, wrap=True)]
        if sub:
            kids.append(fenn.text(sub, size="small", color="dim", wrap=True))
        return fenn.column(kids, gap=3, align="center", justify="center", tooltip=tip)
    lead = (fenn.ring(s["progress"], label=str(pct), size=32, color=colour) if running
            else fenn.icon(icon_name, size=22, color=icon_colour))
    lines = [fenn.text(head, size="small", bold=True)]
    if sub:
        lines.append(fenn.text(sub, size="small", color="dim"))
    return fenn.column([fenn.row([lead, fenn.column(lines, gap=0)], gap=6, align="center")],
                       align="center", justify="center", tooltip=tip)


def controls(s):
    st = s["state"]
    if st == "printing":
        button = fenn.button("pause", label="Pause", icon="pause")
    elif st == "paused":
        button = fenn.button("resume", label="Resume", icon="play", primary=True)
    else:
        return fenn.text(control_note, size="small", color="dim") if control_note else None
    return fenn.row([button, fenn.text(control_note, size="small", color="dim")], gap=8,
                    align="center")


def camera_view(s):
    if not configured():
        return fenn.text("Enter the printer's IP address in Settings first.", color="dim", wrap=True)
    c = camera
    if c["frame"]:
        width, height = fit(c["size"] or (4, 3), 280, 200)
        picture = fenn.image(base64=base64.b64encode(c["frame"]).decode("ascii"), width=width,
                             height=height, rounding=8)
        side = []
    else:
        waiting = not c["error"]
        picture = fenn.column([
            fenn.icon("camera", size=28, color="dim"),
            fenn.text("Connecting to the camera\u2026" if waiting else
                      f"No picture from {c['error']}", size="small", color="dim", wrap=True),
        ], gap=6, align="center", justify="center")
        side = [] if waiting else [fenn.text(
            "The Adventurer 5M Pro has a built-in camera; the standard 5M doesn't. For any other "
            "camera, put its MJPEG or picture address under Settings.",
            size="small", color="faint", wrap=True)]
    head, sub = describe(s, short=False)
    side = [fenn.text(head, size="title"), fenn.text(sub or " ", size="small", color="dim")] + side
    extra = controls(s)
    if extra:
        side.append(extra)
    return fenn.row([picture, fenn.column(side, gap=6)], gap=14, align="center")


def status_view(s):
    st = s["state"]
    if st in ("printing", "paused", "heating", "done"):
        pct = round(s["progress"] * 100)
        lines = [fenn.text(s.get("file") or LABELS[st], size="title", wrap=True),
                 fenn.text(" \u00b7 ".join(x for x in (LABELS[st], layer_text(s)) if x),
                           size="small", color="dim")]
        if s.get("left") is not None:
            lines.append(fenn.text(f"{fmt_duration(s['left'])} left \u00b7 done around {s['eta']}",
                                   size="small"))
        elif st == "printing" and s.get("source") == "tcp":
            lines.append(fenn.text("Time left shows up after a minute or two of watching the print.",
                                   size="small", color="faint"))
        lines.append(fenn.text(f"Nozzle {s['nozzle']:.0f}\u00b0/{s['nozzle_target']:.0f}\u00b0    "
                               f"Bed {s['bed']:.0f}\u00b0/{s['bed_target']:.0f}\u00b0",
                               size="small", mono=True, color="dim"))
        extra = controls(s)
        if extra:
            lines.append(extra)
        return fenn.column([
            fenn.row([fenn.ring(s["progress"], label=f"{pct}%", size=76,
                                color="orange" if st in ("paused", "heating") else None),
                      fenn.column(lines, gap=4)], gap=14, align="center"),
            fenn.progress(s["progress"]),
        ], gap=10)
    messages = {
        "idle": f"The printer is idle. Nozzle {s.get('nozzle', 0):.0f}\u00b0, bed {s.get('bed', 0):.0f}\u00b0.",
        "busy": "The printer is busy (homing or calibrating).",
        "cancelled": "The last print was cancelled.",
        "error": "The printer reports an error. Check its screen.",
        "offline": "Can't reach the printer. Check that it is on, on the same network, and that "
                   "the IP address in Settings is right.",
        "auth": "The printer rejected the serial number or check code. Check them in Settings.",
        "setup": "Enter your printer's details in Settings to start.",
        "connecting": "Connecting\u2026",
        "inuse": "Another program (FlashPrint or Orca, for example) is connected to the printer "
                 "and holding it. It will show here once that program lets go.",
    }
    return fenn.text(messages.get(st, ""), wrap=True, color="dim")


def settings_view():
    hints = {"ip": "Printer IP, e.g. 192.168.1.50", "serial": "Serial number (optional)",
             "code": "Check code (optional)"}
    inputs = [fenn.text_input(k, settings.get(k, ""), hint=hints[k], live=True)
              for k in ("ip", "serial", "code")]
    camera_input = fenn.text_input("camera", settings.get("camera", ""), live=True,
                                   hint="Camera address (optional), e.g. http://192.168.1.50:8080/?action=stream")
    return fenn.column([
        fenn.row(inputs, gap=8),
        camera_input,
        fenn.row([fenn.button("save", label="Save", primary=True),
                  fenn.text(form_error, size="small", color="red")], gap=10, align="center"),
        fenn.text("Only the IP address is needed. The serial number and check code (printer "
                  "settings) give exact layers and the slicer's time estimate. Leave the camera "
                  "empty to use the printer's own. Everything stays on your network.",
                  size="small", color="faint", wrap=True),
    ], gap=10)


def page_view(s):
    body = {TAB_CAMERA: camera_view, TAB_SETTINGS: lambda _: settings_view()}.get(tab, status_view)(s)
    return fenn.column([fenn.chips("tab", ["Status", "Camera", "Settings"], selected=tab), body],
                       gap=12)


def badge_for(s):
    st = s["state"]
    if st == "printing":
        return {"kind": "ring", "progress": round(s["progress"], 2), "label": f"{round(s['progress'] * 100)}%"}
    glyph = {"paused": "pause", "heating": "thermometer-simple", "done": "check-circle",
             "error": "warning", "auth": "warning"}.get(st)
    return {"kind": "glyph", "icon": glyph} if glyph else None


def draw():
    """Sends the tile, page, badge and glow, each only when it has changed."""
    global last_tile, last_page, last_badge, last_active
    with draw_lock:
        s, size = state, app.tile_size or "wide"
        tile = tile_view(s, size)
        if tile != last_tile:
            last_tile = tile
            app.tile(tile)
        page = page_view(s)
        if page != last_page:
            last_page = page
            app.page(page, PAGE_ACTIONS)
        badge = badge_for(s)
        if badge != last_badge:
            last_badge = badge
            if badge is None:
                app.clear_badge()
            elif badge["kind"] == "ring":
                app.badge_ring(badge["progress"], badge["label"])
            else:
                app.badge_glyph(badge["icon"])
        active = s["state"] == "printing"
        if active != last_active:
            last_active = active
            app.active(active)


def save_settings():
    global settings, state, tab, form_error
    merged = {**settings, **draft}
    if not HOST.match(merged.get("ip", "")):
        form_error = "That doesn't look like an IP address or host name."
    elif bool(merged.get("serial")) != bool(merged.get("code")):
        form_error = "Enter both the serial number and check code, or leave both empty."
    elif merged.get("camera") and not CAMERA_URL.match(merged["camera"]):
        form_error = "The camera address should start with http:// or https://"
    else:
        settings = {k: merged.get(k, "") for k in FIELDS}
        app.save("settings", settings)
        draft.clear()
        form_error, tab = "", TAB_STATUS
        state = {"state": "connecting"}
        wake.set()  # check the printer straight away
    sync_camera()
    draw()


def on_field(field, event):
    draft[field] = str(event.get("value") or "").strip()
    if event.get("kind") == "submit":
        save_settings()


for _field in FIELDS:
    app.on(_field)(lambda event, f=_field: on_field(f, event))


@app.on("save")
def on_save(event):
    save_settings()


@app.on("tab")
def on_tab(event):
    global tab
    tab = int((event.get("value") or {}).get("index", 0))
    sync_camera()
    draw()


@app.on("pause")
def on_pause(event):
    threading.Thread(target=run_job, args=("pause",), daemon=True).start()


@app.on("resume")
def on_resume(event):
    threading.Thread(target=run_job, args=("continue",), daemon=True).start()


@app.on_message("page_open")
def on_page_open(message):
    global page_visible
    page_visible = True
    sync_camera()
    draw()


@app.on_message("page_close")
def on_page_close(message):
    global page_visible
    page_visible = False
    sync_camera()


@app.on("refresh")
def on_refresh(event):
    wake.set()


@app.on_message("layout")
def on_layout(message):
    draw()


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()  # network waits stay off Fenn's loop
    app.run()
