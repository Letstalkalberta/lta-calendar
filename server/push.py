#!/usr/bin/env python3
"""Push notifications for the future LTA phone app (Firebase Cloud Messaging, HTTP v1).

Run every 15 minutes (cron). For each device registered through POST /api/devices it sends:
  * "new event"  when the watcher or a reviewer publishes an event matching what the device follows
  * "changed"    when a followed event's date, time or place changes
  * reminders    24 hours and 1 hour before events the device follows (starred events, or
                 matching its parties / levels / types / regions)
Each notification is sent once per device (table 'sent').

Needs:  pip install google-auth requests
        FCM_PROJECT_ID, GOOGLE_APPLICATION_CREDENTIALS (service-account JSON from Firebase)
The app must send its FCM token and follow choices to POST /api/devices.
Status: written against the FCM HTTP v1 API; not yet run against a live Firebase project.
"""
import datetime as dt, json, os, pathlib, sqlite3, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import server as S

AB = S.AB
DRY = "--dry-run" in sys.argv

def fcm_session():
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    creds = service_account.Credentials.from_service_account_file(os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=["https://www.googleapis.com/auth/firebase.messaging"])
    return AuthorizedSession(creds)

def send(sess, token, title, body, eid):
    if DRY: print(f"  would send to {token[:12]}…: {title} | {body}"); return True
    r = sess.post(f"https://fcm.googleapis.com/v1/projects/{os.environ['FCM_PROJECT_ID']}/messages:send",
                  json={"message": {"token": token, "notification": {"title": title, "body": body},
                                    "data": {"event_id": eid, "url": f"{S.PUBLIC}/e/{eid}"}}}, timeout=20)
    if r.status_code == 404 or "UNREGISTERED" in r.text:  # app uninstalled
        with S.db() as c: c.execute("DELETE FROM devices WHERE token=?", (token,))
    return r.ok

def matches(follows, e):
    if e["id"] in follows.get("events", []): return True
    checks = [("levels", e.get("level")), ("parties", e.get("party")), ("types", e.get("type"))]
    chosen = [k for k, _ in checks if follows.get(k)] + (["regions"] if follows.get("regions") else [])
    if not chosen: return False
    ok = all(v in follows[k] for k, v in checks if follows.get(k))
    if follows.get("regions"): ok = ok and any(r.lower() in (e.get("region") or "").lower() for r in follows["regions"])
    return ok

def when(e):
    s = str(e["start"])
    return dt.datetime.fromisoformat(s + "T09:00:00-06:00") if len(s) == 10 else dt.datetime.fromisoformat(s)

def main():
    S.init_db()
    events = {e["id"]: e for e in S.published()[1]}
    changes = S.load_json("changes.json", {"changes": []})["changes"]
    def at(c):
        t = dt.datetime.fromisoformat(c["at"])
        return t if t.tzinfo else t.replace(tzinfo=AB)
    recent = [c for c in changes if at(c) > dt.datetime.now(AB) - dt.timedelta(days=2)]
    now = dt.datetime.now(AB)
    sess = None if DRY else fcm_session()
    with S.db() as c:
        devices = c.execute("SELECT * FROM devices").fetchall()
        sent = {(r["device"], r["event"], r["kind"]) for r in c.execute("SELECT * FROM sent")}
    out = []
    for d in devices:
        f = json.loads(d["follows"] or "{}")
        for ch in recent:
            e = events.get(ch["id"])
            if e and matches(f, e) and (d["token"], e["id"], ch["kind"]) not in sent and when(e) > now:
                out.append((d["token"], e, ch["kind"], ("New: " if ch["kind"] == "new" else "Changed: ") + e["title"]))
        for e in events.values():
            if not matches(f, e): continue
            delta = when(e) - now
            for kind, lo, hi in (("remind-24h", dt.timedelta(hours=23), dt.timedelta(hours=24, minutes=15)), ("remind-1h", dt.timedelta(minutes=45), dt.timedelta(hours=1, minutes=15))):
                if lo <= delta <= hi and (d["token"], e["id"], kind) not in sent:
                    out.append((d["token"], e, kind, ("Tomorrow: " if kind == "remind-24h" else "In an hour: ") + e["title"]))
    for tok, e, kind, title in out:
        loc = (e.get("location") or {}).get("name", "")
        if send(sess, tok, title, loc or "Tap for details, sharing and rides", e["id"]) and not DRY:
            with S.db() as c: c.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?,?)", (tok, e["id"], kind, now.isoformat()))
    print(f"{len(out)} notification(s){' (dry run)' if DRY else ''}")

if __name__ == "__main__":
    main()
