"""
Now playing, through Windows' own media controls.

Works with anything that shows up in the Windows volume pop-up: Spotify,
YouTube in a browser, Apple Music, Media Player, VLC and so on. If the
add-on isn't installed or Windows says no, the dashboard simply shows
"Nothing playing" and everything else keeps working.
"""

import asyncio
import datetime
import hashlib
import threading

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as exc:  # not installed, or not on Windows
    AVAILABLE = False
    IMPORT_ERROR = exc

POLL_S = 1.0
MAX_ART_BYTES = 6_000_000

APP_NAMES = [
    ("spotify", "Spotify"), ("applemusic", "Apple Music"), ("itunes", "iTunes"),
    ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
    ("308046b0af4a39cb", "Firefox"), ("opera", "Opera"), ("brave", "Brave"),
    ("vivaldi", "Vivaldi"), ("zunemusic", "Media Player"), ("media.player", "Media Player"),
    ("vlc", "VLC"), ("foobar", "foobar2000"), ("tidal", "TIDAL"), ("deezer", "Deezer"),
    ("amazonmusic", "Amazon Music"), ("youtube", "YouTube Music"), ("discord", "Discord"),
]


def app_name(aumid):
    low = (aumid or "").lower()
    for key, name in APP_NAMES:
        if key in low:
            return name
    base = low.split("!")[-1].split("\\")[-1]
    if base.endswith(".exe"):
        base = base[:-4]
    return base.replace("_", " ").title() if base else None


class Media:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"available": AVAILABLE, "title": None}
        self.art = None
        self.error = None if AVAILABLE else str(IMPORT_ERROR)
        self.loop = None
        self.wake = None
        if AVAILABLE:
            threading.Thread(target=self._run, name="media", daemon=True).start()

    # ---- public (called from other threads) -------------------------------
    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def art_bytes(self):
        with self.lock:
            return self.art

    def command(self, action):
        """action: 'toggle' | 'next' | 'prev'. Returns True if it was sent."""
        if not self.loop or action not in ("toggle", "next", "prev"):
            return False
        fut = asyncio.run_coroutine_threadsafe(self._command(action), self.loop)
        try:
            return bool(fut.result(timeout=3))
        except Exception:
            return False

    # ---- background loop ----------------------------------------------------
    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.error = str(exc)
            with self.lock:
                self.state = {"available": False, "title": None}

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.manager = await MediaManager.request_async()
        last_track = None
        while True:
            try:
                last_track = await self._refresh(last_track)
            except Exception as exc:  # one bad read shouldn't stop music updates
                self.error = str(exc)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _refresh(self, last_track):
        session = self.manager.get_current_session()
        if session is None:
            with self.lock:
                self.state = {"available": True, "title": None}
                self.art = None
            return None
        props = await session.try_get_media_properties_async()
        info = session.get_playback_info()
        timeline = session.get_timeline_properties()
        if props is None or not (props.title or "").strip():
            with self.lock:
                self.state = {"available": True, "title": None}
            return None

        track = (props.title, props.artist, props.album_title)
        art = None
        art_changed = track != last_track
        if art_changed:
            art = await self._read_art(props.thumbnail)

        playing = info is not None and info.playback_status == PlaybackStatus.PLAYING
        position, duration = self._timeline(timeline, playing)
        key = hashlib.sha1(repr(track).encode("utf-8", "replace")).hexdigest()[:12]
        with self.lock:
            if art_changed:
                self.art = art
            has_art = self.art is not None
            self.state = {
                "available": True,
                "title": props.title,
                "artist": props.artist or props.album_artist or "",
                "album": props.album_title or "",
                "app": app_name(session.source_app_user_model_id),
                "playing": playing,
                "position_s": position,
                "duration_s": duration,
                "art": f"/media/art?k={key}" if has_art else None,
            }
        return track

    @staticmethod
    def _timeline(timeline, playing):
        try:
            end = timeline.end_time.total_seconds() - timeline.start_time.total_seconds()
            pos = timeline.position.total_seconds() - timeline.start_time.total_seconds()
        except Exception:
            return None, None
        if end <= 1:
            return None, None  # this app doesn't report progress
        try:
            updated = timeline.last_updated_time
            if playing and updated and updated.year > 2000:
                now = datetime.datetime.now(datetime.timezone.utc)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=datetime.timezone.utc)
                pos += max(0.0, (now - updated).total_seconds())
        except Exception:
            pass
        return max(0.0, min(pos, end)), end

    @staticmethod
    async def _read_art(ref):
        if ref is None:
            return None
        try:
            stream = await ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > MAX_ART_BYTES:
                return None
            buf = await stream.read_async(Buffer(size), size, InputStreamOptions.READ_AHEAD)
            try:
                data = bytes(memoryview(buf))[: buf.length]
            except TypeError:
                reader = DataReader.from_buffer(buf)
                arr = bytearray(buf.length)
                reader.read_bytes(arr)
                data = bytes(arr)
            if data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG" or data[:4] == b"RIFF":
                return data
        except Exception:
            return None
        return None

    async def _command(self, action):
        session = self.manager.get_current_session()
        if session is None:
            return False
        if action == "toggle":
            ok = await session.try_toggle_play_pause_async()
        elif action == "next":
            ok = await session.try_skip_next_async()
        else:
            ok = await session.try_skip_previous_async()
        self.wake.set()
        return ok
