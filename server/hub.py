"""Hub for LTA Watcher PCs and reviewers. Loaded by server.py.

PCs (nodes)
  POST /api/hub/enroll     {code, name}                -> {node_id, node_token}   one-time code from hub_admin.py
  POST /api/hub/heartbeat  {version, status}           -> {sources: [...due work for this PC...], ...}
  POST /api/hub/results    {source_id, kind, events | hash+text | error}
       kind "structured": official events, published straight away
       kind "ai":         page text; the hub runs the AI reader with LTA's key and queues verified items
       kind "unchanged" / "error": bookkeeping only
  Node calls carry the header  X-Node-Token: <token>

Reviewers (named people)
  POST /api/hub/login      {email, password}           -> {token, name, role}
  POST /api/hub/logout · GET /api/hub/me · POST /api/hub/password {old, new}
  GET  /api/hub/review                                  -> waiting items
  POST /api/hub/review     {id, action: approve|reject, edits: {...}}
  POST /api/hub/events     {...}                        add an event by hand (source link required)
  GET  /api/hub/status                                  PCs, sources, last runs, AI usage
  POST /api/hub/sources    {action: add|enable|disable, source}   (admins)
  Reviewer calls carry  Authorization: Bearer <token>

How work is shared: every enabled source belongs to exactly one online PC (online = seen in the
last 15 minutes), chosen by a stable hash of the source id. When a PC goes offline its sources move
to the others automatically. If no PC is online, the server's own cron (watch.py --stale-hours)
picks up anything not checked for a day.
"""
import datetime as dt, hashlib, hmac, json, os, pathlib, re, secrets, sqlite3, sys, zlib

AB = dt.timezone(dt.timedelta(hours=-6))
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "watcher"))
import watch, review  # noqa: E402

ONLINE_MIN = 15
DEFAULT_EVERY_H = float(os.environ.get("CHECK_EVERY_HOURS", "6"))
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "300"))
LATEST_VERSION = os.environ.get("LATEST_APP_VERSION", "")
_db = _lock = None

def now(): return dt.datetime.now(AB)
def h(s): return hashlib.sha256(s.encode()).hexdigest()

def setup(db_func, lock):
    global _db, _lock
    _db, _lock = db_func, lock
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created TEXT, last_seen TEXT,
            version TEXT, status TEXT, enabled INT DEFAULT 1);
        CREATE TABLE IF NOT EXISTS enroll_codes(code_hash TEXT PRIMARY KEY, created TEXT, expires TEXT, used_by TEXT);
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, pw TEXT, role TEXT, active INT DEFAULT 1,
            must_change INT DEFAULT 1, created TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT, created TEXT, expires TEXT);
        CREATE TABLE IF NOT EXISTS source_runs(source_id TEXT PRIMARY KEY, last_checked TEXT, last_hash TEXT, last_node TEXT,
            last_ok TEXT, last_error TEXT, fails INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ai_usage(day TEXT PRIMARY KEY, calls INT);
        CREATE TABLE IF NOT EXISTS audit(at TEXT, who TEXT, action TEXT, target TEXT, detail TEXT);
        """)

# ------------------------------------------------------------------ passwords, codes, audit
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"
def check_pw(pw, stored):
    try: _, salt, _ = stored.split("$")
    except ValueError: return False
    return hmac.compare_digest(hash_pw(pw, salt), stored)

def audit(who, action, target, detail=""):
    with _db() as c: c.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (now().isoformat(timespec="seconds"), who, action, target, detail[:500]))

def new_enroll_code(hours=72):
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))  # e.g. 3F9A-11C2-7B0D
    with _db() as c: c.execute("INSERT INTO enroll_codes VALUES(?,?,?,NULL)", (h(code), now().isoformat(), (now() + dt.timedelta(hours=hours)).isoformat()))
    return code

def add_user(name, email, role="reviewer"):
    pw = secrets.token_urlsafe(9)
    with _db() as c: c.execute("INSERT INTO users(id,name,email,pw,role,created) VALUES(?,?,?,?,?,?)", (secrets.token_hex(6), name, email.lower().strip(), hash_pw(pw), role, now().isoformat()))
    return pw

def bootstrap_admin():
    """First start: create the admin from BOOTSTRAP_ADMIN_* settings if there are no users yet."""
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower(); pw = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "Admin").strip()
    with _db() as c:
        if c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]: return
        if not email or len(pw) < 10:
            print("No users yet. Set BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD (10+ characters) to create the first admin."); return
        c.execute("INSERT INTO users(id,name,email,pw,role,must_change,created) VALUES(?,?,?,?,?,0,?)", (secrets.token_hex(6), name, email, hash_pw(pw), "admin", now().isoformat()))
    print(f"Created first admin: {name} <{email}>")

def merge_seed_sources():
    """Add sources that are new in the shipped list to the live list on the disk. Never changes or
    re-enables a source that's already there (so an admin's 'turn off' sticks)."""
    live_p = DATA / "sources.json"
    seed_p = pathlib.Path("/app/seed/sources.json")
    if not seed_p.exists(): seed_p = ROOT / "watcher/sources.json"
    if not live_p.exists() or seed_p.resolve() == live_p.resolve(): return
    live, seed = watch.jload(live_p, {"sources": []}), watch.jload(seed_p, {"sources": []})
    have = {x["id"] for x in live["sources"]}
    added = [x for x in seed["sources"] if x["id"] not in have]
    if added:
        live["sources"] += added; watch.jsave(live_p, live)
        print("Added new sources:", ", ".join(x["id"] for x in added))

# ------------------------------------------------------------------ sources and work sharing
def sources():
    return watch.jload(watch.sources_path(), {"sources": []})["sources"]

def online_nodes(c):
    cutoff = (now() - dt.timedelta(minutes=ONLINE_MIN)).isoformat()
    return [r["id"] for r in c.execute("SELECT id FROM nodes WHERE enabled=1 AND last_seen>=? ORDER BY id", (cutoff,))]

def owner(src_id, nodes):
    return nodes[zlib.crc32(src_id.encode()) % len(nodes)] if nodes else None

def due_for(node_id):
    with _db() as c:
        nodes = online_nodes(c)
        runs = {r["source_id"]: r for r in c.execute("SELECT * FROM source_runs")}
    out = []
    for s in sources():
        if s.get("enabled") is False or owner(s["id"], nodes) != node_id: continue
        r = runs.get(s["id"])
        every = float(s.get("every_hours", DEFAULT_EVERY_H))
        if r and r["fails"]: every = min(every, 1.0) if r["fails"] < 3 else every  # retry failures sooner, then back off
        if r and r["last_checked"] and now() - dt.datetime.fromisoformat(r["last_checked"]) < dt.timedelta(hours=every): continue
        out.append(dict(s, last_hash=r["last_hash"] if r else None))
    return out

def record_run(src_id, node, ok, hash_=None, error=None):
    t = now().isoformat(timespec="minutes")
    with _db() as c:
        r = c.execute("SELECT * FROM source_runs WHERE source_id=?", (src_id,)).fetchone()
        if not r: c.execute("INSERT INTO source_runs(source_id) VALUES(?)", (src_id,))
        if ok: c.execute("UPDATE source_runs SET last_checked=?, last_ok=?, last_node=?, last_hash=COALESCE(?,last_hash), last_error=NULL, fails=0 WHERE source_id=?", (t, t, node, hash_, src_id))
        else: c.execute("UPDATE source_runs SET last_checked=?, last_node=?, last_error=?, fails=fails+1 WHERE source_id=?", (t, node, (error or "")[:400], src_id))
    if not ok: return  # failures are retried soon; only successful checks hold off the server's backup run
    # keep the server's backup cron from re-checking what a PC just did
    st = watch.jload(watch.STATE, {}); prev = st.get(src_id, {})
    st[src_id] = {"hash": hash_ or prev.get("hash"), "checked": t, "by": node}
    watch.jsave(watch.STATE, st)

def key_set(): return len(os.environ.get("ANTHROPIC_API_KEY", "").strip()) > 20

def ai_allowed():
    if not key_set(): return False, "AI reader is off (no key on the server)"
    if os.environ.get("AI_ENABLED", "1") == "0": return False, "AI reader switched off on the server"
    day = now().date().isoformat()
    with _db() as c:
        r = c.execute("SELECT calls FROM ai_usage WHERE day=?", (day,)).fetchone()
        if r and r["calls"] >= AI_DAILY_LIMIT: return False, f"daily AI limit of {AI_DAILY_LIMIT} reached"
        c.execute("INSERT INTO ai_usage VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (day,))
    return True, ""

# ------------------------------------------------------------------ auth helpers
def node_from(handler):
    tok = handler.headers.get("X-Node-Token", "")
    if not tok: return None
    with _db() as c:
        r = c.execute("SELECT * FROM nodes WHERE token_hash=? AND enabled=1", (h(tok),)).fetchone()
    return r

def user_from(handler):
    a = handler.headers.get("Authorization", "")
    if not a.startswith("Bearer "): return None
    with _db() as c:
        s = c.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (h(a[7:]), now().isoformat())).fetchone()
        if not s: return None
        return c.execute("SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],)).fetchone()

LIMITS = {"title": 200, "description": 2000, "region": 80, "type": 30, "level": 20, "party": 30}

def apply_edits(item, edits):
    for k, n in LIMITS.items():
        if k in edits: item[k] = (str(edits[k]).strip()[:n] or None) if k == "party" else str(edits[k]).strip()[:n]
    if "location" in edits and isinstance(edits["location"], dict):
        item["location"] = {"name": str(edits["location"].get("name", ""))[:200], "address": str(edits["location"].get("address", ""))[:200]}
    if "start" in edits:
        s = str(edits["start"]).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s): item["start"], item["all_day"] = s, True
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s): item["start"], item["all_day"] = s + "-06:00", False
        else: raise ValueError("Start must be YYYY-MM-DD or YYYY-MM-DDTHH:MM (Alberta time).")
    if "end" in edits:
        e = str(edits["end"] or "").strip()
        if not e: item.pop("end", None)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", e): item["end"] = e
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", e): item["end"] = e + "-06:00"
        else: raise ValueError("End must be YYYY-MM-DD or YYYY-MM-DDTHH:MM.")
    return item

# ------------------------------------------------------------------ request router
def handle(hd, method, path, body):
    """Return True if the request was handled."""
    if not path.startswith("/api/hub/"): return False
    p = path[len("/api/hub/"):]

    # ---- PCs
    if p == "enroll" and method == "POST":
        code = str(body.get("code", "")).strip().upper(); name = str(body.get("name", "")).strip()[:60] or "LTA PC"
        with _lock, _db() as c:
            r = c.execute("SELECT * FROM enroll_codes WHERE code_hash=? AND used_by IS NULL AND expires>?", (h(code), now().isoformat())).fetchone()
            if not r: raise ValueError("That setup code is wrong, expired or already used. Ask an admin for a new one.")
            nid, tok = "pc-" + secrets.token_hex(4), secrets.token_urlsafe(32)
            c.execute("INSERT INTO nodes(id,name,token_hash,created,last_seen) VALUES(?,?,?,?,?)", (nid, name, h(tok), now().isoformat(), now().isoformat()))
            c.execute("UPDATE enroll_codes SET used_by=? WHERE code_hash=?", (nid, h(code)))
        audit(nid, "enrolled", nid, name)
        hd.out(200, {"node_id": nid, "node_token": tok}); return True

    if p in ("heartbeat", "results"):
        node = node_from(hd)
        if not node: hd.out(401, {"error": "This PC is not registered with the hub (or was disabled)."}); return True
        if p == "heartbeat":
            with _db() as c:
                c.execute("UPDATE nodes SET last_seen=?, version=?, status=? WHERE id=?", (now().isoformat(), str(body.get("version", ""))[:20], json.dumps(body.get("status", {}))[:2000], node["id"]))
            pend = len(watch.jload(DATA / "pending.json", {"pending": []})["pending"])
            hd.out(200, {"node_id": node["id"], "sources": due_for(node["id"]), "pending": pend, "latest_version": LATEST_VERSION,
                         "server_time": now().isoformat(timespec="seconds")}); return True
        src = next((s for s in sources() if s["id"] == body.get("source_id")), None)
        if not src: raise ValueError("Unknown source.")
        kind = body.get("kind")
        if kind == "error":
            record_run(src["id"], node["id"], False, error=str(body.get("error", ""))); hd.out(200, {"ok": True}); return True
        if kind == "unchanged":
            record_run(src["id"], node["id"], True, body.get("hash")); hd.out(200, {"ok": True}); return True
        with _lock:
            store = watch.Store()
            if kind == "structured":
                evs = body.get("events") or []
                for e in evs:  # a PC may only publish under its source's own origin
                    e["origin"] = "watcher:" + src["id"]; e["verified"] = True
                watch.clean_structured(src, evs)
                store.merge_structured(evs[:500]); store.save()
                record_run(src["id"], node["id"], True)
                hd.out(200, {"added": store.added, "updated": store.updated}); return True
            if kind == "ai":
                text = str(body.get("text", ""))[:120_000]
                ok, why = ai_allowed()
                if not ok:  # don't record the hash, so the page is read again once AI is available
                    record_run(src["id"], node["id"], False, error=why); hd.out(200, {"queued": 0, "note": why}); return True
                parties = watch.jload(DATA / "parties.json", {"parties": []})["parties"]
                try: n = watch.ai_process(src, text, store, parties)
                except RuntimeError as ex:  # key rejected, out of credit, etc.: don't mark the page as read
                    record_run(src["id"], node["id"], False, error=str(ex)); hd.out(200, {"queued": 0, "note": str(ex)}); return True
                store.save()
                record_run(src["id"], node["id"], True, body.get("hash"))
                hd.out(200, {"queued": n}); return True
        raise ValueError("Unknown result kind.")

    # ---- reviewers
    if p == "login" and method == "POST":
        email = str(body.get("email", "")).lower().strip()
        with _db() as c: u = c.execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
        if not u or not check_pw(str(body.get("password", "")), u["pw"]):
            hd.out(401, {"error": "Email or password is wrong."}); return True
        tok = secrets.token_urlsafe(32)
        with _db() as c: c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (h(tok), u["id"], now().isoformat(), (now() + dt.timedelta(days=30)).isoformat()))
        audit(u["name"], "signed in", email)
        hd.out(200, {"token": tok, "name": u["name"], "role": u["role"], "must_change": bool(u["must_change"])}); return True

    u = user_from(hd)
    if not u: hd.out(401, {"error": "Please sign in."}); return True
    who = u["name"]

    if p == "logout":
        with _db() as c: c.execute("DELETE FROM sessions WHERE token_hash=?", (h(hd.headers.get("Authorization", "")[7:]),))
        hd.out(200, {"ok": True}); return True
    if p == "me": hd.out(200, {"name": u["name"], "email": u["email"], "role": u["role"], "must_change": bool(u["must_change"])}); return True
    if p == "password" and method == "POST":
        if not check_pw(str(body.get("old", "")), u["pw"]): raise ValueError("Current password is wrong.")
        new = str(body.get("new", ""))
        if len(new) < 10: raise ValueError("Use at least 10 characters.")
        with _db() as c: c.execute("UPDATE users SET pw=?, must_change=0 WHERE id=?", (hash_pw(new), u["id"]))
        audit(who, "changed password", u["email"]); hd.out(200, {"ok": True}); return True

    if p == "review" and method == "GET":
        hd.out(200, watch.jload(DATA / "pending.json", {"pending": []})); return True
    if p == "review" and method == "POST":
        eid, action = str(body.get("id", "")), body.get("action")
        with _lock:
            pdoc = watch.jload(DATA / "pending.json", {"pending": [], "rejected_ids": []})
            item = next((x for x in pdoc["pending"] if x["id"] == eid), None)
            if not item: raise KeyError(eid)
            if action == "approve":
                if body.get("edits"):
                    apply_edits(item, body["edits"]); watch.jsave(DATA / "pending.json", pdoc)
                review.approve(eid, who, str(body.get("note", ""))[:300])
            elif action == "reject": review.reject(eid)
            else: raise ValueError("Unknown action.")
        audit(who, action, eid, item.get("title", "")); hd.out(200, {"ok": True}); return True

    if p == "events" and method == "POST":
        src_url = str(body.get("source_url", "")).strip()
        if not re.match(r"https?://", src_url): raise ValueError("A source link (https://…) is required for every event.")
        title = str(body.get("title", "")).strip()
        if not title: raise ValueError("Title is required.")
        ev = {"id": "", "title": "", "type": "other", "level": "party", "jurisdiction": "provincial", "party": None, "region": "",
              "location": {}, "description": "", "candidates": [], "links": [],
              "source": {"name": str(body.get("source_name", "") or src_url)[:200], "url": src_url, "kind": "primary" if body.get("primary", True) else "secondary", "checked": now().date().isoformat()},
              "status": "scheduled", "verified": True, "origin": "manual:" + who, "review": {"approved_by": who, "approved": now().isoformat(timespec="minutes")}}
        apply_edits(ev, body)
        ev["id"] = watch.slug(ev.get("party") or "lta", title, str(ev["start"])[:10])
        with _lock:
            store = watch.Store()
            if ev["id"] in store.by_id: raise ValueError("That event is already on the calendar.")
            store.by_id[ev["id"]] = ev; store.changes.append({"kind": "new", "id": ev["id"]}); store.save()
        audit(who, "added event", ev["id"], title); hd.out(200, {"ok": True, "id": ev["id"]}); return True

    if p == "status":
        with _db() as c:
            nodes = [dict(r) for r in c.execute("SELECT id,name,last_seen,version,status,enabled FROM nodes ORDER BY name")]
            runs = {r["source_id"]: dict(r) for r in c.execute("SELECT * FROM source_runs")}
            online = online_nodes(c)
            usage = c.execute("SELECT calls FROM ai_usage WHERE day=?", (now().date().isoformat(),)).fetchone()
            log = [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY at DESC LIMIT 30")]
        for n in nodes: n["online"] = n["id"] in online
        srcs = [dict(s, run=runs.get(s["id"]), assigned_to=owner(s["id"], online)) for s in sources()]
        ok = key_set() and os.environ.get("AI_ENABLED", "1") != "0"
        hd.out(200, {"nodes": nodes, "sources": srcs, "ai": {"on": ok, "calls_today": usage["calls"] if usage else 0, "daily_limit": AI_DAILY_LIMIT},
                     "pending": len(watch.jload(DATA / "pending.json", {"pending": []})["pending"]), "audit": log, "you": {"name": who, "role": u["role"]}}); return True

    # ---- admin: people and PCs (so nothing needs the command line)
    if p.startswith("admin/"):
        if u["role"] != "admin": hd.out(403, {"error": "Only admins can do that."}); return True
        a = p[6:]
        if a == "pc-code" and method == "POST":
            hours = max(1, min(int(body.get("hours", 72) or 72), 24 * 14))
            code = new_enroll_code(hours); audit(who, "made PC setup code", "", f"valid {hours} h")
            hd.out(200, {"code": code, "hours": hours}); return True
        if a == "users" and method == "GET":
            with _db() as c: rows = [dict(r) for r in c.execute("SELECT id,name,email,role,active,must_change,created FROM users ORDER BY name")]
            hd.out(200, {"users": rows}); return True
        if a == "users" and method == "POST":
            act = body.get("action", "add")
            if act == "add":
                name = str(body.get("name", "")).strip()[:80]; email = str(body.get("email", "")).strip().lower()[:120]
                role = "admin" if body.get("role") == "admin" else "reviewer"
                if not name or "@" not in email: raise ValueError("Name and a valid email are required.")
                try: pw = add_user(name, email, role)
                except sqlite3.IntegrityError: raise ValueError("Someone with that email already exists.")
                audit(who, f"added {role}", email, name); hd.out(200, {"temp_password": pw}); return True
            uid = str(body.get("id", ""))
            if uid == u["id"]: raise ValueError("You can't change your own account here.")
            with _db() as c:
                if act == "deactivate": c.execute("UPDATE users SET active=0 WHERE id=?", (uid,)); c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                elif act == "activate": c.execute("UPDATE users SET active=1 WHERE id=?", (uid,))
                elif act == "reset":
                    pw = secrets.token_urlsafe(9); c.execute("UPDATE users SET pw=?, must_change=1 WHERE id=?", (hash_pw(pw), uid)); c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                    audit(who, "reset password", uid); hd.out(200, {"temp_password": pw}); return True
                else: raise ValueError("Unknown action.")
            audit(who, act, uid); hd.out(200, {"ok": True}); return True
        if a == "pcs" and method == "POST":
            with _db() as c: c.execute("UPDATE nodes SET enabled=? WHERE id=?", (1 if body.get("action") == "enable" else 0, str(body.get("id", ""))))
            audit(who, f"PC {body.get('action')}", str(body.get("id", ""))); hd.out(200, {"ok": True}); return True
        hd.out(404, {"error": "Not found"}); return True

    if p == "sources" and method == "POST":
        if u["role"] != "admin": hd.out(403, {"error": "Only admins can change sources."}); return True
        doc = watch.jload(watch.sources_path(), {"sources": []})
        s = body.get("source") or {}; action = body.get("action")
        with _lock:
            if action == "add":
                if not re.match(r"https?://", str(s.get("url", ""))): raise ValueError("Source needs a web address.")
                if s.get("kind") not in ("escribe", "ics", "wp_tribe", "jsonld", "ai_page", "ai_news"): raise ValueError("Unknown reader type.")
                if s.get("level") not in (None, "", "federal", "provincial", "municipal", "party", "general"): raise ValueError("Unknown level.")
                s = {k: str(v)[:300] for k, v in s.items() if k in ("name", "kind", "url", "level", "jurisdiction", "party", "region", "type", "page")}
                s["id"] = watch.slug(s.get("name") or s["url"])
                if any(x["id"] == s["id"] for x in doc["sources"]): raise ValueError("A source with that name already exists.")
                s["added_by"] = who; doc["sources"].append(s)
            elif action in ("enable", "disable"):
                for x in doc["sources"]:
                    if x["id"] == s.get("id"): x["enabled"] = action == "enable"
            else: raise ValueError("Unknown action.")
            watch.jsave(DATA / "sources.json", doc)
        audit(who, f"source {action}", s.get("id", ""), s.get("url", "")); hd.out(200, {"ok": True}); return True

    hd.out(404, {"error": "Not found"}); return True
