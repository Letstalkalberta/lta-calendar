"""LTA Watcher core: settings, hub connection, background worker, and the local screen's API.

Runs the same readers as watcher/watch.py. This PC never holds the AI key: when a watched page
changes, the PC sends the page text to the hub, and the hub runs the AI reader.
"""
import datetime as dt, json, logging, logging.handlers, os, pathlib, secrets, sys, threading, time, urllib.error, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import APP_NAME, VERSION, DEFAULT_HUB

# ------------------------------------------------------------------ paths
if getattr(sys, "frozen", False):
    BUNDLE = pathlib.Path(sys._MEIPASS)
    sys.path.insert(0, str(BUNDLE / "watcher"))
else:
    BUNDLE = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(BUNDLE.parent.parent / "watcher"))
import watch  # noqa: E402  (the shared readers)

UI_DIR = BUNDLE / "ui" if getattr(sys, "frozen", False) else BUNDLE / "ui"
APPDIR = pathlib.Path(os.environ.get("LTA_WATCHER_HOME") or (pathlib.Path(os.environ["APPDATA"]) / APP_NAME if os.name == "nt" else pathlib.Path.home() / ".lta-watcher"))
APPDIR.mkdir(parents=True, exist_ok=True)
CONFIG = APPDIR / "config.json"

log = logging.getLogger("lta")
_h = logging.handlers.RotatingFileHandler(APPDIR / "log.txt", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)
if sys.stderr: log.addHandler(logging.StreamHandler())  # no console in the packaged Windows app
log.setLevel(logging.INFO)

AB = dt.timezone(dt.timedelta(hours=-6))
HEARTBEAT_SEC = int(os.environ.get("LTA_HEARTBEAT_SEC", "300"))

# ------------------------------------------------------------------ settings
class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        try: self.d = json.loads(CONFIG.read_text())
        except (FileNotFoundError, ValueError): self.d = {}
        self.d.setdefault("hub_url", DEFAULT_HUB); self.d.setdefault("paused", False)
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, **kw):
        with self.lock:
            self.d.update(kw)
            tmp = CONFIG.with_suffix(".tmp"); tmp.write_text(json.dumps(self.d, indent=2)); tmp.replace(CONFIG)
            if os.name != "nt": os.chmod(CONFIG, 0o600)
    @property
    def enrolled(self): return bool(self.d.get("node_token"))

# ------------------------------------------------------------------ hub client
class HubError(Exception):
    def __init__(self, msg, code=0): super().__init__(msg); self.code = code

class Hub:
    def __init__(self, settings): self.s = settings
    def call(self, method, path, body=None, auth="node", timeout=60):
        url = self.s.get("hub_url", DEFAULT_HUB).rstrip("/") + path
        headers = {"Content-Type": "application/json", "User-Agent": f"{APP_NAME}/{VERSION}"}
        if auth == "node" and self.s.get("node_token"): headers["X-Node-Token"] = self.s.get("node_token")
        if auth == "user" and (self.s.get("session") or {}).get("token"): headers["Authorization"] = "Bearer " + self.s.get("session")["token"]
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try: msg = json.loads(e.read()).get("error", str(e))
            except ValueError: msg = str(e)
            raise HubError(msg, e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HubError(f"Can't reach the hub ({getattr(e, 'reason', e)}). Check the internet connection.")

# ------------------------------------------------------------------ background worker
class Worker(threading.Thread):
    def __init__(self, settings, hub, notify=None):
        super().__init__(daemon=True)
        self.s, self.hub, self.notify = settings, hub, notify or (lambda t, m: None)
        self.wake = threading.Event(); self.stop_flag = False
        self.state = {"status": "starting", "last_contact": None, "last_error": None, "current": None,
                      "checked_today": 0, "day": None, "pending": None, "latest_version": "", "recent": []}
    def check_now(self): self.wake.set()
    def _note(self, src, outcome):
        self.state["recent"] = ([{"at": dt.datetime.now(AB).strftime("%b %d %I:%M %p"), "source": src, "result": outcome}] + self.state["recent"])[:25]

    def run(self):
        while not self.stop_flag:
            try: self.cycle()
            except Exception as e:
                log.exception("cycle failed"); self.state.update(status="error", last_error=str(e))
            self.wake.wait(HEARTBEAT_SEC); self.wake.clear()

    def cycle(self):
        if not self.s.enrolled: self.state["status"] = "not set up"; return
        if self.s.get("paused"): self.state["status"] = "paused"; return
        today = dt.date.today().isoformat()
        if self.state["day"] != today: self.state.update(day=today, checked_today=0)
        try:
            hb = self.hub.call("POST", "/api/hub/heartbeat", {"version": VERSION, "status": {k: self.state[k] for k in ("status", "checked_today", "last_error")}})
        except HubError as e:
            self.state.update(status="offline", last_error=str(e)); log.warning("heartbeat: %s", e); return
        prev = self.state["pending"]
        self.state.update(status="watching", last_contact=dt.datetime.now(AB).isoformat(timespec="seconds"), last_error=None,
                          pending=hb.get("pending"), latest_version=hb.get("latest_version", ""))
        if prev is not None and (hb.get("pending") or 0) > prev and (self.s.get("session") or {}).get("token"):
            self.notify("New events to review", f"{hb['pending'] - prev} new item(s) waiting in the review queue.")
        for src in hb.get("sources", []):
            if self.stop_flag or self.s.get("paused"): break
            self.state["current"] = src.get("name", src["id"])
            try:
                r = watch.check_source(src, src.get("last_hash"))
                if r["kind"] == "structured":
                    res = self.hub.call("POST", "/api/hub/results", {"source_id": src["id"], "kind": "structured", "events": r["events"]})
                    self._note(src["name"], f"{len(r['events'])} found · {res.get('added', 0)} new")
                elif r["kind"] == "unchanged":
                    self.hub.call("POST", "/api/hub/results", {"source_id": src["id"], "kind": "unchanged", "hash": r["hash"]})
                    self._note(src["name"], "no change")
                else:
                    res = self.hub.call("POST", "/api/hub/results", {"source_id": src["id"], "kind": "ai", "hash": r["hash"], "text": r["text"]}, timeout=180)
                    self._note(src["name"], res.get("note") or f"page changed · {res.get('queued', 0)} sent to review")
                self.state["checked_today"] += 1
            except HubError as e:
                self._note(src["name"], f"hub error: {e}"); log.warning("%s: %s", src["id"], e)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self._note(src["name"], "couldn't read site"); log.warning("%s: %s", src["id"], msg)
                try: self.hub.call("POST", "/api/hub/results", {"source_id": src["id"], "kind": "error", "error": msg})
                except HubError: pass
        self.state["current"] = None

# ------------------------------------------------------------------ local screen API (127.0.0.1 only)
class Local:
    def __init__(self, settings, hub, worker, on_show=None, on_quit=None):
        self.s, self.hub, self.w = settings, hub, worker
        self.key = secrets.token_urlsafe(24)
        self.on_show, self.on_quit = on_show or (lambda: None), on_quit or (lambda: None)
        me = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def out(self, code, obj, ctype="application/json; charset=utf-8"):
                b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def host_ok(self):  # blocks DNS-rebinding: only our own address may talk to this screen
                return self.headers.get("Host", "") in (f"127.0.0.1:{me.port}", f"localhost:{me.port}")
            def do_GET(self):
                if not self.host_ok(): return self.out(403, {"error": "forbidden"})
                p = urlparse(self.path).path
                if p in ("/", "/index.html"):
                    html = (UI_DIR / "index.html").read_text(encoding="utf-8").replace("__LOCAL_KEY__", me.key).replace("__VERSION__", VERSION)
                    return self.out(200, html.encode(), "text/html; charset=utf-8")
                if p == "/logo.png" and (UI_DIR / "logo.png").exists(): return self.out(200, (UI_DIR / "logo.png").read_bytes(), "image/png")
                if p == "/show": me.on_show(); return self.out(200, {"ok": True})  # second launch asks us to show the window
                self.out(404, {"error": "not found"})
            def do_POST(self):
                if not self.host_ok() or self.headers.get("X-Local-Key") != me.key: return self.out(403, {"error": "forbidden"})
                n = int(self.headers.get("Content-Length", "0") or 0)
                try: body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError: return self.out(400, {"error": "bad request"})
                try: self.out(200, me.dispatch(urlparse(self.path).path, body))
                except HubError as e: self.out(400 if e.code != 401 else 401, {"error": str(e)})
                except ValueError as e: self.out(400, {"error": str(e)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        (APPDIR / "ui_port").write_text(str(self.port))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self): return f"http://127.0.0.1:{self.port}/"

    def dispatch(self, p, b):
        s = self.s
        if p == "/local/state":
            sess = s.get("session") or {}
            from . import autostart
            return {"version": VERSION, "enrolled": s.enrolled, "pc_name": s.get("pc_name"), "node_id": s.get("node_id"), "hub_url": s.get("hub_url"),
                    "paused": s.get("paused"), "user": {"name": sess.get("name"), "role": sess.get("role"), "must_change": sess.get("must_change")} if sess.get("token") else None,
                    "worker": self.w.state, "autostart": autostart.is_enabled(), "logfile": str(APPDIR / "log.txt")}
        if p == "/local/setup":
            hub_url = str(b.get("hub_url") or DEFAULT_HUB).strip().rstrip("/")
            if not hub_url.startswith("https://") and not hub_url.startswith("http://127.0.0.1") and not hub_url.startswith("http://localhost"):
                raise ValueError("The hub address must start with https://")
            s.set(hub_url=hub_url)
            r = self.hub.call("POST", "/api/hub/enroll", {"code": b.get("code", ""), "name": b.get("pc_name", "")}, auth=None)
            s.set(node_id=r["node_id"], node_token=r["node_token"], pc_name=b.get("pc_name", ""))
            log.info("enrolled as %s", r["node_id"]); self.w.check_now()
            return {"ok": True}
        if p == "/local/forget":
            s.set(node_id=None, node_token=None, session=None); return {"ok": True}
        if p == "/local/login":
            r = self.hub.call("POST", "/api/hub/login", {"email": b.get("email"), "password": b.get("password")}, auth=None)
            s.set(session=r); return {"ok": True, "name": r["name"], "must_change": r.get("must_change")}
        if p == "/local/logout":
            try: self.hub.call("POST", "/api/hub/logout", {}, auth="user")
            except HubError: pass
            s.set(session=None); return {"ok": True}
        if p == "/local/hub":  # pass a reviewer request through to the hub with this user's sign-in
            path = str(b.get("path", ""))
            if not path.startswith("/api/"): raise ValueError("bad path")
            try: return self.hub.call(b.get("method", "GET"), path, b.get("body"), auth="user")
            except HubError as e:
                if e.code == 401 and path.startswith("/api/hub/"): s.set(session=None)
                raise
        if p == "/local/pause": s.set(paused=bool(b.get("paused"))); self.w.check_now(); return {"ok": True}
        if p == "/local/check-now": self.w.check_now(); return {"ok": True}
        if p == "/local/open":
            u = str(b.get("url", ""))
            if u.startswith("https://") or u.startswith("http://"): webbrowser.open(u)
            elif u == "log": webbrowser.open((APPDIR / "log.txt").as_uri())
            return {"ok": True}
        if p == "/local/autostart":
            from . import autostart
            autostart.set_enabled(bool(b.get("on"))); return {"ok": True}
        if p == "/local/update":
            from . import updater
            return updater.check_and_install(install=bool(b.get("install")))
        if p == "/local/quit": threading.Timer(0.3, self.on_quit).start(); return {"ok": True}
        raise ValueError("unknown action")
