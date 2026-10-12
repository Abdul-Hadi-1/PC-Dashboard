"""
Volume, sound output and microphone mute, through Windows' own audio settings.

Everything runs on one background thread (Windows audio needs that), checks
the current state once a second, and carries out taps from the dashboard.
Muting the mic here mutes it for every app, Discord included.
"""

import queue
import threading
import time

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as exc:  # not installed, or not on Windows
    AVAILABLE = False
    IMPORT_ERROR = exc

E_RENDER, E_CAPTURE = 0, 1
ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS = 0, 1, 2
DEVICE_STATE_ACTIVE = 1
REFRESH_S = 1.0
LIST_REFRESH_S = 5.0
MAX_OUTPUTS = 4

HEADPHONE_WORDS = ("headphone", "headset", "earphone", "earbud", "airpods", "buds", "arctis",
                   "hyperx", "cloud", "astro", "kraken", "blackshark", "g pro", "g733", "g535",
                   "virtuoso", "void", "nova", "corsair hs", "steelseries")


def split_name(friendly):
    """'Headphones (Arctis Nova 7)' -> ('Headphones', 'Arctis Nova 7')."""
    friendly = friendly or "Audio device"
    if friendly.endswith(")") and " (" in friendly:
        main, _, rest = friendly.partition(" (")
        return main.strip(), rest[:-1].strip()
    return friendly.strip(), ""


def device_kind(friendly):
    low = (friendly or "").lower()
    return "headphones" if any(w in low for w in HEADPHONE_WORDS) else "speakers"


class Audio:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"available": AVAILABLE}
        self.error = None if AVAILABLE else str(IMPORT_ERROR)
        self.jobs = queue.Queue()
        if AVAILABLE:
            threading.Thread(target=self._run, name="audio", daemon=True).start()

    # ---- public --------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def set_volume(self, percent):
        self.jobs.put(("volume", max(0.0, min(100.0, float(percent)))))

    def set_muted(self, muted):
        self.jobs.put(("mute", bool(muted)))

    def set_output(self, device_id):
        self.jobs.put(("output", str(device_id)))

    def set_mic_muted(self, muted):
        self.jobs.put(("mic", bool(muted)))

    # ---- background thread -------------------------------------------------------
    def _run(self):
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        self.outputs = []
        self.outputs_at = 0.0
        while True:
            try:
                self._refresh()
            except Exception as exc:
                self.error = str(exc)
            try:
                job = self.jobs.get(timeout=REFRESH_S)
            except queue.Empty:
                continue
            # Volume drags send many updates: only the latest one matters.
            latest = {job[0]: job}
            while True:
                try:
                    j = self.jobs.get_nowait()
                except queue.Empty:
                    break
                latest[j[0]] = j
            for kind, value in latest.values():
                try:
                    self._do(kind, value)
                except Exception as exc:
                    self.error = str(exc)
                if kind == "output":
                    self.outputs_at = 0.0  # re-read the list straight away

    def _enum(self):
        if getattr(self, "_enumerator", None) is None:
            self._enumerator = AudioUtilities.GetDeviceEnumerator()
        return self._enumerator

    @staticmethod
    def _volume_of(dev):
        iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        return iface.QueryInterface(IAudioEndpointVolume)

    def _default(self, flow, role):
        try:
            return self._enum().GetDefaultAudioEndpoint(flow, role)
        except Exception:
            return None  # e.g. no microphone plugged in

    def _refresh(self):
        enum = self._enum()
        now = time.monotonic()
        if now - self.outputs_at > LIST_REFRESH_S:
            outs = []
            col = enum.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
            for i in range(col.GetCount()):
                d = AudioUtilities.CreateDevice(col.Item(i))
                if d is None:
                    continue
                main, sub = split_name(d.FriendlyName)
                outs.append({"id": d.id, "name": main, "detail": sub,
                             "kind": device_kind(d.FriendlyName)})
            self.outputs = outs[:MAX_OUTPUTS]
            self.outputs_at = now

        speakers = self._default(E_RENDER, ROLE_MULTIMEDIA)
        out_id, volume, muted, out_name = None, None, None, None
        if speakers is not None:
            out_id = speakers.GetId()
            vol = self._volume_of(speakers)
            volume = round(vol.GetMasterVolumeLevelScalar() * 100)
            muted = bool(vol.GetMute())
            for o in self.outputs:
                if o["id"] == out_id:
                    out_name = o["name"]

        mic = self._default(E_CAPTURE, ROLE_COMMUNICATIONS) or self._default(E_CAPTURE, ROLE_CONSOLE)
        mic_muted = None
        if mic is not None:
            mic_muted = bool(self._volume_of(mic).GetMute())

        with self.lock:
            self.state = {
                "available": True,
                "volume": volume,
                "muted": muted,
                "output": out_id,
                "output_name": out_name,
                "outputs": [dict(o, active=o["id"] == out_id) for o in self.outputs],
                "mic": mic is not None,
                "mic_muted": mic_muted,
            }

    def _do(self, kind, value):
        if kind in ("volume", "mute"):
            dev = self._default(E_RENDER, ROLE_MULTIMEDIA)
            if dev is None:
                return
            vol = self._volume_of(dev)
            if kind == "volume":
                vol.SetMasterVolumeLevelScalar(value / 100.0, None)
                if value > 0 and vol.GetMute():
                    vol.SetMute(0, None)
            else:
                vol.SetMute(1 if value else 0, None)
        elif kind == "mic":
            seen = set()
            for role in (ROLE_COMMUNICATIONS, ROLE_CONSOLE):
                dev = self._default(E_CAPTURE, role)
                if dev is None:
                    continue
                dev_id = dev.GetId()
                if dev_id in seen:
                    continue
                seen.add(dev_id)
                self._volume_of(dev).SetMute(1 if value else 0, None)
        elif kind == "output":
            if value not in {o["id"] for o in self.outputs}:
                return
            self._set_default_output(value)

    @staticmethod
    def _set_default_output(device_id):
        try:
            from pycaw.constants import ERole
            AudioUtilities.SetDefaultDevice(
                device_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
            return
        except (ImportError, AttributeError):
            pass
        # Older pycaw: talk to Windows' policy interface directly.
        from pycaw.api.policyconfig import IPolicyConfig
        from pycaw.constants import CLSID_CPolicyConfigClient
        policy = comtypes.CoCreateInstance(CLSID_CPolicyConfigClient, IPolicyConfig,
                                           comtypes.CLSCTX_ALL)
        for role in (ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS):
            policy.SetDefaultEndpoint(device_id, role)
