"""
The dashboard's icon in the Windows tray, next to Afterburner's.

Double-click it (or choose Show dashboard) to bring the window back,
Reset stats does the same as your RivaTuner key, and Stop dashboard quits.
"""

import threading

try:
    import pystray
    from PIL import Image, ImageDraw
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as exc:
    AVAILABLE = False
    IMPORT_ERROR = exc


def _icon_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(16, 22, 30, 255))
    # three bars: GPU green, CPU red, FPS lavender
    d.rounded_rectangle((13, 32, 23, 52), radius=3, fill=(118, 185, 0, 255))
    d.rounded_rectangle((27, 22, 37, 52), radius=3, fill=(220, 24, 40, 255))
    d.rounded_rectangle((41, 12, 51, 52), radius=3, fill=(201, 184, 255, 255))
    return img


def start(on_show, on_reset, on_quit):
    """Start the tray icon on its own thread. Returns the icon, or None if unavailable."""
    if not AVAILABLE:
        return None

    def show(icon, item):
        on_show()

    def reset(icon, item):
        on_reset()

    def quit_(icon, item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Show dashboard", show, default=True),
        pystray.MenuItem("Reset stats", reset),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop dashboard", quit_),
    )
    icon = pystray.Icon("pc-stats-dashboard", _icon_image(), "PC Stats Dashboard", menu)
    threading.Thread(target=icon.run, name="tray", daemon=True).start()
    return icon
