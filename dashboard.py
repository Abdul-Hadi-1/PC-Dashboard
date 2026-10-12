#!/usr/bin/env python3
"""
PC Stats Dashboard
------------------
Shows live GPU / CPU / FPS stats on a second screen, with the running game's
artwork as the background.

Data comes from programs you already run:
  * MSI Afterburner  - GPU + CPU sensors, and RivaTuner's Min/Avg/Max/1% low
  * RivaTuner (RTSS) - which game is running and its current FPS
  * HWiNFO (optional) - fallback for CPU sensors Afterburner doesn't have
  * Windows itself    - now playing, volume, sound output, mic mute
  * Steam's own files - game names, artwork, recently played games

Nothing is injected into games and no stats are written to disk. The music,
sound, tray and Steam parts are optional: if one can't load, the rest works.

Usage:
  pythonw dashboard.py              start the dashboard (opens on the small screen)
  python  dashboard.py --diagnose   print what the app can see, then exit
  python  dashboard.py --demo       fake data, for previewing the look
  python  dashboard.py --no-browser only start the local server
"""

import argparse
import copy
import ctypes
import hashlib
import json
import math
import ntpath  # Windows path rules, regardless of where this runs
import os
import random
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Optional parts: each one handles its own missing add-ons and never stops the stats.
try:
    import dash_media
except Exception:
    dash_media = None
try:
    import dash_audio
except Exception:
    dash_audio = None
try:
    import dash_tray
except Exception:
    dash_tray = None
try:
    import dash_steam
except Exception:
    dash_steam = None

WEB_DIR = APP_DIR / "web"
BG_DIR = APP_DIR / "backgrounds"
CONFIG_PATH = APP_DIR / "config.json"
IS_WIN = sys.platform == "win32"
WINDOW_TITLE = "PC Stats Dashboard"
WS_EX_TOOLWINDOW = 0x00000080   # no taskbar button, not in Alt+Tab
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000   # taps don't take focus from the game

DEFAULT_CONFIG = {
    "port": 8787,
    "refresh_ms": 500,
    # "auto" = the smallest screen that isn't your main one.
    # Or a number: 1 = first screen Windows reports, 2 = second, ...
    "screen": "auto",
    # Leave empty to use the names Windows reports.
    "gpu_name": "",
    "cpu_name": "",
    # Temperatures (°C) where the reading turns amber, then red.
    "temp_warm": 75,
    "temp_hot": 87,
    # Programs that RivaTuner sees but that aren't games.
    "ignore_programs": [],
    # Rename games, e.g. {"witcher3.exe": "The Witcher 3"}
    "game_names": {},
    # Download artwork from Steam's servers when Steam hasn't cached it locally.
    "download_steam_art": True,
    # Background when no game is running: "wallpaper", "none", or a path to an image.
    "idle_background": "wallpaper",
    # Stop the app when the dashboard window has been closed for this many seconds
    # (only when there's no tray icon; with the tray icon, use Stop dashboard).
    "exit_when_closed_s": 120,
    # The key your RivaTuner "Begin recording" is set to. The Reset button presses it.
    "reset_key": "ctrl+v",
    # Keep the dashboard window out of the taskbar and Alt+Tab (tray icon instead).
    "hide_from_taskbar": True,
    # Tapping the dashboard doesn't take focus away from your game.
    "keep_game_focused": True,
    # After each tap on the touchscreen, give focus and the mouse pointer back to
    # your game (Windows moves them to the dashboard when you touch it).
    "give_focus_back": True,
    # How many recently played Steam games to show when no game is running (0 = none).
    "recent_games": 4,
    # Sessions shorter than this don't get an end-of-session summary.
    "summary_min_s": 60,
}

# Programs RivaTuner can hook that are never the game you're playing.
BUILTIN_IGNORE = {
    "msedge.exe", "chrome.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe",
    "vivaldi.exe", "explorer.exe", "dwm.exe", "steamwebhelper.exe", "steam.exe",
    "discord.exe", "msiafterburner.exe", "rtss.exe", "encoderserver.exe",
    "encoderserver64.exe", "desktopoverlayhost.exe", "desktopoverlayhost64.exe",
    "nvidia overlay.exe", "nvidia app.exe", "nvcontainer.exe", "wallpaper32.exe",
    "wallpaper64.exe", "lively.exe", "livelywpf.exe", "obs64.exe", "obs32.exe",
    "epicgameslauncher.exe", "galaxyclient.exe", "eadesktop.exe", "battle.net.exe",
    "applicationframehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "shellexperiencehost.exe", "widgets.exe", "hwinfo64.exe",
    "spotify.exe", "code.exe", "claude.exe", "ubisoftconnect.exe", "riotclientux.exe",
    "python.exe", "pythonw.exe", "py.exe", "pyw.exe",
    "asus_framework.exe", "armourycrate.exe", "armourycrateservice.exe", "nearby_share.exe",
    "phoneexperiencehost.exe", "steelseriesgg.exe", "steelseriesengine.exe", "icue.exe",
    "signalrgb.exe", "razer synapse 3.exe", "razerappengine.exe", "lghub.exe", "msedgewebview2.exe",
    "nvidia share.exe", "radeonsoftware.exe", "amdrsserv.exe", "widgetservice.exe",
}


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
                if any(k not in user for k in DEFAULT_CONFIG):
                    # Add settings from a newer version, keeping the user's own values.
                    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as exc:  # keep going with defaults
            print(f"config.json couldn't be read ({exc}); using defaults.")
    else:
        try:
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        except OSError:
            pass
    return cfg


def num(v):
    """Afterburner/HWiNFO use huge values to mean 'no reading'."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or abs(f) > 1e12:
        return None
    return f


def norm(name):
    """Normalise a sensor name for matching: 'Framerate 1% Low' -> 'framerate1low'."""
    n = name.lower().replace("frametate", "framerate")
    n = re.sub(r"^gpu\d+\s", "gpu ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def norm_gpu(name):
    """For graphics-card readings, ignore the 'GPU'/'GPU2' prefix Afterburner may add:
    'GPU2 core clock', 'GPU core clock' and 'Core clock' all become 'coreclock'."""
    n = re.sub(r"^gpu\d*\s*", "", name.lower().strip())
    return re.sub(r"[^a-z0-9]", "", n)


# --------------------------------------------------------------------------
# Windows plumbing (shared memory, processes, registry, screens)
# --------------------------------------------------------------------------
if IS_WIN:
    from ctypes import wintypes
    import winreg

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    FILE_MAP_READ = 0x0004
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k32.UnmapViewOfFile.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    k32.VirtualQuery.restype = ctypes.c_size_t
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.InternalGetWindowText.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.BringWindowToTop.argtypes = [wintypes.HWND]
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    k32.GetCurrentThreadId.restype = wintypes.DWORD
    u32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    u32.MapVirtualKeyW.restype = wintypes.UINT
    u32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    u32.SendInput.restype = wintypes.UINT

    _LONG_PTR = ctypes.c_ssize_t
    _get_ex = getattr(u32, "GetWindowLongPtrW", None) if hasattr(u32, "GetWindowLongPtrW") \
        else u32.GetWindowLongW
    _set_ex = getattr(u32, "SetWindowLongPtrW", None) if hasattr(u32, "SetWindowLongPtrW") \
        else u32.SetWindowLongW
    _get_ex.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_ex.restype = _LONG_PTR
    _set_ex.argtypes = [wintypes.HWND, ctypes.c_int, _LONG_PTR]
    _set_ex.restype = _LONG_PTR

    def u32_get_exstyle(hwnd):
        return _get_ex(hwnd, -20) & 0xFFFFFFFF  # GWL_EXSTYLE

    def u32_set_exstyle(hwnd, value):
        _set_ex(hwnd, -20, value)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("RegionSize", ctypes.c_size_t),
                    ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                    ("Type", wintypes.DWORD)]

    def read_shared_memory(name, size_from_header, header_len=64):
        """Copy a named shared-memory block. Returns bytes or None if it doesn't exist."""
        handle = k32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not handle:
            return None
        try:
            addr = k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
            if not addr:
                return None
            try:
                mbi = MEMORY_BASIC_INFORMATION()
                k32.VirtualQuery(addr, ctypes.byref(mbi), ctypes.sizeof(mbi))
                region = mbi.RegionSize or header_len
                head = ctypes.string_at(addr, min(header_len, region))
                size = size_from_header(head)
                if not size:
                    return None
                return ctypes.string_at(addr, min(size, region))
            finally:
                k32.UnmapViewOfFile(addr)
        finally:
            k32.CloseHandle(handle)

    def process_alive(pid):
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)

    def process_path(pid):
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return None
        finally:
            k32.CloseHandle(h)

    def foreground_pid():
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def reg_value(root, path, name):
        try:
            with winreg.OpenKey(root, path) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def file_description(path):
        """FileDescription / ProductName from an .exe's version info."""
        try:
            ver = ctypes.WinDLL("version")
            size = ver.GetFileVersionInfoSizeW(path, None)
            if not size:
                return None
            data = ctypes.create_string_buffer(size)
            if not ver.GetFileVersionInfoW(path, 0, size, data):
                return None
            ptr, plen = ctypes.c_void_p(), wintypes.UINT()
            langs = []
            if ver.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                  ctypes.byref(ptr), ctypes.byref(plen)) and plen.value >= 4:
                raw = ctypes.string_at(ptr.value, plen.value)
                for i in range(0, len(raw) - 3, 4):
                    lang, cp = struct.unpack_from("<HH", raw, i)
                    langs.append(f"{lang:04x}{cp:04x}")
            langs += ["040904b0", "040904e4", "000004b0"]
            for field in ("FileDescription", "ProductName"):
                for lc in langs:
                    if ver.VerQueryValueW(data, f"\\StringFileInfo\\{lc}\\{field}",
                                          ctypes.byref(ptr), ctypes.byref(plen)) and plen.value:
                        text = ctypes.wstring_at(ptr.value, plen.value).strip("\x00 ").strip()
                        if text:
                            return text
        except Exception:
            return None
        return None
else:  # lets --demo run anywhere
    def read_shared_memory(name, size_from_header, header_len=64):
        return None

    def process_alive(pid):
        return False

    def process_path(pid):
        return None

    def foreground_pid():
        return 0

    def reg_value(root, path, name):
        return None

    def file_description(path):
        return None


def cstr(buf, off, length):
    raw = buf[off:off + length].split(b"\x00", 1)[0]
    return raw.decode("mbcs" if IS_WIN else "latin-1", errors="replace")


# --------------------------------------------------------------------------
# MSI Afterburner (MAHMSharedMemory)
# --------------------------------------------------------------------------
MAHM_SIG = 0x4D41484D  # 'MAHM'


def _mahm_size(head):
    if len(head) < 32:
        return 0
    sig, _ver, hdr, n, esz, _t, ngpu, gsz = struct.unpack_from("<IIIIIiII", head)
    if sig != MAHM_SIG:
        return 0
    return hdr + n * esz + ngpu * gsz


def read_afterburner():
    buf = read_shared_memory("MAHMSharedMemory", _mahm_size)
    if not buf:
        return None
    sig, _ver, hdr, n, esz, _t, ngpu, gsz = struct.unpack_from("<IIIIIiII", buf)
    if sig != MAHM_SIG:
        return None
    entries = []
    for i in range(n):
        off = hdr + i * esz
        if off + 1324 > len(buf):
            break
        data, lo, hi = struct.unpack_from("<fff", buf, off + 1300)
        _flags, gpu, _src = struct.unpack_from("<III", buf, off + 1312)
        entries.append({
            "name": cstr(buf, off, 260),
            "units": cstr(buf, off + 260, 260),
            "value": num(data),
            "max": num(hi),
            "gpu": gpu,
        })
    gpus = []
    base = hdr + n * esz
    for j in range(ngpu):
        off = base + j * gsz
        if off + 1300 > len(buf):
            break
        gpus.append({
            "index": j,
            "family": cstr(buf, off + 260, 260),
            "device": cstr(buf, off + 520, 260),
            "memory": struct.unpack_from("<I", buf, off + 1300)[0] if off + 1304 <= len(buf) else 0,
        })
    return {"entries": entries, "gpus": gpus}


def pick_nvidia_gpu(ab):
    """Index of the dedicated GPU (skips the Ryzen's built-in graphics)."""
    gpus = ab["gpus"]
    if not gpus:
        return 0
    for g in gpus:
        text = (g["device"] + " " + g["family"]).lower()
        if any(k in text for k in ("nvidia", "geforce", "rtx", "gtx")):
            return g["index"]
    for g in gpus:  # a Radeon card that isn't the iGPU
        text = (g["device"] + " " + g["family"]).lower()
        if "rx " in text or "radeon rx" in text:
            return g["index"]
    return max(gpus, key=lambda g: g["memory"])["index"]


class AfterburnerView:
    def __init__(self, ab):
        self.ab = ab
        self.gpu = pick_nvidia_gpu(ab) if ab else 0

    def find(self, names, gpu=None):
        if not self.ab:
            return None
        key = norm_gpu if gpu is not None else norm
        wanted = [key(n) for n in names]
        for want in wanted:
            for e in self.ab["entries"]:
                if key(e["name"]) != want:
                    continue
                if gpu is not None and e["gpu"] != gpu and len(self.ab["gpus"]) > 1:
                    continue
                if e["value"] is not None:
                    return e
        return None

    def value(self, names, gpu=None):
        e = self.find(names, gpu)
        return e["value"] if e else None

    def gpu_name(self):
        for g in (self.ab or {}).get("gpus", []):
            if g["index"] == self.gpu:
                return g["device"] or g["family"]
        return None


# --------------------------------------------------------------------------
# RivaTuner Statistics Server (RTSSSharedMemoryV2)
# --------------------------------------------------------------------------
RTSS_SIG = 0x52545353  # 'RTSS'


def _rtss_size(head):
    if len(head) < 32:
        return 0
    sig, ver, esz, arr_off, arr_n = struct.unpack_from("<5I", head)
    if sig != RTSS_SIG or ver < 0x00020000:
        return 0
    return arr_off + arr_n * esz


def read_rtss():
    buf = read_shared_memory("RTSSSharedMemoryV2", _rtss_size)
    if not buf:
        return None
    sig, ver, esz, arr_off, arr_n = struct.unpack_from("<5I", buf)
    if sig != RTSS_SIG:
        return None
    apps = []
    for i in range(arr_n):
        off = arr_off + i * esz
        if off + 284 > len(buf):
            break
        pid = struct.unpack_from("<I", buf, off)[0]
        if not pid:
            continue
        path = cstr(buf, off + 4, 260)
        _flags, t0, t1, frames = struct.unpack_from("<4I", buf, off + 264)
        fps = 1000.0 * frames / (t1 - t0) if t1 > t0 and frames else None
        apps.append({"pid": pid, "path": path, "exe": ntpath.basename(path).lower(),
                     "time1": t1, "fps": fps})
    return {"apps": apps}


# --------------------------------------------------------------------------
# HWiNFO (optional fallback)
# --------------------------------------------------------------------------
HWINFO_SIG = 0x53695748  # 'HWiS'


def _hwinfo_size(head):
    if len(head) < 44:
        return 0
    sig = struct.unpack_from("<I", head)[0]
    if sig != HWINFO_SIG:
        return 0
    s_off, s_sz, s_n, r_off, r_sz, r_n = struct.unpack_from("<6I", head, 20)
    return max(s_off + s_sz * s_n, r_off + r_sz * r_n)


def read_hwinfo():
    buf = read_shared_memory("Global\\HWiNFO_SENS_SM2", _hwinfo_size)
    if not buf:
        return None
    s_off, s_sz, s_n, r_off, r_sz, r_n = struct.unpack_from("<6I", buf, 20)
    sensors = [cstr(buf, s_off + i * s_sz + 8, 128) for i in range(s_n)]
    readings = []
    for i in range(r_n):
        off = r_off + i * r_sz
        if off + 292 > len(buf):
            break
        kind, sidx, _rid = struct.unpack_from("<3I", buf, off)
        label = cstr(buf, off + 12, 128)
        unit = cstr(buf, off + 268, 16)
        value = struct.unpack_from("<d", buf, off + 284)[0]
        readings.append({"kind": kind, "sensor": sensors[sidx] if sidx < len(sensors) else "",
                         "label": label, "unit": unit, "value": num(value)})
    return {"readings": readings}


def hwinfo_value(hw, labels, sensor_has=None):
    if not hw:
        return None
    for want in labels:
        w = want.lower()
        for r in hw["readings"]:
            if r["label"].lower() != w or r["value"] is None:
                continue
            if sensor_has and not any(s in r["sensor"].lower() for s in sensor_has):
                continue
            return r["value"]
    return None


# --------------------------------------------------------------------------
# Game name + artwork
# --------------------------------------------------------------------------
class GameInfo:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache = {}           # exe path -> info
        self.images = {}          # key -> (bytes or Path)
        self.manifests = None     # list of (library, installdir_lower, appid, name)
        self.steam_root = None
        self.lock = threading.Lock()

    # ---- Steam -------------------------------------------------------------
    def _steam(self):
        if self.manifests is not None:
            return
        self.manifests = []
        root = None
        if IS_WIN:
            root = reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath") or \
                reg_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")
        if not root or not os.path.isdir(root):
            return
        self.steam_root = os.path.normpath(root)
        libs = {self.steam_root}
        vdf = os.path.join(self.steam_root, "steamapps", "libraryfolders.vdf")
        try:
            text = Path(vdf).read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                libs.add(os.path.normpath(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
        for lib in libs:
            apps = os.path.join(lib, "steamapps")
            try:
                names = os.listdir(apps)
            except OSError:
                continue
            for fn in names:
                if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                    continue
                try:
                    acf = Path(apps, fn).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                appid = re.search(r'"appid"\s+"(\d+)"', acf)
                name = re.search(r'"name"\s+"([^"]*)"', acf)
                inst = re.search(r'"installdir"\s+"([^"]*)"', acf)
                if appid and inst:
                    self.manifests.append((lib, inst.group(1).lower(), appid.group(1),
                                           name.group(1) if name else None))

    def _steam_match(self, path, retry=True):
        self._steam()
        p = ntpath.normcase(ntpath.normpath(path))
        for lib, inst, appid, name in self.manifests:
            common = ntpath.normcase(ntpath.join(lib, "steamapps", "common")) + "\\"
            if p.startswith(common):
                first = p[len(common):].split("\\", 1)[0]
                if first == ntpath.normcase(inst):
                    return appid, name
        if retry and "steamapps" in p:  # a game installed after the dashboard started
            self.manifests = None
            return self._steam_match(path, retry=False)
        return None, None

    def _steam_local_art(self, appid):
        if not self.steam_root:
            return None
        cache = os.path.join(self.steam_root, "appcache", "librarycache")
        found = []
        for fn in (f"{appid}_library_hero.jpg", f"{appid}_library_hero.png", f"{appid}_header.jpg"):
            p = os.path.join(cache, fn)
            if os.path.isfile(p):
                found.append(p)
        sub = os.path.join(cache, appid)
        if os.path.isdir(sub):
            for dirpath, _dirs, files in os.walk(sub):
                for fn in files:
                    if fn.lower() in ("library_hero.jpg", "library_hero.png", "header.jpg"):
                        found.append(os.path.join(dirpath, fn))

        def rank(p):
            b = os.path.basename(p).lower()
            return (0 if "library_hero" in b else 1, -os.path.getsize(p))
        found.sort(key=rank)
        return found[0] if found else None

    def _steam_download(self, appid):
        urls = [
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_hero.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_hero.jpg",
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
        ]
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PC-Stats-Dashboard"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    data = r.read()
                if data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG":
                    return data
            except Exception:
                continue
        return None

    # ---- public ------------------------------------------------------------
    def _register_image(self, src):
        key = hashlib.sha1(repr(src if isinstance(src, str) else len(src)).encode()
                           + (src[:64] if isinstance(src, bytes) else b"")).hexdigest()[:16]
        self.images[key] = src
        return f"/bg/{key}"

    def info(self, path):
        with self.lock:
            if path in self.cache:
                return self.cache[path]
        exe = ntpath.basename(path)
        stem = ntpath.splitext(exe)[0]
        appid, steam_name = self._steam_match(path)
        overrides = {k.lower(): v for k, v in self.cfg.get("game_names", {}).items()}
        name = overrides.get(exe.lower()) or steam_name or file_description(path) or pretty_exe(stem)
        info = {"name": name, "exe": exe, "appid": appid, "bg": None}
        with self.lock:
            self.cache[path] = info
        # Artwork can involve a download, so do it off the sampling thread.
        threading.Thread(target=self._find_art, args=(info, stem, appid), daemon=True).start()
        return info

    def _find_art(self, info, stem, appid):
        src = None
        if BG_DIR.is_dir():
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                p = BG_DIR / (stem + ext)
                if p.is_file():
                    src = str(p)
                    break
        if not src and appid:
            local = self._steam_local_art(appid)
            if local and "library_hero" in os.path.basename(local).lower():
                src = local
            elif self.cfg.get("download_steam_art", True):
                src = self._steam_download(appid) or local
            else:
                src = local
        if src:
            with self.lock:
                info["bg"] = self._register_image(src)

    def idle_background(self):
        choice = self.cfg.get("idle_background", "wallpaper")
        if not choice or choice == "none":
            return None
        if choice == "wallpaper":
            if not IS_WIN:
                return None
            candidates = [os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                                       "Themes", "TranscodedWallpaper"),
                          reg_value(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "WallPaper")]
        else:
            candidates = [choice]
        for c in candidates:
            if c and os.path.isfile(c):
                key = self._register_image(c)
                return key
        return None

    def image(self, key):
        src = self.images.get(key)
        if src is None:
            return None
        if isinstance(src, bytes):
            return src
        try:
            return Path(src).read_bytes()
        except OSError:
            return None


def pretty_exe(stem):
    s = re.sub(r"[-_.]+", " ", stem)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"\b(win64|x64|dx11|dx12|shipping|vulkan)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().title() or stem


def short_cpu_name(raw):
    s = raw or ""
    s = re.sub(r"\(R\)|\(TM\)|\bCPU\b|@.*$", "", s, flags=re.I)
    s = re.sub(r"\s+\d+-Core Processor", "", s, flags=re.I)
    s = re.sub(r"\bProcessor\b", "", s, flags=re.I)
    s = re.sub(r"^\s*(AMD|Intel)\s+", "", s, flags=re.I)
    s = s.replace("Core ", "Core ")
    return re.sub(r"\s+", " ", s).strip()


def short_gpu_name(raw):
    s = raw or ""
    s = re.sub(r"^\s*(NVIDIA\s+)?(GeForce\s+)?", "", s, flags=re.I)
    s = re.sub(r"^\s*AMD\s+(Radeon\s+)?", "Radeon ", s, flags=re.I)
    s = re.sub(r"\bTi\b", "Ti", s, flags=re.I)
    return s.strip()


# --------------------------------------------------------------------------
# Sampler: reads everything twice a second and keeps the latest snapshot
# --------------------------------------------------------------------------
class Sampler:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.games = GameInfo(cfg)
        self.snapshot = {"ok": False}
        self.lock = threading.Lock()
        self.current_pid = None
        self.seen = {}        # pid -> (last frame-counter time, when it last changed)
        self.local = deque()  # (time, fps) for the rolling fallback stats
        self.ignore = BUILTIN_IGNORE | {p.lower() for p in cfg.get("ignore_programs", [])}
        self.cpu_name = cfg.get("cpu_name") or short_cpu_name(
            reg_value(winreg.HKEY_LOCAL_MACHINE,
                      r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                      "ProcessorNameString") if IS_WIN else "") or "CPU"
        self.idle_bg = None if demo else self.games.idle_background()
        self.last_request = time.monotonic()
        self.served_once = False
        self.sessions = {}    # pid -> running totals for the end-of-session summary
        self.summary = None
        self.media = None     # set by main() when the add-ons are available
        self.audio = None
        self.recent = None

    # ---- choosing the game ------------------------------------------------
    def choose_game(self, rtss):
        if not rtss:
            self.current_pid = None
            return None
        now = time.monotonic()
        apps = [a for a in rtss["apps"] if a["exe"] not in self.ignore]
        # A program is "rendering" if RivaTuner updated its frame counter in the last 3 s.
        for a in apps:
            prev = self.seen.get(a["pid"])
            if prev is None:
                self.seen[a["pid"]] = (a["time1"], float("-inf"))
            elif prev[0] != a["time1"]:
                self.seen[a["pid"]] = (a["time1"], now)
        live = {a["pid"] for a in apps}
        for pid in list(self.seen):
            if pid not in live:
                del self.seen[pid]
        fresh = [a for a in apps if now - self.seen[a["pid"]][1] < 3.0]
        fresh_pids = {a["pid"] for a in fresh}
        by_pid = {a["pid"]: a for a in apps}
        fg = foreground_pid()

        pick = by_pid.get(fg)  # the game you're playing is normally in focus
        if pick is None and self.current_pid in by_pid and process_alive(self.current_pid):
            pick = by_pid[self.current_pid]   # stay on it when you tap the dashboard
        if pick is None and fresh:
            pick = max(fresh, key=lambda a: a["fps"] or 0)
        if pick is None:
            self.current_pid = None
            return None
        if pick["pid"] != self.current_pid:
            self.current_pid = pick["pid"]
            self.local.clear()
        pick = dict(pick)
        pick["active"] = pick["pid"] in fresh_pids
        return pick

    # ---- one sample --------------------------------------------------------
    def sample(self):
        if self.demo:
            return demo_snapshot(self)
        ab_raw = read_afterburner()
        ab = AfterburnerView(ab_raw)
        rtss = read_rtss()
        hw = read_hwinfo()
        nv = ("nvidia", "geforce", "rtx", "gtx", "radeon rx")

        gpu = {
            "name": self.cfg.get("gpu_name") or short_gpu_name(ab.gpu_name() or "") or "GPU",
            "usage": ab.value(["GPU usage"], ab.gpu) if ab_raw else
            hwinfo_value(hw, ["GPU Core Load", "GPU Utilization"], nv),
            "temp": ab.value(["GPU temperature"], ab.gpu) if ab_raw else
            hwinfo_value(hw, ["GPU Temperature"], nv),
            "extras": [],
        }
        clock = ab.value(["Core clock"], ab.gpu) if ab_raw else hwinfo_value(hw, ["GPU Clock"], nv)
        power_e = ab.find(["Power consumption", "GPU power", "Power"], ab.gpu)
        power_unit = "W"
        power = hwinfo_value(hw, ["GPU Power", "Total Board Power"], nv)
        if power_e and power_e["units"].strip().upper() == "W":
            power = power_e["value"]
        elif power is None and power_e and "%" in power_e["units"]:
            power, power_unit = power_e["value"], "% TDP"
        vram = ab.value(["Memory usage"], ab.gpu) if ab_raw else \
            hwinfo_value(hw, ["GPU Memory Allocated", "GPU D3D Memory Dedicated"], nv)
        add_extra(gpu["extras"], "Clock", clock, "MHz", 0)
        add_extra(gpu["extras"], "Power", power, power_unit, 0)
        if vram is not None:
            add_extra(gpu["extras"], "VRAM", vram / 1024.0, "GB", 1)

        cpu_hw = ("cpu", "ryzen", "core", "intel", "amd")
        cpu = {
            "name": self.cpu_name,
            "usage": ab.value(["CPU usage"]) if ab.find(["CPU usage"]) else
            hwinfo_value(hw, ["Total CPU Usage"]),
            "temp": ab.value(["CPU temperature"]) if ab.find(["CPU temperature"]) else
            hwinfo_value(hw, ["CPU (Tctl/Tdie)", "CPU Package", "CPU Die (average)",
                              "Core Temperatures (avg)", "CPU (Tctl)"], cpu_hw),
            "extras": [],
        }
        cclock = ab.value(["CPU clock"]) or hwinfo_value(hw, ["Core Clocks (avg)", "Average Effective Clock"])
        cpower = ab.value(["CPU power"]) or hwinfo_value(hw, ["CPU Package Power", "CPU PPT"])
        ram = ab.value(["RAM usage"]) or hwinfo_value(hw, ["Physical Memory Used"])
        add_extra(cpu["extras"], "Clock", cclock, "MHz", 0)
        add_extra(cpu["extras"], "Power", cpower, "W", 0)
        if ram is not None:
            add_extra(cpu["extras"], "RAM", ram / 1024.0, "GB", 1)

        game = None
        pick = self.choose_game(rtss)
        if pick:
            info = self.games.info(pick["path"])
            fps = pick["fps"] if pick["active"] else 0.0
            if fps is None:
                fps = ab.value(["Framerate"])
            game = {"name": info["name"], "exe": info["exe"], "active": pick["active"],
                    "fps": fps, "frametime": 1000.0 / fps if fps else None,
                    "bg": info["bg"]}
            stats = {
                "min": ab.value(["Framerate Min"]),
                "avg": ab.value(["Framerate Avg"]),
                "max": ab.value(["Framerate Max"]),
                "low1": ab.value(["Framerate 1% Low"]),
            }
            if any(v is not None for v in stats.values()):
                game.update(stats)
                game["stats"] = "rivatuner"
            else:
                game.update(self.rolling_stats(fps if pick["active"] else None))
                game["stats"] = "rolling"
        self.track_sessions(pick, game, gpu, cpu)
        return {
            "ok": True,
            "sources": {"afterburner": ab_raw is not None, "rivatuner": rtss is not None,
                        "hwinfo": hw is not None},
            "gpu": gpu, "cpu": cpu, "game": game,
            "summary": None if game else self.summary,
            "bg": (game or {}).get("bg") or self.idle_bg,
            "temp_warm": self.cfg.get("temp_warm", 75), "temp_hot": self.cfg.get("temp_hot", 87),
            "refresh_ms": self.cfg.get("refresh_ms", 500),
            **self.extras(game),
        }

    def extras(self, game):
        """Music, sound and recent games. Each part is optional."""
        out = {"media": {"available": False}, "audio": {"available": False}, "recent": []}
        for key, part in (("media", self.media), ("audio", self.audio)):
            if part is not None:
                try:
                    out[key] = part.snapshot()
                except Exception:
                    pass
        if self.recent is not None and not game:
            try:
                out["recent"] = self.recent.list()
            except Exception:
                pass
        return out

    # ---- play sessions (memory only) ------------------------------------------
    def track_sessions(self, pick, game, gpu, cpu):
        now = time.monotonic()
        if pick and game:
            s = self.sessions.get(pick["pid"])
            if s is None:
                s = self.sessions[pick["pid"]] = {
                    "name": game["name"], "start": now, "fps_sum": 0.0, "fps_n": 0,
                    "gpu_peak": None, "cpu_peak": None, "low1": None}
                self.summary = None  # a new game replaces the last summary
            if game["active"] and game["fps"]:
                s["fps_sum"] += game["fps"]
                s["fps_n"] += 1
            s["gpu_peak"] = peak(s["gpu_peak"], gpu["temp"])
            s["cpu_peak"] = peak(s["cpu_peak"], cpu["temp"])
            if game.get("low1") is not None:
                s["low1"] = game["low1"]
            game["session_s"] = round(now - s["start"])
        for pid in list(self.sessions):
            if pick and pid == pick["pid"]:
                continue
            if not process_alive(pid):  # the game was closed
                self.end_session(self.sessions.pop(pid), now)
        if self.summary and now - self.summary["ended_at"] > 90:
            self.summary = None

    def end_session(self, s, now):
        length = now - s["start"]
        if length < self.cfg.get("summary_min_s", 60):
            return
        self.summary = {
            "id": int(s["start"] * 1000) % 1_000_000_000,
            "name": s["name"], "duration_s": round(length),
            "avg": s["fps_sum"] / s["fps_n"] if s["fps_n"] else None,
            "low1": s["low1"], "gpu_peak": s["gpu_peak"], "cpu_peak": s["cpu_peak"],
            "ended_at": now,
        }
        if self.recent is not None:
            self.recent.invalidate()  # the game you just played moves to the front

    def rolling_stats(self, fps, window=120):
        now = time.monotonic()
        if fps is not None and fps > 0:
            self.local.append((now, fps))
        while self.local and now - self.local[0][0] > window:
            self.local.popleft()
        vals = [v for _t, v in self.local]
        if not vals:
            return {"min": None, "avg": None, "max": None, "low1": None}
        return {"min": min(vals), "avg": sum(vals) / len(vals), "max": max(vals), "low1": None}

    def run(self):
        period = max(0.2, self.cfg.get("refresh_ms", 500) / 1000.0)
        while True:
            try:
                snap = self.sample()
            except Exception as exc:  # never let one bad read kill the dashboard
                snap = {"ok": False, "error": str(exc)}
            with self.lock:
                self.snapshot = snap
            time.sleep(period)


def peak(old, new):
    if new is None:
        return old
    return new if old is None else max(old, new)


def add_extra(extras, label, value, unit, digits):
    if value is None:
        return
    extras.append({"label": label, "value": round(value, digits) if digits else round(value),
                   "unit": unit})


# --------------------------------------------------------------------------
# Demo data (for previews)
# --------------------------------------------------------------------------
def demo_snapshot(s):
    t = time.monotonic()
    if not hasattr(s, "_demo"):
        s._demo = {"min": 999, "max": 0, "sum": 0.0, "n": 0}
        demo_bg = APP_DIR / "demo_background.jpg"
        s._demo_bg = s.games._register_image(str(demo_bg)) if demo_bg.exists() else None
    fps = 54 + 6 * math.sin(t / 3) + random.uniform(-3, 3)
    if random.random() < 0.02:
        fps -= 20
    d = s._demo
    d["min"], d["max"] = min(d["min"], fps), max(d["max"], fps)
    d["sum"] += fps
    d["n"] += 1
    mode = s.demo
    no_game = mode in ("idle", "summary")
    hot = mode == "hot"
    started = d.setdefault("started", t - 5040)
    return {
        "ok": True,
        "sources": {"afterburner": True, "rivatuner": True, "hwinfo": False},
        "gpu": {"name": "RTX 5070 Ti", "usage": 5 if no_game else 96 + random.uniform(-2, 2),
                "temp": 44 if no_game else (79 if hot else 64),
                "extras": [{"label": "Clock", "value": 2865, "unit": "MHz"},
                           {"label": "Power", "value": 258 + random.randint(-6, 6), "unit": "W"},
                           {"label": "VRAM", "value": 9.4, "unit": "GB"}]},
        "cpu": {"name": "Ryzen 7 9700X", "usage": 8 if no_game else 41 + random.uniform(-4, 4),
                "temp": 49 if no_game else (91 if hot else 66),
                "extras": [{"label": "Clock", "value": 5410, "unit": "MHz"},
                           {"label": "Power", "value": 71, "unit": "W"},
                           {"label": "RAM", "value": 14.2, "unit": "GB"}]},
        "game": None if no_game else {
            "name": "The Witcher 3: Wild Hunt", "exe": "witcher3.exe", "active": True,
            "fps": fps, "frametime": 1000 / fps, "min": 45.0, "avg": d["sum"] / d["n"],
            "max": 71.0, "low1": 46.0, "stats": "rivatuner", "bg": s._demo_bg,
            "session_s": round(t - started)},
        "summary": {"id": 1, "name": "The Witcher 3: Wild Hunt", "duration_s": 5040, "avg": 53.2,
                    "low1": 46.0, "gpu_peak": 71.0, "cpu_peak": 78.0, "ended_at": t}
        if mode == "summary" else None,
        "bg": None if mode == "idle" else s._demo_bg,
        "temp_warm": 75, "temp_hot": 87, "refresh_ms": 500,
        "media": {"available": True, "title": "Song title", "artist": "Artist name",
                  "album": "", "app": "Spotify", "playing": True,
                  "position_s": 108 + (t % 60), "duration_s": 232, "art": None},
        "audio": {"available": True, "volume": 62, "muted": False, "output": "a",
                  "output_name": "Headphones", "mic": True, "mic_muted": False,
                  "outputs": [{"id": "a", "name": "Headphones", "detail": "Arctis Nova 7",
                               "kind": "headphones", "active": True},
                              {"id": "b", "name": "Speakers", "detail": "Realtek(R) Audio",
                               "kind": "speakers", "active": False}]},
        "recent": [{"appid": "292030", "name": "The Witcher 3: Wild Hunt", "art": s._demo_bg},
                   {"appid": "1", "name": "Recent game", "art": None},
                   {"appid": "2", "name": "Another game", "art": None},
                   {"appid": "3", "name": "One more game", "art": None}] if mode == "idle" else [],
    }


# --------------------------------------------------------------------------
# Local web server
# --------------------------------------------------------------------------
class Actions:
    """What the dashboard's buttons and the tray menu do."""

    def __init__(self, cfg, sampler, url):
        self.cfg = cfg
        self.sampler = sampler
        self.url = url
        self.server = None
        self.tray = None

    def handle(self, path, body):
        s = self.sampler
        if path == "/api/reset":
            return self.reset()
        if path == "/api/media":
            return bool(s.media and s.media.command(str(body.get("action"))))
        if path == "/api/volume":
            if not s.audio:
                return False
            if "muted" in body:
                s.audio.set_muted(bool(body["muted"]))
            if "level" in body:
                s.audio.set_volume(float(body["level"]))
            return True
        if path == "/api/output":
            if not s.audio:
                return False
            s.audio.set_output(str(body.get("id")))
            return True
        if path == "/api/mic":
            if not s.audio:
                return False
            s.audio.set_mic_muted(bool(body.get("muted")))
            return True
        if path == "/api/launch":
            return bool(s.recent and s.recent.launch(str(body.get("appid"))))
        if path == "/api/refocus":
            if not self.cfg.get("give_focus_back", True) or self.sampler.demo:
                return False
            return focus_guard.refocus()
        if path == "/api/show":
            self.show()
            return True
        if path == "/api/quit":
            self.quit()
            return True
        return None

    def reset(self):
        if self.sampler.demo:
            return True
        return press_keys(self.cfg.get("reset_key", "ctrl+v"))

    def show(self):
        hwnd = find_dashboard_window()
        if hwnd:
            screen = pick_screen(self.cfg, list_screens())
            u32.ShowWindow(hwnd, 8)  # SW_SHOWNA: show without stealing focus
            if screen:
                x, y, w, h, _ = screen
                u32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)
                u32.ShowWindow(hwnd, 3)
        else:
            open_dashboard(self.cfg, self.url)

    def quit(self):
        def stop():
            time.sleep(0.2)
            hwnd = find_dashboard_window()
            if hwnd:
                u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: close the dashboard window
            if self.tray is not None:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            if self.server is not None:
                self.server.shutdown()
        threading.Thread(target=stop, daemon=True).start()


def make_handler(sampler, actions, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code, body, ctype, cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            # Only answer this PC's own dashboard window, never a web page pretending to be it.
            return (self.headers.get("Host") or "").lower() in hosts

        def do_GET(self):
            if not self._local():
                return self._send(403, b"", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/api/stats":
                sampler.last_request = time.monotonic()
                sampler.served_once = True
                with sampler.lock:
                    body = json.dumps(sampler.snapshot).encode()
                return self._send(200, body, "application/json")
            if path.startswith("/bg/"):
                data = sampler.games.image(path[4:])
                if not data:
                    return self._send(404, b"", "text/plain")
                return self._send(200, data, sniff_image(data), cache=True)
            if path == "/media/art":
                data = sampler.media.art_bytes() if sampler.media else None
                if not data:
                    return self._send(404, b"", "text/plain")
                return self._send(200, data, sniff_image(data), cache=True)
            if path in ("/", "/index.html"):
                return self._send(200, (WEB_DIR / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            return self._send(404, b"", "text/plain")

        def do_POST(self):
            # The custom header means other websites can't trigger these (browsers block it).
            if not self._local() or self.headers.get("X-Dashboard") != "1":
                return self._send(403, b"", "text/plain")
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    body = {}
            except Exception:
                return self._send(400, b"", "text/plain")
            path = self.path.split("?", 1)[0]
            try:
                result = actions.handle(path, body)
            except Exception:
                result = False
            if result is None:
                return self._send(404, b"", "text/plain")
            return self._send(200, json.dumps({"ok": bool(result)}).encode(), "application/json")

    return Handler


def sniff_image(data):
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"BM":
        return "image/bmp"
    return "image/jpeg"


# --------------------------------------------------------------------------
# Opening the dashboard on the small screen
# --------------------------------------------------------------------------
def list_screens():
    """[(left, top, width, height, is_primary)] in physical pixels."""
    if not IS_WIN:
        return []
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            u32.SetProcessDPIAware()
        except Exception:
            pass

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    screens = []
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.POINTER(RECT), ctypes.c_void_p)

    def cb(hmon, _hdc, _rect, _data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if u32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            screens.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                            bool(mi.dwFlags & 1)))
        return 1

    u32.EnumDisplayMonitors(None, None, proc_t(cb), None)
    return screens


def pick_screen(cfg, screens):
    if not screens:
        return None
    choice = cfg.get("screen", "auto")
    if isinstance(choice, int) or (isinstance(choice, str) and choice.isdigit()):
        i = int(choice) - 1
        if 0 <= i < len(screens):
            return screens[i]
    others = [s for s in screens if not s[4]]
    if not others:
        return screens[0]
    return min(others, key=lambda s: s[2] * s[3])


def find_browser():
    if not IS_WIN:
        return None
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    for p in (os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
              os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
              os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
              os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
              os.path.join(local, r"Google\Chrome\Application\chrome.exe")):
        if os.path.isfile(p):
            return p
    return None


def open_dashboard(cfg, url):
    browser = find_browser()
    screen = pick_screen(cfg, list_screens())
    if not browser:
        import webbrowser
        webbrowser.open(url)
        return
    profile = os.path.join(os.environ.get("LOCALAPPDATA", str(APP_DIR)),
                           "PC-Stats-Dashboard", "browser")
    args = [browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
            "--disable-extensions", "--disable-sync", "--disable-default-apps",
            "--disable-features=Translate,msEdgeSidebarV2,HubApps,EdgeCollections"]
    if screen:
        x, y, w, h, _primary = screen
        args += [f"--window-position={x},{y}", f"--window-size={w},{h}"]
    subprocess.Popen(args, close_fds=True)
    start_window_keeper(cfg, screen)


def close_dashboard_windows():
    """Close any dashboard windows already open. Returns how many there were."""
    closed = 0
    for _ in range(5):
        hwnd = find_dashboard_window()
        if not hwnd:
            break
        u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        closed += 1
        time.sleep(0.4)
    return closed


def find_dashboard_window():
    """The dashboard's Edge/Chrome window, or None."""
    if not IS_WIN:
        return None
    found = []

    def cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(256)
        u32.InternalGetWindowText(hwnd, buf, 256)
        if buf.value == WINDOW_TITLE:
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = ntpath.basename(process_path(pid.value) or "").lower()
            if exe in ("msedge.exe", "chrome.exe"):
                found.append((0 if u32.IsWindowVisible(hwnd) else 1, hwnd))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return min(found)[1] if found else None


def apply_window_styles(hwnd, cfg):
    """Keep the window out of the taskbar / Alt+Tab, and stop taps taking focus from the game."""
    ex = u32_get_exstyle(hwnd)
    want = ex
    if cfg.get("hide_from_taskbar", True):
        want = (want | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    if cfg.get("keep_game_focused", True):
        want |= WS_EX_NOACTIVATE
    if want == ex:
        return
    u32_set_exstyle(hwnd, want)
    if (ex ^ want) & WS_EX_TOOLWINDOW:
        u32.ShowWindow(hwnd, 0)  # hide and show again so the taskbar drops its button
        u32.ShowWindow(hwnd, 8)
    u32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)


def snap_to_screen(hwnd, screen, cfg):
    """Put the window exactly on the small screen (DPI scaling can push it off)."""
    x, y, w, h, _ = screen
    u32.ShowWindow(hwnd, 4)  # restore without activating, so it can be moved
    u32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
    if not cfg.get("keep_game_focused", True):
        u32.ShowWindow(hwnd, 3)  # maximise (this one does take focus)


class FocusGuard:
    """
    Windows turns a tap on the touchscreen into a mouse click: the pointer jumps to the
    small screen and the dashboard takes focus, which pauses many games. After each tap
    the page asks for focus (and the pointer) to go back to whatever you were using.
    """

    def __init__(self):
        self.screen = None      # (x, y, w, h, primary) of the dashboard's screen
        self.dash_hwnd = None
        self.last_hwnd = None   # the last window in focus that wasn't the dashboard
        self.last_pt = None     # the last pointer position that wasn't on the small screen
        self.started = False

    def start(self, screen):
        self.screen = screen
        if IS_WIN and not self.started:
            self.started = True
            threading.Thread(target=self._watch, name="focus", daemon=True).start()

    def _on_small_screen(self, x, y):
        if not self.screen:
            return False
        sx, sy, w, h, _ = self.screen
        return sx <= x < sx + w and sy <= y < sy + h

    def _is_dashboard(self, hwnd):
        if not hwnd:
            return False
        if hwnd == self.dash_hwnd:
            return True
        buf = ctypes.create_unicode_buffer(64)
        u32.InternalGetWindowText(hwnd, buf, 64)
        return buf.value == WINDOW_TITLE

    def _watch(self):
        pt = wintypes.POINT()
        while True:
            try:
                fg = u32.GetForegroundWindow()
                if fg and not self._is_dashboard(fg):
                    self.last_hwnd = fg
                if u32.GetCursorPos(ctypes.byref(pt)) and not self._on_small_screen(pt.x, pt.y):
                    self.last_pt = (pt.x, pt.y)
            except Exception:
                pass
            time.sleep(0.25)

    def refocus(self):
        if not IS_WIN:
            return False
        pt = wintypes.POINT()
        if self.last_pt and u32.GetCursorPos(ctypes.byref(pt)) and self._on_small_screen(pt.x, pt.y):
            u32.SetCursorPos(*self.last_pt)  # put the pointer back where it was
        fg = u32.GetForegroundWindow()
        target = self.last_hwnd
        if not self._is_dashboard(fg) or not target or not u32.IsWindow(target):
            return False
        me = k32.GetCurrentThreadId()
        fg_thread = u32.GetWindowThreadProcessId(fg, None)
        attached = bool(fg_thread and fg_thread != me and u32.AttachThreadInput(me, fg_thread, True))
        try:
            if u32.IsIconic(target):
                u32.ShowWindow(target, 9)  # SW_RESTORE: a fullscreen game that minimised
            ok = u32.SetForegroundWindow(target)
            u32.BringWindowToTop(target)
        finally:
            if attached:
                u32.AttachThreadInput(me, fg_thread, False)
        if not ok:
            u32.SwitchToThisWindow(target, True)
        return True


focus_guard = FocusGuard()

_keeper = {"started": False}


def start_window_keeper(cfg, screen):
    """Watches the dashboard window: places it once, then keeps its taskbar settings."""
    if not IS_WIN or _keeper["started"]:
        return
    _keeper["started"] = True
    if cfg.get("give_focus_back", True):
        focus_guard.start(screen)

    def run():
        last = None
        while True:
            try:
                hwnd = find_dashboard_window()
                focus_guard.dash_hwnd = hwnd
                if hwnd and hwnd != last:
                    if screen:
                        snap_to_screen(hwnd, screen, cfg)
                    last = hwnd
                if hwnd:
                    apply_window_styles(hwnd, cfg)
            except Exception:
                pass
            time.sleep(0.3 if last is None else 2.0)

    threading.Thread(target=run, name="window", daemon=True).start()


# ---- the Reset button: press your RivaTuner key ----------------------------
VK_NAMES = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
            "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "insert": 0x2D,
            "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pause": 0x13,
            "scrolllock": 0x91, "printscreen": 0x2C}
EXTENDED_KEYS = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}


def parse_combo(combo):
    """'ctrl+v' -> [0x11, 0x56]; None if a part isn't understood."""
    vks = []
    for p in [p.strip().lower() for p in str(combo).split("+") if p.strip()]:
        if p in VK_NAMES:
            vks.append(VK_NAMES[p])
        elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", p):
            vks.append(0x6F + int(p[1:]))
        elif re.fullmatch(r"num(pad)?\s*\d", p):
            vks.append(0x60 + int(p[-1]))
        elif len(p) == 1 and p.isalnum():
            vks.append(ord(p.upper()))
        else:
            return None
    return vks or None


def press_keys(combo):
    if not IS_WIN:
        return False
    vks = parse_combo(combo)
    if not vks:
        return False

    def send(vk, up):
        inp = INPUT()
        inp.type = 1  # keyboard
        flags = (0x0002 if up else 0) | (0x0001 if vk in EXTENDED_KEYS else 0)
        inp.ki = KEYBDINPUT(vk, u32.MapVirtualKeyW(vk, 0), flags, 0, 0)
        u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    for vk in vks:
        send(vk, False)
        time.sleep(0.02)
    time.sleep(0.12)  # held long enough for RivaTuner to notice
    for vk in reversed(vks):
        send(vk, True)
        time.sleep(0.02)
    return True


# --------------------------------------------------------------------------
# Diagnose
# --------------------------------------------------------------------------
def diagnose(cfg):
    print("PC Stats Dashboard - what it can see\n")
    ab_raw = read_afterburner()
    if not ab_raw:
        print("MSI Afterburner: NOT FOUND - start Afterburner (it must be running).")
    else:
        ab = AfterburnerView(ab_raw)
        print(f"MSI Afterburner: OK, {len(ab_raw['entries'])} readings")
        for g in ab_raw["gpus"]:
            mark = "  <- used" if g["index"] == ab.gpu else ""
            print(f"  GPU {g['index']}: {g['device'] or g['family']}{mark}")
        for e in ab_raw["entries"]:
            v = "n/a" if e["value"] is None else f"{e['value']:.1f}"
            gpu = "" if e["gpu"] > 64 else f" [GPU {e['gpu']}]"
            print(f"    {e['name']}{gpu}: {v} {e['units']}")
        def present(n):
            return any(norm(e["name"]) == norm(n) or norm_gpu(e["name"]) == norm_gpu(n)
                       for e in ab_raw["entries"])
        wanted = ("GPU usage", "GPU temperature", "Core clock", "Memory usage", "CPU usage",
                  "CPU temperature", "Framerate", "Framerate Min", "Framerate Avg",
                  "Framerate Max", "Framerate 1% Low")
        missing = [n for n in wanted if not present(n)]
        if missing:
            print("  Not enabled in Afterburner's Monitoring tab: " + ", ".join(missing))
        if not missing or any(n.startswith("Framerate") for n in wanted if n not in missing):
            print("  (Framerate readings show n/a until a game is running - that's normal.)")
        gpu_names = {"GPU usage", "GPU temperature", "Core clock", "Memory usage"}
        used = {n: ab.value([n], ab.gpu if n in gpu_names else None) for n in wanted}
        print("  Dashboard reads: " + ", ".join(
            f"{n} {v:.0f}" for n, v in used.items() if v is not None))
    rtss = read_rtss()
    if not rtss:
        print("\nRivaTuner: NOT FOUND - start RivaTuner Statistics Server.")
    else:
        print(f"\nRivaTuner: OK, {len(rtss['apps'])} programs hooked")
        for a in rtss["apps"]:
            fps = "-" if a["fps"] is None else f"{a['fps']:.0f} fps"
            tag = " (ignored)" if a["exe"] in BUILTIN_IGNORE else ""
            print(f"    {a['exe']}  {fps}{tag}")
    hw = read_hwinfo()
    print("\nHWiNFO shared memory: " + ("OK" if hw else "not running (optional)"))
    screens = list_screens()
    target = pick_screen(cfg, screens)
    print("\nScreens:")
    for i, s in enumerate(screens, 1):
        tags = (" main" if s[4] else "") + ("  <- dashboard goes here" if s == target else "")
        print(f"    {i}: {s[2]}x{s[3]} at ({s[0]},{s[1]}){tags}")
    print(f"\nBrowser: {find_browser() or 'not found (will use your default browser)'}")

    print("\nAdd-ons:")
    for label, mod in (("Music (now playing)", dash_media), ("Sound and mic", dash_audio),
                       ("Tray icon", dash_tray)):
        if mod is None:
            print(f"    {label}: file missing")
        elif mod.AVAILABLE:
            print(f"    {label}: OK")
        else:
            print(f"    {label}: not installed - run Start Dashboard once to install it "
                  f"({mod.IMPORT_ERROR})")
    if dash_media is not None and dash_media.AVAILABLE:
        m = dash_media.Media()
        time.sleep(2.5)
        snap = m.snapshot()
        playing = snap.get("title")
        print("    Now playing: " + (f"{playing} - {snap.get('artist')} ({snap.get('app')})"
                                     if playing else f"nothing{' (' + m.error + ')' if m.error else ''}"))
    if dash_audio is not None and dash_audio.AVAILABLE:
        a = dash_audio.Audio()
        time.sleep(1.5)
        snap = a.snapshot()
        outs = ", ".join(o["name"] + (" [on]" if o.get("active") else "") for o in snap.get("outputs", []))
        print(f"    Volume: {snap.get('volume')}%  Outputs: {outs or '-'}  "
              f"Mic: {'muted' if snap.get('mic_muted') else ('on' if snap.get('mic') else 'none')}"
              + (f"  ({a.error})" if a.error else ""))
    if dash_steam is not None:
        r = dash_steam.RecentGames(GameInfo(cfg), int(cfg.get("recent_games", 4) or 4),
                                   download=False)
        r._refresh()
        print("    Recent Steam games: " + (", ".join(i["name"] for i in r.items) or "none found"))
    keys = parse_combo(cfg.get("reset_key", "ctrl+v"))
    print(f"\nReset button presses: {cfg.get('reset_key')}" + ("" if keys else "  (not understood - check config.json)"))


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="PC Stats Dashboard")
    ap.add_argument("--demo", nargs="?", const="game", choices=["game", "idle", "summary", "hot"],
                    help="show fake data (game, idle, summary or hot)")
    ap.add_argument("--diagnose", action="store_true", help="print what the app can see")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard window")
    ap.add_argument("--port", type=int, help="override the port")
    args = ap.parse_args()

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    if args.diagnose:
        diagnose(cfg)
        return

    url = f"http://127.0.0.1:{cfg['port']}/"
    sampler = Sampler(cfg, demo=args.demo)
    actions = Actions(cfg, sampler, url)
    handler = make_handler(sampler, actions, cfg["port"])
    server = None
    for attempt in range(3):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), handler)
            break
        except OSError:
            if attempt == 0 and ask_running_instance(url, "show"):
                return  # this version is already running: it brought its window back
            # An older version may still be running: ask it to stop, then take over.
            try:
                urllib.request.urlopen(url + "api/quit", timeout=3).close()
            except Exception:
                pass
            time.sleep(2)
    if server is None:
        print(f"Port {cfg['port']} is in use by another program. Change \"port\" in config.json.")
        return
    server.daemon_threads = True
    actions.server = server
    if not args.no_browser and close_dashboard_windows():
        time.sleep(1.5)  # a window left over from before: start with a fresh one

    if not args.demo:
        if dash_media is not None and dash_media.AVAILABLE:
            sampler.media = dash_media.Media()
        if dash_audio is not None and dash_audio.AVAILABLE:
            sampler.audio = dash_audio.Audio()
        if dash_steam is not None and cfg.get("recent_games", 4):
            sampler.recent = dash_steam.RecentGames(sampler.games, int(cfg.get("recent_games", 4)),
                                                    cfg.get("download_steam_art", True))
    threading.Thread(target=sampler.run, name="sampler", daemon=True).start()

    if not args.no_browser:
        threading.Timer(0.3, open_dashboard, args=(cfg, url)).start()
        if IS_WIN and dash_tray is not None:
            actions.tray = dash_tray.start(actions.show, actions.reset, actions.quit)

    limit = cfg.get("exit_when_closed_s", 120)
    if limit and not args.no_browser and actions.tray is None:
        def watchdog():
            while True:
                time.sleep(5)
                if sampler.served_once and time.monotonic() - sampler.last_request > limit:
                    server.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()

    print(f"Dashboard running at {url}")
    server.serve_forever()


def ask_running_instance(url, action):
    try:
        req = urllib.request.Request(url + f"api/{action}", data=b"{}", method="POST",
                                     headers={"X-Dashboard": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    main()
