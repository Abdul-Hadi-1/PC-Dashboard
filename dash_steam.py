"""
Recently played Steam games for the idle screen, and launching them with a tap.

Reads Steam's own files on your PC (which games are installed, when you last
played them, and the box art Steam has already downloaded). Nothing is written.
"""

import os
import re
import threading
import time
import urllib.request
from pathlib import Path

REFRESH_S = 600
SKIP_APPIDS = {"228980", "250820", "1070560", "1391110", "1628350", "1493710", "2180100"}
SKIP_NAMES = re.compile(r"redistributable|soundtrack|dedicated server|\bsdk\b|steamvr|proton|"
                        r"linux runtime|wallpaper engine|benchmark|demo$", re.I)


def parse_vdf(text):
    """Parse Valve's KeyValues text format into nested dicts."""
    root = {}
    stack = [root]
    key = None
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            child = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = s
        else:
            stack[-1][key] = s.replace("\\\\", "\\")
            key = None
    return root


def _get(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        match = next((v for k, v in d.items() if k.lower() == p.lower()), None)
        d = match
    return d


class RecentGames:
    def __init__(self, games, count=4, download=True):
        self.games = games          # dashboard.GameInfo (Steam paths + image registry)
        self.count = count
        self.download = download
        self.items = []
        self.at = 0.0
        self.busy = False
        self.lock = threading.Lock()

    def list(self):
        if not self.busy and time.monotonic() - self.at > REFRESH_S:
            self.busy = True
            threading.Thread(target=self._refresh, name="steam-recent", daemon=True).start()
        with self.lock:
            return list(self.items)

    def invalidate(self):
        self.at = 0.0

    def launch(self, appid):
        with self.lock:
            allowed = {i["appid"] for i in self.items}
        if str(appid) not in allowed:
            return False
        try:
            os.startfile(f"steam://rungameid/{appid}")  # Windows only
            return True
        except Exception:
            return False

    # ---- internals ---------------------------------------------------------
    def _refresh(self):
        try:
            items = self._build()
            with self.lock:
                self.items = items
        except Exception:
            pass
        finally:
            self.at = time.monotonic()
            self.busy = False

    def _build(self):
        g = self.games
        g._steam()
        root = g.steam_root
        if not root:
            return []
        installed = {appid: name for _lib, _inst, appid, name in (g.manifests or []) if name}
        played = self._last_played(root)
        ranked = sorted(played.items(), key=lambda kv: kv[1], reverse=True)
        items = []
        for appid, _ts in ranked:
            name = installed.get(appid)
            if not name or appid in SKIP_APPIDS or SKIP_NAMES.search(name):
                continue
            items.append({"appid": appid, "name": name, "art": self._art(root, appid)})
            if len(items) >= self.count:
                break
        return items

    @staticmethod
    def _last_played(root):
        userdata = Path(root, "userdata")
        configs = []
        try:
            for d in userdata.iterdir():
                f = d / "config" / "localconfig.vdf"
                if f.is_file():
                    configs.append(f)
        except OSError:
            return {}
        if not configs:
            return {}
        newest = max(configs, key=lambda f: f.stat().st_mtime)  # the account you use
        data = parse_vdf(newest.read_text(encoding="utf-8", errors="replace"))
        apps = _get(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
        out = {}
        for appid, info in apps.items():
            ts = _get(info, "LastPlayed") if isinstance(info, dict) else None
            if appid.isdigit() and ts and str(ts).isdigit() and int(ts) > 0:
                out[appid] = int(ts)
        return out

    def _art(self, root, appid):
        cache = os.path.join(root, "appcache", "librarycache")
        candidates = [os.path.join(cache, f"{appid}_library_600x900.jpg"),
                      os.path.join(cache, f"{appid}_library_600x900_2x.jpg")]
        sub = os.path.join(cache, appid)
        if os.path.isdir(sub):
            for dirpath, _dirs, files in os.walk(sub):
                for fn in files:
                    if fn.lower().startswith("library_600x900") or fn.lower() == "library_capsule.jpg":
                        candidates.append(os.path.join(dirpath, fn))
        for c in candidates:
            if os.path.isfile(c):
                return self.games._register_image(c)
        if not self.download:
            return None
        for url in (f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900.jpg",
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PC-Stats-Dashboard"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    data = r.read()
                if data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG":
                    return self.games._register_image(data)
            except Exception:
                continue
        return None
