"""Digital clock for Fenn: your local time with seconds, plus world clocks.

Click the tile to open its page: choose 12 or 24-hour time and add up to six world clocks
(a city name, or a time zone such as Asia/Tokyo). On Windows, time zones need one small
package, once:  pip install tzdata
"""

import time
import unicodedata
from datetime import datetime, timezone

import fenn

try:
    import zoneinfo
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python older than 3.9
    zoneinfo = ZoneInfo = None

    class ZoneInfoNotFoundError(KeyError):
        pass

MAX_WORLD = 6
ROTATE_SECONDS = 5   # the wide tile has room for one world clock at a time; it takes turns
NOTE_SECONDS = 8
PIP_CMD = "pip install tzdata"
TZDATA_HELP = "Time zone data is missing. Run: pip install tzdata (then restart the plugin)."
CITIES = {
    "New York": "America/New_York", "Los Angeles": "America/Los_Angeles", "Chicago": "America/Chicago",
    "Denver": "America/Denver", "Toronto": "America/Toronto", "Vancouver": "America/Vancouver",
    "Mexico City": "America/Mexico_City", "S\u00e3o Paulo": "America/Sao_Paulo",
    "Buenos Aires": "America/Argentina/Buenos_Aires", "Anchorage": "America/Anchorage",
    "Honolulu": "Pacific/Honolulu", "London": "Europe/London", "Dublin": "Europe/Dublin",
    "Paris": "Europe/Paris", "Berlin": "Europe/Berlin", "Madrid": "Europe/Madrid", "Rome": "Europe/Rome",
    "Amsterdam": "Europe/Amsterdam", "Stockholm": "Europe/Stockholm", "Athens": "Europe/Athens",
    "Istanbul": "Europe/Istanbul", "Moscow": "Europe/Moscow", "Cairo": "Africa/Cairo",
    "Lagos": "Africa/Lagos", "Nairobi": "Africa/Nairobi", "Johannesburg": "Africa/Johannesburg",
    "Dubai": "Asia/Dubai", "Karachi": "Asia/Karachi", "Mumbai": "Asia/Kolkata", "Delhi": "Asia/Kolkata",
    "Dhaka": "Asia/Dhaka", "Bangkok": "Asia/Bangkok", "Jakarta": "Asia/Jakarta", "Singapore": "Asia/Singapore",
    "Hong Kong": "Asia/Hong_Kong", "Shanghai": "Asia/Shanghai", "Manila": "Asia/Manila",
    "Seoul": "Asia/Seoul", "Tokyo": "Asia/Tokyo", "Perth": "Australia/Perth",
    "Adelaide": "Australia/Adelaide", "Brisbane": "Australia/Brisbane", "Sydney": "Australia/Sydney",
    "Melbourne": "Australia/Melbourne", "Auckland": "Pacific/Auckland", "Chennai": "Asia/Kolkata", "UTC": "UTC",
}
POPULAR = ["New York", "London", "Tokyo", "Los Angeles", "Singapore", "Dubai", "Chennai"]


# --- Time zones ----------------------------------------------------------------------------------

_zones = {}
_tz_missing = None
_keys = None


def zone_for(key):
    """A tzinfo for an IANA key like 'Asia/Tokyo'. Raises LookupError if it isn't available."""
    if key.upper() in ("UTC", "GMT"):
        return timezone.utc
    if key not in _zones:
        if ZoneInfo is None:
            raise LookupError("time zones need Python 3.9 or newer")
        try:
            _zones[key] = ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise LookupError(key) from None
    return _zones[key]


def tz_missing():
    """True when this PC has no time zone database (Windows without the tzdata package)."""
    global _tz_missing
    if _tz_missing is None:
        try:
            zone_for("Europe/London")
            _tz_missing = False
        except LookupError:
            _tz_missing = True
    return _tz_missing


def canonical_key(typed):
    """The properly capitalised IANA key for what was typed, or None."""
    global _keys
    if zoneinfo is None:
        return None
    if _keys is None:
        try:
            _keys = {k.lower(): k for k in zoneinfo.available_timezones()}
        except Exception:
            _keys = {}
    return _keys.get(typed.strip().lower())


def fold(text):
    """Lower case without accents, so 'sao paulo' finds 'S\u00e3o Paulo'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def resolve(text):
    """(name to show, IANA key) for a city from the list or a zone like Asia/Tokyo."""
    typed = " ".join(text.split())
    if not typed:
        raise ValueError("Type a city or a time zone, like Tokyo or Asia/Tokyo.")
    wanted = fold(typed)
    for match in (lambda n: fold(n) == wanted, lambda n: fold(n).startswith(wanted)):
        for name, key in CITIES.items():
            if match(name):
                return name, key
    if "/" in typed or wanted in ("utc", "gmt"):
        key = "UTC" if wanted in ("utc", "gmt") else canonical_key(typed)
        if key is None:
            raise ValueError(TZDATA_HELP if tz_missing() else f"I don't know the time zone {typed}.")
        return ("UTC" if key == "UTC" else key.split("/")[-1].replace("_", " ")), key
    for name, key in CITIES.items():
        if wanted in fold(name):
            return name, key
    raise ValueError(f"I don't know \"{typed}\". Try a big city, or a zone like Asia/Tokyo.")


# --- Formatting ------------------------------------------------------------------------------------

def fmt_clock(dt, use_24h, seconds=False):
    """('HH:MM[:SS]', 'AM'/'PM' or '') with a two-digit hour."""
    hour = dt.hour if use_24h else (dt.hour % 12 or 12)
    text = f"{hour:02d}:{dt.minute:02d}" + (f":{dt.second:02d}" if seconds else "")
    return text, ("" if use_24h else ("AM" if dt.hour < 12 else "PM"))


def fmt_short(dt, use_24h):
    """'14:05', or '2:05p' in 12-hour mode: compact enough for a line on the tile."""
    if use_24h:
        return f"{dt.hour:02d}:{dt.minute:02d}"
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'AM' if dt.hour < 12 else 'PM'}"


def fmt_offset(delta):
    minutes = int(delta.total_seconds() // 60)
    sign, minutes = ("+" if minutes >= 0 else "-"), abs(minutes)
    hours, rest = divmod(minutes, 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def day_marker(dt, local):
    diff = (dt.date() - local.date()).days
    return "" if diff == 0 else f" {diff:+d}"


def day_label(dt, local):
    diff = (dt.date() - local.date()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(diff, f"{diff:+d} days")


def short(name, n=9):
    return name if len(name) <= n else name[:n - 1] + "\u2026"


# --- State ---------------------------------------------------------------------------------------------

app = fenn.Plugin()
use_24h = bool(app.load("use_24h", True))  # saved in FENN_DATA_DIR, survives updates


def clean_world(raw):
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("zone"), str):
            out.append({"name": item["name"], "zone": item["zone"]})
    return out[:MAX_WORLD]


world = clean_world(app.load("world", []))
city_n = 0          # the add field gets a new id after each add, which empties it
draft = ""          # what is typed in the add field
note = ""
note_until = 0.0
last_tile = last_page = None


def now():
    return datetime.now(timezone.utc)


def set_note(text):
    global note, note_until
    note, note_until = text, time.monotonic() + NOTE_SECONDS


def world_times(moment):
    """[(name, local datetime)] for the clocks whose time zone is available."""
    out = []
    for clock in world:
        try:
            out.append((clock["name"], moment.astimezone(zone_for(clock["zone"]))))
        except LookupError:
            pass
    return out


# --- Drawing ---------------------------------------------------------------------------------------------

def tile_view(size, moment):
    local = moment.astimezone()
    text, ampm = fmt_clock(local, use_24h, seconds=True)
    worlds = world_times(moment)
    
    if size == "small":  # 44x44: hh:mm over :ss (plus a/p in 12-hour mode)
        return fenn.column([
            fenn.text(text[:0], mono=True),
            fenn.text(f":{text[1:]}{ampm[:1].lower()}", size="small", mono=True),
        ], gap=0, align="center", justify="center")
        
    tip = "\n".join(f"{n}: {fmt_clock(dt, use_24h)}{day_marker(dt, local)} ({fmt_offset(dt.utcoffset())})"
                    for n, dt in worlds) or None
                    
    # with a world clock beneath, the main line is a size smaller so both fit the wide tile
    main_size = "title" if size == "wide" or not worlds else "big"
    
    # LOCAL TIME ROW
    line = [fenn.text(text, size=main_size, bold=bool(worlds), mono=True)]
    if ampm:
        line.append(fenn.text(ampm, size=main_size, mono=True))
    
    children = [fenn.row(line, gap=5, align="center")]
    
    if size == "large":
        children.append(fenn.text(local.strftime("%a %d %b"), size=main_size, mono=True))
        for name, dt in worlds[:3]:
            children.append(fenn.row([
                fenn.text(short(name, 5), size="small", color="dim", mono=True ), fenn.spacer(),
                fenn.text(fmt_short(dt, use_24h) + day_marker(dt, local), size="small", mono=True)],
                gap=6, align="center"))
                
    elif worlds:  # wide: one world clock at a time, taking turns
        name, dt = worlds[int(moment.timestamp() // ROTATE_SECONDS) % len(worlds)]
        
        # WORLD TIME ROW (Wrapped in a row to remove default text padding)
        children.append(fenn.row([
            fenn.text(name, size="small", mono=True),
            fenn.text(fmt_short(dt, use_24h), size="small", mono=True)
        ], gap=2, align="center"))
        
    # Reverted to gap=0 for tight stacking without layout errors
    return fenn.column(children, gap=-5 if size != "large" else 2, align="center", justify="center", tooltip=tip)


def page_view(moment):
    local = moment.astimezone()

    header = fenn.row([
        fenn.text(f"Local \u00b7 {local.strftime('%a %d %b')} \u00b7 {fmt_offset(local.utcoffset())}",
                  size="small", color="dim"),
        fenn.spacer(),
        fenn.text("Time format", size="small", color="dim"),
        fenn.chips("format", ["12-hour", "24-hour"], selected=1 if use_24h else 0),
    ], gap=8, align="center")

    body = [header]

    clocks = []
    for i, clock in enumerate(world):
        try:
            dt = moment.astimezone(zone_for(clock["zone"]))
            clock_text, ampm = fmt_clock(dt, use_24h)
            detail = f"{day_label(dt, local)} \u00b7 {fmt_offset(dt.utcoffset())}"
        except LookupError:
            clock_text, ampm, detail = "--:--", "", "time zone data missing"
        clocks.append(fenn.row([
            fenn.column([fenn.text(clock["name"], bold=True), fenn.text(detail, size="small", color="dim")], gap=0),
            fenn.spacer(),
            fenn.text(clock_text + (f" {ampm}" if ampm else ""), size="title", mono=True),
            fenn.button(f"rm:{i}", icon="x", tooltip="Remove this clock")], gap=8, align="center"))

    if clocks:
        body.append(fenn.card(clocks, gap=6, padding=10))
    if not world:
        body.append(fenn.text("No world clocks yet. Add a city below.", size="small", color="faint"))

    if len(world) < MAX_WORLD:
        add_rows = [
            fenn.row([
                # Added width=180 to constrain the input
                fenn.text_input(f"city:{city_n}", "", hint="Add a city or zone, e.g. Tokyo or Asia/Tokyo", live=True, width=400),
                fenn.button("add_city", label="Add", icon="plus", primary=True)
            ], gap=8, align="center")
        ]
        
        free = [c for c in POPULAR if c not in {w["name"] for w in world}][:8]
        if free:
            # Split into chunks of 2 for a neat 2-column grid
            for i in range(0, len(free), 2):
                chunk = free[i:i+2]
                add_rows.append(fenn.row([
                    fenn.button(f"quick:{c}", label=c, width=100) for c in chunk
                ], gap=6))
            
        body.append(fenn.card(add_rows, gap=10, padding=10))
    else:
        body.append(fenn.text(f"{MAX_WORLD} world clocks is the most. Remove one to add another.",
                               size="small", color="faint"))

    if tz_missing():
        body.append(fenn.card([fenn.row([
            fenn.text("World clocks need time zone data on Windows.", size="small", color="orange", wrap=True),
            fenn.button("copy_tz", label=PIP_CMD)], gap=8, align="center")], gap=6, padding=10))
    if note and time.monotonic() < note_until:
        body.append(fenn.text(note, size="small", color="dim", wrap=True))

    return fenn.column(body, gap=8, scroll=True)


def draw():
    """Sends the tile and page, each only when what it shows has changed."""
    global last_tile, last_page
    moment = now()
    tile = tile_view(app.tile_size or "wide", moment)  # the first draw happens before the layout message
    if tile != last_tile:
        last_tile = tile
        app.tile(tile)
    page = page_view(moment)
    if page != last_page:
        last_page = page
        app.page(page)


# --- Events ------------------------------------------------------------------------------------------------

def add_clock(typed):
    """Adds a world clock from text; says why in the page when it can't."""
    global world, draft, city_n
    try:
        name, key = resolve(typed)
        zone_for(key)
    except ValueError as e:
        set_note(str(e))
        return
    except LookupError:
        set_note(TZDATA_HELP if tz_missing() else f"I don't know the time zone {key}.")
        return
    if any(c["zone"] == key for c in world):
        set_note(f"{name} is already on the list.")
    elif len(world) >= MAX_WORLD:
        set_note(f"{MAX_WORLD} world clocks is the most. Remove one to add another.")
    else:
        world = world + [{"name": name, "zone": key}]
        app.save("world", world)
        draft, city_n = "", city_n + 1  # a new id empties the field
        set_note("")


@app.on("format")
def on_format(event):
    global use_24h
    use_24h = (event.get("value") or {}).get("index", 1) == 1
    app.save("use_24h", use_24h)
    draw()


@app.on("add_city")
def on_add(event):
    add_clock(draft)
    draw()


for _city in POPULAR:
    app.on(f"quick:{_city}")(lambda event, c=_city: (add_clock(c), draw()))


@app.on_message("event")
def on_any_event(message):
    """The add field has a new id after every add, so it is matched by prefix."""
    global draft, world
    event_id = str(message.get("id") or "")
    if event_id.startswith("city:"):
        draft = str(message.get("value") or "")
        if message.get("kind") == "submit":
            add_clock(draft)
            draw()
    elif event_id.startswith("rm:"):
        try:
            index = int(event_id[3:])
        except ValueError:
            return
        if 0 <= index < len(world):
            world = world[:index] + world[index + 1:]
            app.save("world", world)
            draw()


@app.on("copy_tz")
def on_copy_tz(event):
    app.copy(PIP_CMD)


@app.on_message("layout")
def on_layout(message):
    draw()  # the user resized the tile


if __name__ == "__main__":
    app.every(0.25, draw)  # fine enough that the seconds never skip; sends only when something changed
    draw()
    app.run()
