#!/usr/bin/env python3
"""LTA political calendar server. Python standard library only (plus SQLite, built in).

Public
  GET  /api/events                    published events (verified only)
  GET  /api/parties                   parties and governance documents
  GET  /feeds/<feed>.ics              calendar subscription feeds: all, federal, provincial,
                                      municipal, party, party-<party id>
  GET  /e/<event id>                  share page: rich link preview (Facebook, X, iMessage),
                                      then forwards to the calendar on letstalkalberta.com
  GET  /api/rides?event=<id>          public ride board (first names and towns only)
  POST /api/rides/offers              offer a ride  -> returns the driver's private manage link
  POST /api/rides/needs               ask for a ride -> returns the rider's private manage link
  POST /api/rides/offers/<id>/requests  ask a driver for a seat
  GET  /rides/manage/<token>          private page for a driver (sees requests + contacts) or rider
  POST /api/rides/manage/<token>      {action: accept|decline|delete, request_id}

App (for the future LTA phone app)
  POST   /api/devices                 {token, platform, follows:{levels,parties,types,regions,events}}
  DELETE /api/devices/<token>

Admin (needs ADMIN_KEY)
  GET  /admin/review?key=...          approve or reject what the AI reader found
  POST /admin/review                  {key, id, action: approve|reject}

Environment
  PORT (8080), PUBLIC_BASE (https://calendar-api.letstalkalberta.com), CALENDAR_PAGE
  (https://www.letstalkalberta.com/political-calendar), ALLOWED_ORIGINS (comma list),
  ADMIN_KEY, DB_PATH (server/lta.db), SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_FROM (optional)
"""
import datetime as dt, html, json, os, pathlib, re, secrets, smtplib, sqlite3, sys, threading, time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "watcher"))
sys.path.insert(0, str(ROOT / "server"))
DATA = ROOT / "data"
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC = (os.environ.get("PUBLIC_BASE") or os.environ.get("RENDER_EXTERNAL_URL") or f"http://localhost:{PORT}").rstrip("/")
PAGE = os.environ.get("CALENDAR_PAGE", "https://www.letstalkalberta.com/political-calendar")
ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://www.letstalkalberta.com,https://letstalkalberta.com").split(",") if o.strip()]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "server/lta.db"))
AB = dt.timezone(dt.timedelta(hours=-6))
LOCK = threading.Lock()

# ------------------------------------------------------------------ storage
def db():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c
def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS offers(id TEXT PRIMARY KEY, event TEXT, name TEXT, contact TEXT, origin TEXT, depart TEXT,
            seats INT, taken INT DEFAULT 0, notes TEXT, token TEXT UNIQUE, created TEXT);
        CREATE TABLE IF NOT EXISTS needs(id TEXT PRIMARY KEY, event TEXT, name TEXT, contact TEXT, origin TEXT, seats INT,
            notes TEXT, token TEXT UNIQUE, created TEXT);
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, offer TEXT, name TEXT, contact TEXT, seats INT, notes TEXT,
            status TEXT DEFAULT 'pending', token TEXT UNIQUE, created TEXT);
        CREATE TABLE IF NOT EXISTS devices(token TEXT PRIMARY KEY, platform TEXT, follows TEXT, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS sent(device TEXT, event TEXT, kind TEXT, at TEXT, PRIMARY KEY(device,event,kind));
        """)

def load_json(name, default):
    try: return json.loads((DATA / name).read_text())
    except FileNotFoundError: return default
def published():
    d = load_json("events.json", {"events": []})
    return d.get("generated"), [e for e in d["events"] if e.get("verified") is not False]
def event_by_id(eid): return next((e for e in published()[1] if e["id"] == eid), None)

def event_end(e):
    s = str(e.get("end") or e["start"])
    return dt.datetime.fromisoformat(s + ("T23:59:00-06:00" if len(s) == 10 else ""))

def purge_old_rides():
    """Privacy: delete ride contacts one day after the event ends."""
    ended = {e["id"] for e in published()[1] if event_end(e) < dt.datetime.now(AB) - dt.timedelta(days=1)}
    if not ended: return
    with db() as c:
        q = ",".join("?" * len(ended))
        c.execute(f"DELETE FROM requests WHERE offer IN (SELECT id FROM offers WHERE event IN ({q}))", tuple(ended))
        c.execute(f"DELETE FROM offers WHERE event IN ({q})", tuple(ended))
        c.execute(f"DELETE FROM needs WHERE event IN ({q})", tuple(ended))

# ------------------------------------------------------------------ feeds
def ics_escape(s): return str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
def utc(d): return d.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
def fold(line):
    out, b = [], line.encode()
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80: cut -= 1
        out.append(b[:cut].decode()); b = b" " + b[cut:]
    out.append(b.decode()); return "\r\n".join(out)

def build_ics(events, name):
    now = utc(dt.datetime.now(dt.timezone.utc))
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Let's Talk Alberta//Political Calendar//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         f"X-WR-CALNAME:{ics_escape(name)}", "X-WR-TIMEZONE:America/Regina", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for e in events:
        loc = ", ".join(x for x in [(e.get("location") or {}).get("name"), (e.get("location") or {}).get("address")] if x)
        L += ["BEGIN:VEVENT", f"UID:{e['id']}@letstalkalberta.com", f"DTSTAMP:{now}"]
        if e.get("all_day") or len(str(e["start"])) == 10:
            s = dt.date.fromisoformat(str(e["start"])[:10]); en = dt.date.fromisoformat(str(e.get("end") or e["start"])[:10]) + dt.timedelta(days=1)
            L += [f"DTSTART;VALUE=DATE:{s:%Y%m%d}", f"DTEND;VALUE=DATE:{en:%Y%m%d}"]
        else:
            s = dt.datetime.fromisoformat(e["start"]); en = dt.datetime.fromisoformat(e["end"]) if e.get("end") else s + dt.timedelta(hours=1)
            L += [f"DTSTART:{utc(s)}", f"DTEND:{utc(en)}"]
        desc = (e.get("description") or "") + f"\n\nSource: {e['source']['url']}\nDetails, candidates, sharing and rides: {PUBLIC}/e/{e['id']}"
        L += [f"SUMMARY:{ics_escape(e['title'])}", f"LOCATION:{ics_escape(loc)}", f"DESCRIPTION:{ics_escape(desc)}", f"URL:{PUBLIC}/e/{e['id']}"]
        if e.get("status") == "cancelled": L.append("STATUS:CANCELLED")
        L += ["END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"

def feed_events(feed):
    evs = [e for e in published()[1] if event_end(e) > dt.datetime.now(AB) - dt.timedelta(days=90)]
    if feed == "all": return evs, "LTA Political Calendar"
    if feed in ("federal", "provincial", "municipal", "party"):
        return [e for e in evs if e["level"] == feed], f"LTA Political Calendar: {feed.title()}"
    if feed.startswith("party-"):
        pid = feed[6:]; p = next((x for x in load_json("parties.json", {"parties": []})["parties"] if x["id"] == pid), None)
        if p: return [e for e in evs if e.get("party") == pid or any(c.get("party") == pid for c in e.get("candidates", []))], f"LTA Political Calendar: {p['name']}"
    return None, None

# ------------------------------------------------------------------ email (optional)
def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host or "@" not in (to or ""): return False
    try:
        m = EmailMessage(); m["From"] = os.environ.get("MAIL_FROM", "letstalkalberta@gmail.com"); m["To"] = to; m["Subject"] = subject; m.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"): s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            s.send_message(m)
        return True
    except Exception as ex:
        print("mail failed:", ex); return False

# ------------------------------------------------------------------ rate limit + validation
HITS = {}
def limited(ip, bucket, n=20, per=3600):
    k = (ip, bucket); now = time.time(); h = [t for t in HITS.get(k, []) if now - t < per]
    HITS[k] = h + [now]; return len(h) >= n
def clean(d, field, maxlen, required=True):
    v = str(d.get(field, "") or "").strip()
    v = re.sub(r"[\x00-\x1f]", " ", v)[:maxlen]
    if required and not v: raise ValueError(f"Please fill in {field}.")
    return v
def seats(d, hi):
    try: n = int(d.get("seats", 1))
    except (TypeError, ValueError): raise ValueError("Seats must be a number.")
    if not 1 <= n <= hi: raise ValueError(f"Seats must be between 1 and {hi}.")
    return n

# ------------------------------------------------------------------ pages
CSS = "body{font-family:Arial,sans-serif;max-width:720px;margin:24px auto;padding:0 14px;color:#1d2433}h1{font-family:Georgia,serif;color:#0B2C6E}.card{border:1px solid #e3dccd;border-radius:10px;padding:12px;margin:10px 0}.btn{background:#C8102E;color:#fff;border:0;border-radius:999px;padding:7px 14px;font-weight:bold;cursor:pointer;margin-right:6px}.btn.g{background:#0B2C6E}.muted{color:#5b6474;font-size:14px}.q{background:#FDF7EC;border-left:4px solid #E8A800;padding:8px}"
def page(title, body):
    return f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"

def share_page(e):
    when = e["start"][:10] if len(str(e["start"])) == 10 else dt.datetime.fromisoformat(e["start"]).strftime("%A, %B %-d, %Y at %-I:%M %p") + " Alberta time"
    loc = ", ".join(x for x in [(e.get("location") or {}).get("name"), (e.get("location") or {}).get("address")] if x)
    desc = f"{when}. {loc}. Details, candidates, sharing and carpools on the Let's Talk Alberta political calendar."
    target = f"{PAGE}#event-{e['id']}"
    t, d, u = html.escape(e["title"]), html.escape(desc), html.escape(target)
    return (f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8><title>{t}</title>"
            f"<meta property='og:title' content='{t}'><meta property='og:description' content='{d}'><meta property='og:type' content='website'>"
            f"<meta property='og:url' content='{html.escape(PUBLIC)}/e/{html.escape(e['id'])}'><meta property='og:image' content='{html.escape(PUBLIC)}/static/og.png'>"
            f"<meta name='twitter:card' content='summary_large_image'><meta http-equiv='refresh' content='0;url={u}'>"
            f"<link rel=canonical href='{u}'></head><body><p><a href='{u}'>{t}</a></p></body></html>")

def manage_page(token):
    with db() as c:
        o = c.execute("SELECT * FROM offers WHERE token=?", (token,)).fetchone()
        n = None if o else c.execute("SELECT * FROM needs WHERE token=?", (token,)).fetchone()
        if o:
            reqs = c.execute("SELECT * FROM requests WHERE offer=? ORDER BY created", (o["id"],)).fetchall()
    if not (o or n): return None
    ev = event_by_id((o or n)["event"]); evt = html.escape(ev["title"] if ev else (o or n)["event"])
    js = ("<script>function act(a,r){fetch('/api/rides/manage/" + token + "',{method:'POST',headers:{'Content-Type':'application/json'},"
          "body:JSON.stringify({action:a,request_id:r})}).then(x=>x.json()).then(j=>{if(j.error)alert(j.error);location.reload()})}</script>")
    if o:
        rows = "".join(f"<div class=card><b>{html.escape(r['name'])}</b> · {r['seats']} seat(s) · <i>{html.escape(r['status'])}</i><br>Contact: <b>{html.escape(r['contact'])}</b>"
                       f"{'<br>Note: ' + html.escape(r['notes']) if r['notes'] else ''}<br><br>"
                       + (f"<button class=btn onclick=\"act('accept','{r['id']}')\">Accept</button><button class='btn g' onclick=\"act('decline','{r['id']}')\">Decline</button>" if r["status"] == "pending" else "")
                       + "</div>" for r in reqs) or "<p class=muted>No requests yet. Share your event link so people know you have seats.</p>"
        body = (f"<h1>Your ride</h1><p><b>{evt}</b><br>From {html.escape(o['origin'])}{' · leaves ' + html.escape(o['depart']) if o['depart'] else ''} · {o['taken']} of {o['seats']} seats taken</p>"
                f"<p class=muted>Keep this page private. Contact the riders you accept to arrange pickup. Riders' details are deleted one day after the event.</p><h2>Seat requests</h2>{rows}"
                f"<p><button class='btn g' onclick=\"if(confirm('Remove your ride?'))act('delete','')\">Remove my ride</button></p>{js}")
    else:
        body = (f"<h1>Your ride request</h1><p><b>{evt}</b><br>From {html.escape(n['origin'])} · {n['seats']} seat(s)</p>"
                f"<p class=muted>Drivers can see your first name and town. Ask a driver for a seat from the event's Rides tab.</p>"
                f"<p><button class='btn g' onclick=\"if(confirm('Remove your request?'))act('delete','')\">Remove my request</button></p>{js}")
    return page("Manage your ride", body)

def review_page(key):
    p = load_json("pending.json", {"pending": []})["pending"]
    cards = "".join(f"<div class=card><b>{html.escape(x['title'])}</b><br>{html.escape(str(x['start']))} · {html.escape(x.get('level',''))} · {html.escape(x.get('party') or '')}<br>"
                    f"<span class=muted>{html.escape(x.get('description',''))}</span><div class=q>“{html.escape(x.get('review',{}).get('evidence_quote',''))}” <span class=muted>({html.escape(str(x.get('review',{}).get('confidence')))})</span></div>"
                    f"<p><a href='{html.escape(x['source']['url'])}' target=_blank>Open the source and check it</a></p>"
                    f"<button class=btn onclick=\"go('{x['id']}','approve')\">Approve and publish</button><button class='btn g' onclick=\"go('{x['id']}','reject')\">Reject</button></div>" for x in p)
    rep = load_json("watch_report.json", {})
    fails = "".join(f"<li>{html.escape(f['id'])}: {html.escape(f['error'])}</li>" for f in rep.get("failed", []))
    js = ("<script>function go(id,a){fetch('/admin/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:"
          + json.dumps(key) + ",id:id,action:a})}).then(r=>r.json()).then(j=>{if(j.error)alert(j.error);location.reload()})}</script>")
    return page("Review queue", f"<h1>Review queue</h1><p class=muted>Found by the AI reader. Nothing here is public until you approve it. Check every item against its source.</p>{cards or '<p>Nothing waiting.</p>'}"
                f"<h2>Last watcher run</h2><p class=muted>{html.escape(rep.get('checked_at','never'))} · added {rep.get('added',0)} · updated {rep.get('updated',0)} · queued {rep.get('queued',0)}</p>"
                + (f"<p><b>Sources that failed:</b></p><ul>{fails}</ul>" if fails else "") + js)

# ------------------------------------------------------------------ handler
class H(BaseHTTPRequestHandler):
    server_version = "LTACalendar/1.0"
    def log_message(self, fmt, *a): sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))
    def ip(self): return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
    def cors(self):
        o = self.headers.get("Origin")
        if o and (o in ORIGINS or "*" in ORIGINS):
            self.send_header("Access-Control-Allow-Origin", o); self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS"); self.send_header("Access-Control-Allow-Headers", "Content-Type")
    def out(self, code, body, ctype="application/json; charset=utf-8", cache=None):
        b = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cache: self.send_header("Cache-Control", cache)
        self.cors(); self.end_headers(); self.wfile.write(b)
    def body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 20000: raise ValueError("Request too large.")
        try: return json.loads(self.rfile.read(n) or b"{}")
        except ValueError: raise ValueError("Bad request.")
    def do_OPTIONS(self): self.send_response(204); self.cors(); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); p = u.path
        try:
            if hub.handle(self, "GET", p, {}): return
        except ValueError as ex: return self.out(400, {"error": str(ex)})
        if p == "/api/events":
            gen, evs = published(); return self.out(200, {"generated": gen, "events": evs}, cache="public, max-age=300")
        if p == "/api/parties": return self.out(200, load_json("parties.json", {"parties": []}), cache="public, max-age=3600")
        m = re.fullmatch(r"/feeds/([a-z0-9-]+)\.ics", p)
        if m:
            evs, name = feed_events(m.group(1))
            if evs is None: return self.out(404, {"error": "No such feed"})
            return self.out(200, build_ics(evs, name), "text/calendar; charset=utf-8", cache="public, max-age=1800")
        m = re.fullmatch(r"/e/([a-z0-9-]+)", p)
        if m:
            e = event_by_id(m.group(1))
            if e: return self.out(200, share_page(e), "text/html; charset=utf-8")
            return self._redir(PAGE)
        if p == "/api/rides":
            eid = (q.get("event") or [""])[0]
            with db() as c:
                offers = [{"id": r["id"], "name": r["name"], "from": r["origin"], "depart": r["depart"], "seats": r["seats"], "taken": r["taken"], "notes": r["notes"]}
                          for r in c.execute("SELECT * FROM offers WHERE event=? ORDER BY created", (eid,))]
                needs = [{"id": r["id"], "name": r["name"], "from": r["origin"], "seats": r["seats"], "notes": r["notes"]}
                         for r in c.execute("SELECT * FROM needs WHERE event=? ORDER BY created", (eid,))]
            return self.out(200, {"offers": offers, "needs": needs})
        m = re.fullmatch(r"/rides/manage/([A-Za-z0-9_-]{20,})", p)
        if m:
            pg = manage_page(m.group(1)); return self.out(200 if pg else 404, pg or page("Not found", "<h1>That link has expired</h1><p>Ride details are deleted one day after the event.</p>"), "text/html; charset=utf-8")
        if p in ("/admin", "/admin/"):
            return self.out(200, (ROOT / "server/admin.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/admin/review":
            k = (q.get("key") or [""])[0]
            if not ADMIN_KEY or not secrets.compare_digest(k, ADMIN_KEY): return self.out(403, page("No access", "<h1>No access</h1>"), "text/html; charset=utf-8")
            return self.out(200, review_page(k), "text/html; charset=utf-8")
        if p == "/health": return self.out(200, {"ok": True})
        if p == "/static/og.png" and (ROOT / "server/og.png").exists(): return self.out(200, (ROOT / "server/og.png").read_bytes(), "image/png", cache="public, max-age=86400")
        self.out(404, {"error": "Not found"})

    def _redir(self, to):
        self.send_response(302); self.send_header("Location", to); self.end_headers(); return False

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            d = self.body()
            if p.startswith("/api/hub/"):
                if p in ("/api/hub/login", "/api/hub/enroll") and limited(self.ip(), p, n=20, per=900): return self.out(429, {"error": "Too many attempts. Wait 15 minutes."})
                if hub.handle(self, "POST", p, d): return
            if d.get("website"): return self.out(200, {"ok": True})  # honeypot field
            if p in ("/api/rides/offers", "/api/rides/needs", "/api/devices") or p.startswith("/api/rides/offers/"):
                if limited(self.ip(), p.split("/")[3] if p.count("/") > 2 else p, n=30): return self.out(429, {"error": "Too many requests. Please try again later."})
            if p == "/api/rides/offers":
                ev = event_by_id(clean(d, "event", 100))
                if not ev: raise ValueError("Unknown event.")
                if event_end(ev) < dt.datetime.now(AB): raise ValueError("This event has passed.")
                rid, tok = secrets.token_urlsafe(8), secrets.token_urlsafe(24)
                row = (rid, ev["id"], clean(d, "name", 40), clean(d, "contact", 80), clean(d, "from", 60), clean(d, "depart", 30, False), seats(d, 12), clean(d, "notes", 140, False), tok, dt.datetime.now(AB).isoformat())
                with LOCK, db() as c: c.execute("INSERT INTO offers(id,event,name,contact,origin,depart,seats,notes,token,created) VALUES(?,?,?,?,?,?,?,?,?,?)", row)
                url = f"{PUBLIC}/rides/manage/{tok}"
                send_mail(row[3], f"Your ride to {ev['title']}", f"Thanks for offering a ride.\n\nYour private link to see seat requests:\n{url}\n\nLet's Talk Alberta")
                return self.out(200, {"id": rid, "manage_url": url})
            if p == "/api/rides/needs":
                ev = event_by_id(clean(d, "event", 100))
                if not ev: raise ValueError("Unknown event.")
                rid, tok = secrets.token_urlsafe(8), secrets.token_urlsafe(24)
                row = (rid, ev["id"], clean(d, "name", 40), clean(d, "contact", 80), clean(d, "from", 60), seats(d, 8), clean(d, "notes", 140, False), tok, dt.datetime.now(AB).isoformat())
                with LOCK, db() as c: c.execute("INSERT INTO needs VALUES(?,?,?,?,?,?,?,?,?)", row)
                return self.out(200, {"id": rid, "manage_url": f"{PUBLIC}/rides/manage/{tok}"})
            m = re.fullmatch(r"/api/rides/offers/([A-Za-z0-9_-]+)/requests", p)
            if m:
                with LOCK, db() as c:
                    o = c.execute("SELECT * FROM offers WHERE id=?", (m.group(1),)).fetchone()
                    if not o: raise ValueError("That ride is no longer available.")
                    n = seats(d, 8)
                    if o["taken"] + n > o["seats"]: raise ValueError("Not enough seats left in that car.")
                    row = (secrets.token_urlsafe(8), o["id"], clean(d, "name", 40), clean(d, "contact", 80), n, clean(d, "notes", 140, False), secrets.token_urlsafe(16), dt.datetime.now(AB).isoformat())
                    c.execute("INSERT INTO requests(id,offer,name,contact,seats,notes,token,created) VALUES(?,?,?,?,?,?,?,?)", row)
                send_mail(o["contact"], "Someone asked for a seat in your car", f"{row[2]} asked for {n} seat(s).\n\nSee their contact and accept or decline:\n{PUBLIC}/rides/manage/{o['token']}\n\nLet's Talk Alberta")
                return self.out(200, {"ok": True})
            m = re.fullmatch(r"/api/rides/manage/([A-Za-z0-9_-]{20,})", p)
            if m:
                tok, a, rq = m.group(1), d.get("action"), d.get("request_id", "")
                with LOCK, db() as c:
                    o = c.execute("SELECT * FROM offers WHERE token=?", (tok,)).fetchone()
                    if a == "delete":
                        if o: c.execute("DELETE FROM requests WHERE offer=?", (o["id"],)); c.execute("DELETE FROM offers WHERE id=?", (o["id"],))
                        else: c.execute("DELETE FROM needs WHERE token=?", (tok,))
                        return self.out(200, {"ok": True})
                    if not o: raise ValueError("Not found.")
                    r = c.execute("SELECT * FROM requests WHERE id=? AND offer=?", (rq, o["id"])).fetchone()
                    if not r or r["status"] != "pending": raise ValueError("That request was already handled.")
                    if a == "accept":
                        if o["taken"] + r["seats"] > o["seats"]: raise ValueError("Not enough seats left.")
                        c.execute("UPDATE offers SET taken=taken+? WHERE id=?", (r["seats"], o["id"])); c.execute("UPDATE requests SET status='accepted' WHERE id=?", (rq,))
                        send_mail(r["contact"], "You have a seat", f"{o['name']} accepted your request and will contact you to arrange pickup.\n\nLet's Talk Alberta")
                    elif a == "decline": c.execute("UPDATE requests SET status='declined' WHERE id=?", (rq,))
                    else: raise ValueError("Unknown action.")
                return self.out(200, {"ok": True})
            if p == "/api/devices":
                tok = clean(d, "token", 400); plat = clean(d, "platform", 20)
                f = d.get("follows") or {}
                f = {k: [str(x)[:80] for x in (f.get(k) or [])][:100] for k in ("levels", "parties", "types", "regions", "events")}
                now = dt.datetime.now(AB).isoformat()
                with LOCK, db() as c: c.execute("INSERT INTO devices VALUES(?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET platform=excluded.platform, follows=excluded.follows, updated=excluded.updated", (tok, plat, json.dumps(f), now, now))
                return self.out(200, {"ok": True})
            if p == "/admin/review":
                if not ADMIN_KEY or not secrets.compare_digest(str(d.get("key", "")), ADMIN_KEY): return self.out(403, {"error": "No access"})
                import review
                with LOCK:
                    if d.get("action") == "approve": review.approve(d["id"], "admin page")
                    elif d.get("action") == "reject": review.reject(d["id"])
                    else: raise ValueError("Unknown action.")
                return self.out(200, {"ok": True})
            self.out(404, {"error": "Not found"})
        except ValueError as ex: self.out(400, {"error": str(ex)})
        except KeyError as ex: self.out(404, {"error": f"Not found: {ex}"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/devices/(.+)", urlparse(self.path).path)
        if not m: return self.out(404, {"error": "Not found"})
        with LOCK, db() as c: c.execute("DELETE FROM devices WHERE token=?", (m.group(1),))
        self.out(200, {"ok": True})

def housekeeping():
    while True:
        try: purge_old_rides()
        except Exception as ex: print("housekeeping:", ex)
        time.sleep(3600)

def scheduler():
    """Backup watcher (sites no PC has checked in a day) every 6 hours; app push every 15 minutes."""
    import watch
    last_watch = 0
    time.sleep(60)
    while True:
        if time.time() - last_watch > 6 * 3600:
            try:
                rep = watch.run(stale_hours=24, lock=LOCK)
                for sid in rep.get("ok", []): hub.record_run(sid, "server", True)
                for f in rep.get("failed", []): hub.record_run(f["id"], "server", False, error=f["error"])
            except Exception as ex: print("backup watch:", ex)
            last_watch = time.time()
        if os.environ.get("FCM_PROJECT_ID"):
            try:
                import subprocess; subprocess.run([sys.executable, str(ROOT / "server/push.py")], timeout=300)
            except Exception as ex: print("push:", ex)
        time.sleep(900)

import hub  # noqa: E402  (PCs and reviewers)

if __name__ == "__main__":
    init_db()
    hub.setup(db, LOCK)
    hub.bootstrap_admin()
    threading.Thread(target=housekeeping, daemon=True).start()
    if os.environ.get("RUN_SCHEDULER", "1") == "1": threading.Thread(target=scheduler, daemon=True).start()
    print(f"LTA calendar server on :{PORT}  public base {PUBLIC}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
