import time

import fenn

app = fenn.Plugin()
use_24h = bool(app.load("use_24h", True))  # saved in FENN_DATA_DIR, survives updates
last = None


def draw():
    """Build the whole tile from the time, format and tile size; send only on change."""
    global last
    size = app.tile_size or "wide"  # the first draw happens before Fenn's layout message
    now = time.localtime()
    hour = now.tm_hour if use_24h else (now.tm_hour % 12 or 12)
    ampm = "" if use_24h else ("AM" if now.tm_hour < 12 else "PM")
    hh, mm, ss = f"{hour:02d}", f"{now.tm_min:02d}", f"{now.tm_sec:02d}"
    date = time.strftime("%a %d %b", now)

    key = (size, use_24h, hh, mm, ss, date if size == "large" else "")
    if key == last:
        return  # nothing changed, so stay silent
    last = key

    if size == "small":  # 44x44: hh:mm over :ss (plus a/p in 12-hour mode)
        children = [
            fenn.text(f"{hh}:{mm}", mono=True),
            fenn.text(f":{ss}{ampm[:1].lower()}", size="small", color="dim", mono=True),
        ]
    else:  # wide 104x36, large 104x104
        line = [fenn.text(f"{hh}:{mm}:{ss}", size="title", mono=True)]
        if ampm:
            line.append(fenn.text(ampm, size="small", color="dim"))
        children = [fenn.row(line, gap=3, align="center")]
        if size == "large":
            children.append(fenn.text(date, size="small", color="dim"))

    app.tile(fenn.column(children, gap=0, align="center", justify="center"))


def draw_page():
    # The page opens when the tile is clicked. Echo back the format we actually hold.
    app.page(fenn.column([
        fenn.text("Time format", size="small", color="dim"),
        fenn.chips("format", ["12-hour", "24-hour"], selected=1 if use_24h else 0),
    ], gap=8))


@app.on("format")
def on_format(event):
    global use_24h
    value = event.get("value") or {}
    use_24h = value.get("index", 1) == 1
    app.save("use_24h", use_24h)
    draw_page()
    draw()


@app.on_message("layout")
def on_layout(msg):
    draw()  # the user resized the tile


# Check a few times a second so the seconds never skip; it only sends when something changed.
app.every(0.25, draw)
draw_page()
draw()
app.run()
