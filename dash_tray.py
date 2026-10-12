"""
The dashboard's icon in the Windows tray, next to Afterburner's.

Double-click it (or choose Show dashboard) to bring the window back. The menu
also has Reset stats, Start with Windows, Check setup and Stop dashboard.
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


def icon_image(size=64):
    """Three bars: GPU green, CPU red, FPS lavender."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0

    def box(x0, y0, x1, y1):
        return (round(x0 * s), round(y0 * s), round(x1 * s), round(y1 * s))

    d.rounded_rectangle(box(2, 2, 61, 61), radius=round(14 * s), fill=(16, 22, 30, 255))
    d.rounded_rectangle(box(13, 32, 23, 52), radius=max(1, round(3 * s)), fill=(118, 185, 0, 255))
    d.rounded_rectangle(box(27, 22, 37, 52), radius=max(1, round(3 * s)), fill=(220, 24, 40, 255))
    d.rounded_rectangle(box(41, 12, 51, 52), radius=max(1, round(3 * s)), fill=(201, 184, 255, 255))
    return img


def start(on_show, on_reset, on_quit, on_check=None, startup_get=None, startup_set=None):
    """Start the tray icon on its own thread. Returns the icon, or None if unavailable."""
    if not AVAILABLE:
        return None

    def show(icon, item):
        on_show()

    def reset(icon, item):
        on_reset()

    def check(icon, item):
        if on_check:
            on_check()

    def toggle_startup(icon, item):
        if startup_get and startup_set:
            startup_set(not startup_get())

    def startup_checked(item):
        try:
            return bool(startup_get and startup_get())
        except Exception:
            return False

    def quit_(icon, item):
        icon.stop()
        on_quit()

    items = [
        pystray.MenuItem("Show dashboard", show, default=True),
        pystray.MenuItem("Reset stats", reset),
        pystray.Menu.SEPARATOR,
    ]
    if startup_get and startup_set:
        items.append(pystray.MenuItem("Start with Windows", toggle_startup, checked=startup_checked))
    if on_check:
        items.append(pystray.MenuItem("Check setup", check))
    items += [pystray.Menu.SEPARATOR, pystray.MenuItem("Stop dashboard", quit_)]

    icon = pystray.Icon("pc-stats-dashboard", icon_image(64), "PC Stats Dashboard",
                        pystray.Menu(*items))
    threading.Thread(target=icon.run, name="tray", daemon=True).start()
    return icon
