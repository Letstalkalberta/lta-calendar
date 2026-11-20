"""LTA Watcher: tray icon + window. Entry point for the Windows app.

  "LTA Watcher.exe"                 open the window
  "LTA Watcher.exe" --background    start in the tray only (used at Windows sign-in)
  python -m lta_watcher --headless  no tray or window: open the screen in the web browser (testing, Linux)
"""
import os, pathlib, socket, sys, threading, urllib.request, webbrowser
from . import APP_NAME, VERSION
from . import core

LOCK_PORT = int(os.environ.get("LTA_LOCK_PORT", "47862"))  # one copy per Windows user

def already_running():
    s = socket.socket()
    try: s.bind(("127.0.0.1", LOCK_PORT)); s.listen(1); return s  # keep the socket open for our lifetime
    except OSError:
        try:
            port = (core.APPDIR / "ui_port").read_text().strip()
            urllib.request.urlopen(f"http://127.0.0.1:{port}/show", timeout=3)
        except Exception: pass
        return None

def icon_image():
    from PIL import Image
    p = core.UI_DIR / "icon.png"
    return Image.open(p) if p.exists() else Image.new("RGB", (64, 64), (11, 44, 110))

def main():
    args = sys.argv[1:]
    lock = already_running()
    if lock is None: return
    settings = core.Settings(); hub = core.Hub(settings)
    state = {"window": None, "icon": None}

    def notify(title, msg):
        ic = state["icon"]
        if ic is not None:
            try: ic.notify(msg, title)
            except Exception: pass

    worker = core.Worker(settings, hub, notify)
    def show():
        w = state["window"]
        if w is not None: w.show(); w.restore()
        elif "--headless" in args: webbrowser.open(local.url)
    def quit_app():
        worker.stop_flag = True
        if state["icon"] is not None: state["icon"].stop()
        if state["window"] is not None: state["window"].destroy()
        os._exit(0)
    local = core.Local(settings, hub, worker, on_show=show, on_quit=quit_app)
    worker.start()
    core.log.info("%s %s started; screen at %s", APP_NAME, VERSION, local.url)

    if "--headless" in args:
        if sys.stdout: print(f"Open {local.url}", flush=True)
        if "--no-browser" not in args: webbrowser.open(local.url)
        threading.Event().wait()
        return

    import pystray, webview
    def toggle_pause(icon, item): settings.set(paused=not settings.get("paused")); worker.check_now()
    menu = pystray.Menu(
        pystray.MenuItem("Open LTA Watcher", lambda i, it: show(), default=True),
        pystray.MenuItem("Check now", lambda i, it: worker.check_now()),
        pystray.MenuItem("Pause watching", toggle_pause, checked=lambda it: bool(settings.get("paused"))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda i, it: quit_app()))
    icon = pystray.Icon("lta-watcher", icon_image(), f"{APP_NAME} {VERSION}", menu)
    state["icon"] = icon
    icon.run_detached()

    window = webview.create_window(APP_NAME, local.url, width=1120, height=780, min_size=(820, 560), hidden="--background" in args)
    state["window"] = window
    def on_closing():  # closing the window keeps the app running in the tray
        window.hide()
        notify(APP_NAME, "Still watching in the background. Right-click the tray icon to quit.")
        return False
    window.events.closing += on_closing
    webview.start(private_mode=False)

if __name__ == "__main__":
    main()
