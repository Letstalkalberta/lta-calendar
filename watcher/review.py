#!/usr/bin/env python3
"""Review queue for events the AI reader found.

  python3 watcher/review.py list
  python3 watcher/review.py show <id>
  python3 watcher/review.py approve <id> --by "Christopher"   # publishes it
  python3 watcher/review.py reject <id>                       # never suggested again

Before approving: open the source link, check the date, time, place and quote match the
page, and fix anything in data/pending.json first if needed. The server's admin page
(/admin/review) does the same thing in a browser.
"""
import argparse, datetime as dt, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "data"

def load(n, default):
    try: return json.loads((D / n).read_text())
    except FileNotFoundError: return default
def save(n, o): (D / n).write_text(json.dumps(o, indent=2, ensure_ascii=False) + "\n")

def approve(eid, by, note=""):
    p = load("pending.json", {"pending": [], "rejected_ids": []}); e = load("events.json", {"events": []})
    item = next((x for x in p["pending"] if x["id"] == eid), None)
    if not item: raise KeyError(eid)
    p["pending"] = [x for x in p["pending"] if x["id"] != eid]
    item["verified"] = True
    item["source"]["checked"] = dt.date.today().isoformat()
    item.setdefault("review", {}).update({"approved_by": by, "approved": dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).isoformat(timespec="minutes"), "note": note})
    e["events"] = [x for x in e["events"] if x["id"] != eid] + [item]
    e["events"].sort(key=lambda x: str(x["start"]))
    save("events.json", e); save("pending.json", p)
    log = load("changes.json", {"changes": []}); log["changes"].append({"kind": "new", "id": eid, "at": dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).isoformat(timespec="minutes")}); save("changes.json", log)
    return item

def reject(eid):
    p = load("pending.json", {"pending": [], "rejected_ids": []})
    p["pending"] = [x for x in p["pending"] if x["id"] != eid]
    p.setdefault("rejected_ids", []).append(eid); save("pending.json", p)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["list", "show", "approve", "reject"]); ap.add_argument("id", nargs="?")
    ap.add_argument("--by", default="LTA"); ap.add_argument("--note", default="")
    a = ap.parse_args()
    p = load("pending.json", {"pending": []})
    if a.cmd == "list":
        if not p["pending"]: print("Nothing waiting for review.")
        for x in p["pending"]:
            print(f'{x["id"]}\n   {x["start"]}  {x["title"]}\n   source: {x["source"]["url"]}\n   quote: "{x.get("review",{}).get("evidence_quote","")}"  ({x.get("review",{}).get("confidence")})\n')
    elif a.cmd == "show": print(json.dumps(next((x for x in p["pending"] if x["id"] == a.id), {}), indent=2))
    elif a.cmd == "approve": print("Published:", approve(a.id, a.by, a.note)["title"])
    elif a.cmd == "reject": reject(a.id); print("Rejected", a.id)
