#!/usr/bin/env python3
"""Hub admin commands (run on the server).

  python3 server/hub_admin.py add-reviewer "Jamie Wu" jamie@example.com     # prints a temporary password
  python3 server/hub_admin.py add-admin "Christopher Scott" cjscottx@gmail.com
  python3 server/hub_admin.py reset-password jamie@example.com
  python3 server/hub_admin.py deactivate jamie@example.com
  python3 server/hub_admin.py users
  python3 server/hub_admin.py pc-code [--hours 72]      # one-time setup code for a new LTA PC
  python3 server/hub_admin.py pcs
  python3 server/hub_admin.py disable-pc pc-1a2b3c4d
"""
import argparse, sys, pathlib, threading
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import server, hub

server.init_db(); hub.setup(server.db, threading.Lock())
ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("args", nargs="*"); ap.add_argument("--hours", type=int, default=72)
a = ap.parse_args()
with server.db() as c:
    if a.cmd in ("add-reviewer", "add-admin"):
        name, email = a.args
        pw = hub.add_user(name, email, "admin" if a.cmd == "add-admin" else "reviewer")
        print(f"Added {name} <{email}>. Temporary password: {pw}\nThey'll be asked to change it after signing in.")
    elif a.cmd == "reset-password":
        pw = __import__("secrets").token_urlsafe(9)
        n = c.execute("UPDATE users SET pw=?, must_change=1 WHERE email=?", (hub.hash_pw(pw), a.args[0].lower())).rowcount
        print(f"New temporary password: {pw}" if n else "No such user.")
    elif a.cmd == "deactivate":
        c.execute("UPDATE users SET active=0 WHERE email=?", (a.args[0].lower(),)); c.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email=?)", (a.args[0].lower(),)); print("Done.")
    elif a.cmd == "users":
        for r in c.execute("SELECT name,email,role,active FROM users ORDER BY name"): print(f"{r['name']:<28} {r['email']:<34} {r['role']:<9} {'active' if r['active'] else 'off'}")
    elif a.cmd == "pc-code":
        print(f"Setup code for a new LTA PC (valid {a.hours} hours, works once): {hub.new_enroll_code(a.hours)}")
    elif a.cmd == "pcs":
        for r in c.execute("SELECT id,name,last_seen,version,enabled FROM nodes ORDER BY name"): print(f"{r['id']:<14} {r['name']:<24} last seen {r['last_seen'][:16]}  v{r['version'] or '?'}  {'' if r['enabled'] else 'DISABLED'}")
    elif a.cmd == "disable-pc":
        c.execute("UPDATE nodes SET enabled=0 WHERE id=?", (a.args[0],)); print("Disabled.")
    else:
        print(__doc__)
