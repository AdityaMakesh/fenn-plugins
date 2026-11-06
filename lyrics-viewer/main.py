"""Lyrics for Fenn: shows the lyric line for the song that is playing on this PC.

* What is playing (title, artist, position) comes from Windows' media controls, the same
  source as the volume flyout. Only Spotify is followed (SOURCE_APP below): the desktop app
  and the Microsoft Store version both work. Set SOURCE_APP = "" to follow any media app.
* Lyrics are looked up on lrclib.net, a free lyrics database. The only thing sent is the
  track's title, artist, album and length, once per new song. Results are cached in
  FENN_DATA_DIR, so playing a song again makes no request at all.

Needs (once):
    pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams
"""

import asyncio
import bisect
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import fenn

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlayStatus,
    )
    IMPORT_ERROR = None
except ImportError as first_error:
    try:  # the older package with the same API
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlayStatus,
        )
        IMPORT_ERROR = None
    except ImportError:
        MediaManager = PlayStatus = None
        IMPORT_ERROR = first_error

API = "https://lrclib.net/api"
USER_AGENT = "Fenn Lyrics plugin 1.0"
PIP_CMD = ("pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation "
           "winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams")
OFFSET_STEP = 0.5
SOURCE_APP = "spotify"   # matched against the app's Windows id, e.g. "Spotify.exe"; "" = any app
SOURCE_NAME = "Spotify"  # only used in on-screen messages
NOTE = "\u266a"
PAGE_ACTIONS = [{"id": "refetch", "icon": "arrow-clockwise", "tooltip": "Look up lyrics again"}]
TILE_MESSAGES = {
    "loading": "Finding lyrics\u2026", "none": "No lyrics found", "instrumental": "Instrumental",
    "offline": "Lookup failed", "setup": "Setup needed",
    "nomedia": f"{SOURCE_NAME} isn't playing" if SOURCE_APP else "Nothing playing",
}
PAGE_MESSAGES = {
    "loading": "Finding lyrics\u2026",
    "none": "No lyrics found for this track. The refresh button tries again.",
    "instrumental": "Instrumental, no lyrics.",
    "offline": "Couldn't reach lrclib.net. Check your connection, then press refresh.",
    "nomedia": (f"Open {SOURCE_NAME} and play a song." if SOURCE_APP
                else "Nothing is playing. Start a song in any media app."),
}


# --- Plain helpers (no Windows, no network) -------------------------------------------

_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_WORD_STAMP = re.compile(r"<\d+:\d+(?:\.\d+)?>")


def parse_lrc(text):
    """'[mm:ss.xx] words' lines -> [(seconds, words)] sorted by time."""
    lines = []
    for raw in (text or "").splitlines():
        stamps = _STAMP.findall(raw)
        if not stamps:
            continue  # tags like [ar:...] and blank lines
        words = _WORD_STAMP.sub("", _STAMP.sub("", raw)).strip()
        for minutes, seconds in stamps:
            lines.append((int(minutes) * 60 + float(seconds), words))
    lines.sort(key=lambda item: item[0])
    return lines


def current_index(times, position):
    """Index of the line being sung at `position` seconds, or -1 before the first line."""
    return bisect.bisect_right(times, position) - 1


def clean_artist(name):
    name = (name or "").strip()
    return name[:-len(" - Topic")].strip() if name.endswith(" - Topic") else name


def first_artist(name):
    return re.split(r"\s*(?:,|;|&|/|\bfeat\.?|\bft\.?)\s*", name, maxsplit=1)[0].strip()


def pick_best(results, duration):
    """Best search hit: right length first, synced before plain."""
    usable = [r for r in results
              if r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental")]

    def score(r):
        gap = abs((r.get("duration") or 0) - duration) if duration else 0
        return (0 if gap <= 4 else 1, 0 if r.get("syncedLyrics") else 1, gap)

    return min(usable, key=score) if usable else None


def lyr(status, lines=(), times=()):
    return {"status": status, "lines": list(lines), "times": list(times)}


def to_lyrics(found):
    if not found:
        return lyr("none")
    if found.get("instrumental"):
        return lyr("instrumental")
    synced = parse_lrc(found.get("syncedLyrics"))
    if synced:
        return lyr("synced", synced, [t for t, _ in synced])
    plain = [line.strip() for line in (found.get("plainLyrics") or "").splitlines()]
    return lyr("plain", plain) if any(plain) else lyr("none")


# --- Looking lyrics up -----------------------------------------------------------------

_last_warning = None


def warn_once(message):
    global _last_warning
    if message != _last_warning:
        _last_warning = message
        print(message, file=sys.stderr, flush=True)


def api(path, **params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def search_online(t):
    artist, title = t["artist"], t["title"]
    if t["album"] and t["duration"]:
        exact = api("get", track_name=title, artist_name=artist, album_name=t["album"],
                    duration=int(round(t["duration"])))
        if exact:
            return exact
    best = pick_best(api("search", track_name=title, artist_name=artist) or [], t["duration"])
    if not best and first_artist(artist) not in ("", artist):
        best = pick_best(api("search", track_name=title, artist_name=first_artist(artist)) or [],
                         t["duration"])
    return best


def cache_name(key):
    return "lyrics-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def fetch(token, t, force):
    """Runs on a worker thread; only ever replaces the `lyrics` reference."""
    global lyrics
    try:
        name = cache_name(t["key"])
        if force:
            try:
                os.remove(os.path.join(app.data_dir, name + ".json"))
            except OSError:
                pass
        found = app.load(name)
        if not found:
            found = search_online(t)
            if found:
                app.save(name, found)
        result = to_lyrics(found)
    except (OSError, ValueError) as e:  # no connection, timeout, bad reply
        warn_once(f"lyrics lookup failed: {type(e).__name__}: {e}")
        result = lyr("offline")
    if token == fetch_token:  # ignore the answer if the song has already changed
        lyrics = result


def start_fetch(force=False):
    global lyrics, fetch_token
    fetch_token += 1
    lyrics = lyr("loading")
    threading.Thread(target=fetch, args=(fetch_token, dict(track), force), daemon=True).start()


# --- What is playing -------------------------------------------------------------------

app = fenn.Plugin()
offset = float(app.load("offset", 0.0))   # seconds; + shows each line earlier
track = None                              # {"key", "title", "artist", "album", "duration"}
playing = False
anchor = (0.0, time.monotonic())          # (media position, when we read it)
lyrics = lyr("nomedia")
fetch_token = 0
manager = None
loop = asyncio.new_event_loop() if MediaManager else None
last_tile = last_page = None


def from_source(session):
    """True for sessions from the wanted app (Spotify's desktop and Store ids both contain it)."""
    return not SOURCE_APP or SOURCE_APP in (session.source_app_user_model_id or "").lower()


async def read_session():
    global manager
    if manager is None:
        manager = await MediaManager.request_async()
    sessions = [s for s in manager.get_sessions() if from_source(s)]
    current = manager.get_current_session()
    session = (next((s for s in sessions
                     if s.get_playback_info().playback_status == PlayStatus.PLAYING), None)
               or (current if current is not None and from_source(current) else None)
               or next(iter(sessions), None))
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    is_playing = session.get_playback_info().playback_status == PlayStatus.PLAYING
    timeline = session.get_timeline_properties()
    duration = (timeline.end_time - timeline.start_time).total_seconds()
    position = timeline.position.total_seconds()
    if is_playing:  # the app reports its position as of `last_updated_time`
        updated = timeline.last_updated_time
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        drift = (datetime.now(timezone.utc) - updated).total_seconds()
        if 0 < drift < 6 * 3600:
            position += drift
    if duration > 0:
        position = min(position, duration)
    return {"title": props.title or "", "artist": clean_artist(props.artist),
            "album": props.album_title or "", "playing": is_playing,
            "position": max(0.0, position), "duration": max(0.0, duration)}


def poll_media():
    global track, playing, anchor, lyrics, fetch_token
    if loop is None:
        return
    try:
        media = loop.run_until_complete(read_session())
    except Exception as e:
        warn_once(f"media read failed: {type(e).__name__}: {e}")
        return
    if not media or not (media["title"] or media["artist"]):
        if track is not None:
            track, playing = None, False
            fetch_token += 1
            lyrics = lyr("nomedia")
        return
    playing = media["playing"]
    anchor = (media["position"], time.monotonic())
    key = f'{media["artist"]}|{media["title"]}|{media["album"]}'
    if track is None or track["key"] != key:
        track = {"key": key, "title": media["title"], "artist": media["artist"],
                 "album": media["album"], "duration": media["duration"]}
        start_fetch()
    elif media["duration"]:
        track["duration"] = media["duration"]


def position():
    seconds, read_at = anchor
    return seconds + (time.monotonic() - read_at) if playing else seconds


# --- Drawing ---------------------------------------------------------------------------

def tile_text(L, T, idx):
    st = L["status"]
    if st == "synced":
        return (L["lines"][idx][1] or NOTE) if idx >= 0 else NOTE
    if st == "plain":
        return T["title"] if T else NOTE
    return TILE_MESSAGES.get(st, "")


def tile_view(L, T, size, idx):
    tip = f'{T["title"]} \u2014 {T["artist"]}' if T else None
    dim = None if playing else "dim"
    if size == "small":
        children = [fenn.icon("music-notes", size=22, color="accent" if playing else "dim")]
    elif size == "large":
        children = [fenn.text(T["title"] if T else "Lyrics", size="small", color="dim"),
                    fenn.spacer(4),
                    fenn.text(tile_text(L, T, idx), wrap=True, bold=True, color=dim)]
    else:
        children = [fenn.text(tile_text(L, T, idx), size="small", wrap=True, color=dim)]
    return fenn.column(children, gap=2, align="center", justify="center", tooltip=tip)


def page_view(L, T, idx):
    st = L["status"]
    title = f'{T["title"]} \u2014 {T["artist"]}' if T else "Lyrics"
    head = [fenn.icon("music-notes", size=16, color="accent" if playing else "dim"),
            fenn.text(title, size="small", color="dim"), fenn.spacer()]
    if st == "synced":
        head += [fenn.button("sync_dn", label=f"-{OFFSET_STEP}s"),
                 fenn.text(f"{offset:+.1f}s", size="small", mono=True),
                 fenn.button("sync_up", label=f"+{OFFSET_STEP}s")]
    header = fenn.row(head, gap=6, align="center")

    if st == "synced":
        lines = L["lines"]
        rows = []
        for step, size, color in ((-2, "body", "faint"), (-1, "body", "dim"), (0, "title", None),
                                  (1, "body", "dim"), (2, "body", "faint")):
            i = idx + step
            if 0 <= i < len(lines):
                words = lines[i][1] or NOTE
            else:
                words = NOTE if step == 0 else " "  # intro: a note; edges: blank
            rows.append(fenn.text(words, size=size, color=color, bold=(step == 0), wrap=(step == 0)))
        return fenn.column([header, fenn.column(rows, gap=4, align="center")], gap=10)
    if st == "plain":
        body = [fenn.text(line or " ", wrap=True) for line in L["lines"]]
        return fenn.column([header] + body, gap=3, scroll=True, id=T["key"] if T else "plain")
    if st == "setup":
        return fenn.column([
            header,
            fenn.text("Python needs the Windows media packages to see what is playing. "
                      "Run this in a terminal, then restart the plugin:", size="small", wrap=True),
            fenn.text(PIP_CMD, size="small", mono=True, wrap=True, color="dim"),
            fenn.button("copy_pip", label="Copy command"),
        ], gap=8)
    return fenn.column([header, fenn.text(PAGE_MESSAGES.get(st, ""), color="dim", wrap=True)],
                       gap=10)


def tick():
    """Sends a new tile/page only when something visible changed."""
    global last_tile, last_page
    L, T = lyrics, track  # one consistent snapshot; a lookup may finish at any moment
    size = app.tile_size or "wide"
    idx = current_index(L["times"], position() + offset) if L["status"] == "synced" else -1
    key = T["key"] if T else ""

    tile_key = (size, L["status"], idx, key, playing)
    if tile_key != last_tile:
        last_tile = tile_key
        app.tile(tile_view(L, T, size, idx))
    page_key = (L["status"], idx, key, offset, playing)
    if page_key != last_page:
        last_page = page_key
        app.page(page_view(L, T, idx), PAGE_ACTIONS)


def change_offset(delta):
    global offset
    offset = round(offset + delta, 1)
    app.save("offset", offset)
    tick()


@app.on("sync_dn")
def _(event):
    change_offset(-OFFSET_STEP)


@app.on("sync_up")
def _(event):
    change_offset(OFFSET_STEP)


@app.on("refetch")
def _(event):
    if track:
        start_fetch(force=True)


@app.on("copy_pip")
def _(event):
    app.copy(PIP_CMD)


@app.on_message("layout")
def _(message):
    tick()


if __name__ == "__main__":
    if IMPORT_ERROR is not None:
        lyrics = lyr("setup")
        warn_once(f"media packages missing ({IMPORT_ERROR}); run: {PIP_CMD}")
    app.every(0.25, tick)      # lines change by position; nothing is sent unless one did
    app.every(1.0, poll_media)  # re-read title, position and play/pause once a second
    poll_media()
    tick()
    app.run()
