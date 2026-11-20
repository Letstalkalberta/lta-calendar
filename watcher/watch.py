#!/usr/bin/env python3
"""LTA political calendar watcher.

Checks every source in watcher/sources.json and:
  * structured official sources (eSCRIBE council systems, .ics feeds, WordPress event
    calendars, schema.org event markup) -> upserted straight into data/events.json
  * everything else (party news pages, Elections Alberta notices, newspapers) -> read by
    the AI reader -> data/pending.json for a person to approve (watcher/review.py)

AI safeguards
  * The AI must quote the exact sentence it relied on. If that quote is not found word for
    word in the page text, the item is thrown away.
  * AI items never publish on their own.
  * Newspaper items keep only the facts (title, date, place) and a link, never article text.

Usage:
  python3 watcher/watch.py                 # check everything
  python3 watcher/watch.py --only abndp-events
  python3 watcher/watch.py --fixture-dir watcher/fixtures   # offline test run

Environment:
  ANTHROPIC_API_KEY   needed for ai_page / ai_news sources (skipped without it)
  LTA_AI_MODEL        defaults to claude-sonnet-5-5
"""
import argparse, datetime as dt, hashlib, html, json, os, pathlib, re, sys, time, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "watch_state.json"
AB = dt.timezone(dt.timedelta(hours=-6))  # Alberta: UTC-6 year-round since 2026
UA = "LTA-Political-Calendar/1.0 (+https://www.letstalkalberta.com; letstalkalberta@gmail.com)"
MODEL = os.environ.get("LTA_AI_MODEL", "claude-sonnet-5-5")
TODAY = dt.datetime.now(AB).date()
FIXTURES = pathlib.Path(os.environ["LTA_FIXTURES"]) if os.environ.get("LTA_FIXTURES") else None  # offline testing

# ---------------------------------------------------------------- io helpers
def _p(*a, **k):
    if sys.stdout: print(*a, **k)

def jload(p, default):
    try: return json.loads(pathlib.Path(p).read_text())
    except FileNotFoundError: return default
def jsave(p, obj): pathlib.Path(p).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")

def fetch(url, data=None, headers=None, timeout=30):
    if FIXTURES:
        f = FIXTURES / (hashlib.sha1(url.encode()).hexdigest()[:12] + ".txt")
        named = FIXTURES / (re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")[:80] + ".txt")
        for c in (named, f):
            if c.exists(): return c.read_text()
        raise FileNotFoundError(f"no fixture for {url} (expected {named.name})")
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")

def page_text(raw_html):
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw_html)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()

def slug(*parts):
    s = "-".join(str(p) for p in parts if p)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:90]

def iso(d):
    """Return ISO string with -06:00 offset for datetimes; plain date for dates."""
    if isinstance(d, dt.datetime):
        if d.tzinfo is None: d = d.replace(tzinfo=AB)
        return d.astimezone(AB).isoformat(timespec="minutes")
    return d.isoformat()

def base_event(src, **kw):
    ev = {
        "type": src.get("type", "other"), "level": src.get("level", "party"),
        "jurisdiction": src.get("jurisdiction", src.get("level")), "party": src.get("party"),
        "region": src.get("region"), "location": {}, "description": "", "candidates": [], "links": [],
        "source": {"name": src["name"], "url": src.get("page", src["url"]), "kind": "primary", "checked": TODAY.isoformat()},
        "status": "scheduled", "verified": True, "origin": "watcher:" + src["id"],
    }
    ev.update(kw)
    return ev

# ---------------------------------------------------------------- structured readers
def read_escribe(src):
    """eSCRIBE (used by Calgary and Edmonton). The public calendar page loads meetings from
    MeetingsCalendarView.aspx/GetCalendarMeetings. Field names below cover the variants seen
    in the wild; confirm against the live site on first run (status: needs_check)."""
    start, end = TODAY - dt.timedelta(days=7), TODAY + dt.timedelta(days=120)
    body = json.dumps({"calendarStartDate": start.isoformat(), "calendarEndDate": end.isoformat()}).encode()
    raw = fetch(src["url"].rstrip("/") + "/MeetingsCalendarView.aspx/GetCalendarMeetings", data=body,
                headers={"Content-Type": "application/json; charset=utf-8"})
    data = json.loads(raw)
    rows = data.get("d", data) if isinstance(data, dict) else data
    out = []
    for m in rows or []:
        name = m.get("MeetingName") or m.get("Name") or m.get("title")
        s = m.get("StartDate") or m.get("start")
        if not (name and s): continue
        sd = parse_any(s); ed = parse_any(m.get("EndDate") or m.get("end") or "") or None
        if not sd: continue
        mid = m.get("ID") or m.get("Id") or m.get("id")
        link = m.get("Url") or (f'{src["url"].rstrip("/")}/Meeting.aspx?Id={mid}&lang=English' if mid else src["url"])
        if link and link.startswith("/"): link = src["url"].rstrip("/") + link
        t = "committee" if re.search(r"committee|commission|board", name, re.I) and not re.search(r"council", name, re.I) else "council"
        out.append(base_event(src, id=slug(src["id"], name, sd.date()), title=f'{src.get("region","")} {name}'.strip(),
                              type=t, start=iso(sd), end=iso(ed) if ed else None,
                              location={"name": m.get("Location") or m.get("location") or ""},
                              description=f'Public {name} meeting. Agendas are posted on the city\'s meeting site before the meeting.',
                              links=[{"label": "Meeting page, agenda and live stream", "url": link}]))
    return out

def read_ics(src):
    raw = fetch(src["url"]); out = []
    raw = re.sub(r"\r?\n[ \t]", "", raw)  # unfold
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        f = {}
        for line in block.strip().splitlines():
            if ":" not in line: continue
            k, v = line.split(":", 1); f[k.split(";")[0].upper()] = (k, v.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";"))
        if "DTSTART" not in f or "SUMMARY" not in f: continue
        sd, allday = ics_dt(*f["DTSTART"]); ed, _ = ics_dt(*f["DTEND"]) if "DTEND" in f else (None, False)
        title = f["SUMMARY"][1].strip()
        ev = base_event(src, id=slug(src["id"], f.get("UID", ("", title))[1], sd), title=title,
                        start=iso(sd), end=iso(ed) if ed else None, all_day=allday,
                        location={"name": f.get("LOCATION", ("", ""))[1]},
                        description=f.get("DESCRIPTION", ("", ""))[1][:1500])
        if "URL" in f: ev["links"] = [{"label": "Event page", "url": f["URL"][1]}]
        out.append(ev)
    return out

def ics_dt(key, val):
    if "VALUE=DATE" in key or re.fullmatch(r"\d{8}", val):
        return dt.datetime.strptime(val[:8], "%Y%m%d").date(), True
    if val.endswith("Z"):
        return dt.datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc), False
    return dt.datetime.strptime(val[:15], "%Y%m%dT%H%M%S").replace(tzinfo=AB), False  # TZID local -> Alberta

def read_wp_tribe(src):
    """WordPress 'The Events Calendar' REST API."""
    url = f'{src["url"].rstrip("/")}/wp-json/tribe/events/v1/events?per_page=50&start_date={TODAY.isoformat()}'
    data = json.loads(fetch(url)); out = []
    for e in data.get("events", []):
        sd = parse_any(e.get("start_date")); ed = parse_any(e.get("end_date"))
        if not sd: continue
        v = e.get("venue") or {}
        title = html.unescape(e.get("title", "")).strip()
        out.append(base_event(src, id=slug(src["id"], e.get("id"), title), title=title, type=guess_type(title),
                              start=iso(sd), end=iso(ed) if ed else None, all_day=bool(e.get("all_day")),
                              location={"name": html.unescape(v.get("venue", "") or ""), "address": ", ".join(x for x in [v.get("address"), v.get("city")] if x)},
                              region=v.get("city") or src.get("region"),
                              description=page_text(e.get("description", ""))[:1500],
                              links=[{"label": "Event page", "url": e.get("url", src["url"])}]))
    return out

def read_jsonld(src):
    raw = fetch(src["url"]); out = []
    for blob in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', raw):
        try: data = json.loads(blob)
        except ValueError: continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if not isinstance(it, dict) or "Event" not in str(it.get("@type", "")): continue
            sd = parse_any(it.get("startDate")); ed = parse_any(it.get("endDate"))
            if not sd: continue
            loc = it.get("location") or {}
            loc = loc[0] if isinstance(loc, list) and loc else loc
            addr = loc.get("address") if isinstance(loc, dict) else ""
            if isinstance(addr, dict): addr = ", ".join(v for k, v in addr.items() if k != "@type" and isinstance(v, str))
            out.append(base_event(src, id=slug(src["id"], it.get("name"), sd), title=it.get("name", ""), type=guess_type(it.get("name", "")),
                                  start=iso(sd), end=iso(ed) if ed else None,
                                  location={"name": loc.get("name", "") if isinstance(loc, dict) else str(loc), "address": addr or ""},
                                  description=page_text(str(it.get("description", "")))[:1500],
                                  links=[{"label": "Event page", "url": it.get("url", src["url"])}]))
    return out

def parse_any(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s.replace("Z", "+0000"), fmt)
            return d if d.tzinfo else d.replace(tzinfo=AB)
        except ValueError: pass
    m = re.match(r"/Date\((\d+)", s)  # .NET JSON date
    if m: return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, AB)
    return None

def guess_type(title):
    t = title.lower()
    for k, v in (("annual general meeting", "agm"), ("agm", "agm"), ("nomination", "nomination"), ("convention", "convention"),
                 ("town hall", "town-hall"), ("leadership", "leadership"), ("by-election", "by-election"), ("byelection", "by-election"),
                 ("board meeting", "board"), ("council", "council")):
        if k in t: return v
    return "other"

# ---------------------------------------------------------------- AI reader
AI_TOOL = {
    "name": "report_events",
    "description": "Report political events in or affecting Alberta found in the page text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"},
                "type": {"type": "string", "enum": ["election", "by-election", "referendum", "agm", "convention", "nomination", "council", "board", "committee", "town-hall", "leadership", "deadline", "other"]},
                "level": {"type": "string", "enum": ["federal", "provincial", "municipal", "party"]},
                "party_name": {"type": "string", "description": "Exact party name if the event belongs to a party, else empty"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "HH:MM 24h Alberta time, or empty if not stated"},
                "end_time": {"type": "string"},
                "end_date": {"type": "string"},
                "location_name": {"type": "string"}, "address": {"type": "string"}, "region": {"type": "string"},
                "summary": {"type": "string", "description": "One or two plain sentences in your own words. Facts only."},
                "candidates": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "party_name": {"type": "string"}, "status": {"type": "string"}}, "required": ["name"]}},
                "event_url": {"type": "string", "description": "Link to the event's own page if present in the text, else empty"},
                "evidence_quote": {"type": "string", "description": "A short passage copied EXACTLY, character for character, from the page text that states the date of this event."},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]}
            }, "required": ["title", "type", "level", "date", "summary", "evidence_quote", "confidence"]}}
        }, "required": ["events"]
    }
}

AI_SYSTEM = """You extract political events for a non-partisan Alberta civic calendar run by Let's Talk Alberta.
Report only events that are explicitly stated in the page text with a specific date on or after {today}:
elections, by-elections, referendums, nomination deadlines, council and board meetings, party AGMs,
conventions, nomination meetings, leadership votes and public town halls connected to Alberta or to federal
politics in Alberta. Do not report fundraisers or social events unless they are a party's official meeting.
Rules:
- Never guess a date, time, place or candidate. If the text does not state it, leave the field empty.
- evidence_quote must be copied exactly from the text, character for character, and must contain the date.
- Times are Alberta local time.
- summary must be your own neutral wording: no opinions, no adjectives about parties or people.
- If there are no qualifying events, return an empty list."""

def ai_extract(src, text):
    if os.environ.get("LTA_FAKE_AI_FILE"):  # offline testing only: canned AI answers keyed by source id
        return json.loads(pathlib.Path(os.environ["LTA_FAKE_AI_FILE"]).read_text()).get(src["id"], [])
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        _p(f"  [skip] {src['id']}: ANTHROPIC_API_KEY not set"); return []
    text = text[:60000]
    hint = " This is a news site: report only events the articles announce, never the article's own content." if src["kind"] == "ai_news" else ""
    body = {
        "model": MODEL, "max_tokens": 4000,
        "system": AI_SYSTEM.format(today=TODAY.isoformat()) + hint,
        "tools": [AI_TOOL], "tool_choice": {"type": "tool", "name": "report_events"},
        "messages": [{"role": "user", "content": f"Source: {src['name']} ({src['url']})\nToday: {TODAY.isoformat()}\n\nPAGE TEXT:\n{text}"}],
    }
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 529) and attempt < 2: time.sleep(10 * (attempt + 1)); continue
            _p(f"  [error] AI call failed for {src['id']}: {e.code} {e.read()[:300]!r}"); return []
    for block in res.get("content", []):
        if block.get("type") == "tool_use":
            return block["input"].get("events", [])
    return []

def norm_ws(s): return re.sub(r"\s+", " ", s or "").strip().lower()

def ai_items_to_pending(src, items, text, parties):
    out, flat = [], norm_ws(text)
    for it in items:
        q = norm_ws(it.get("evidence_quote"))
        if not q or q not in flat:
            _p(f"  [drop] quote not found on page: {it.get('title')!r}"); continue
        try: d = dt.date.fromisoformat(it["date"])
        except (ValueError, KeyError): _p(f"  [drop] bad date: {it.get('title')!r}"); continue
        if d < TODAY: continue
        pid = match_party(it.get("party_name") or "", parties) or src.get("party")
        st = it.get("start_time") or ""
        if re.fullmatch(r"\d{1,2}:\d{2}", st):
            start = iso(dt.datetime.combine(d, dt.time.fromisoformat(st.zfill(5)), AB)); all_day = False
            et = it.get("end_time") or ""; ed = it.get("end_date") or it["date"]
            end = iso(dt.datetime.combine(dt.date.fromisoformat(ed), dt.time.fromisoformat(et.zfill(5)), AB)) if re.fullmatch(r"\d{1,2}:\d{2}", et) else None
        else:
            start, all_day, end = d.isoformat(), True, (it.get("end_date") or None)
        is_news = src["kind"] == "ai_news"
        ev = base_event(src, id=slug(pid or src["id"], it["title"], d), title=it["title"].strip(), type=it["type"],
                        level=it["level"], jurisdiction=it["level"] if it["level"] != "party" else "provincial",
                        party=pid, region=it.get("region") or src.get("region"), start=start, end=end, all_day=all_day,
                        location={"name": it.get("location_name", ""), "address": it.get("address", "")},
                        description=it.get("summary", ""),
                        candidates=[{"name": c["name"], "party": match_party(c.get("party_name", ""), parties), "affiliation": c.get("party_name", ""), "status": c.get("status", "")} for c in it.get("candidates", [])],
                        links=[{"label": "Event page", "url": it["event_url"]}] if it.get("event_url", "").startswith("http") else [],
                        verified=False, origin="ai:" + src["id"])
        ev["source"]["kind"] = "secondary" if is_news else "primary"
        ev["review"] = {"evidence_quote": it.get("evidence_quote"), "confidence": it.get("confidence"), "found": dt.datetime.now(AB).isoformat(timespec="minutes"), "model": MODEL}
        if not ev["end"]: ev.pop("end")
        out.append(ev)
    return out

def match_party(name, parties):
    n = norm_ws(name)
    if not n: return None
    for p in parties:
        if n in (norm_ws(p["name"]), norm_ws(p["short"]), p["id"]) or norm_ws(p["name"]) in n: return p["id"]
    if "ndp" in n or "new democrat" in n: return "ab-ndp" if "alberta" in n else ("ndp" if "canada" in n else "ab-ndp")
    if "united conservative" in n or n == "ucp": return "ucp"
    return None

# ---------------------------------------------------------------- shared pieces (used by run(), the hub and the PCs)
READERS = {"escribe": read_escribe, "ics": read_ics, "wp_tribe": read_wp_tribe, "jsonld": read_jsonld}

def refresh_today():
    """Long-running processes (hub, desktop app) must not keep yesterday's date."""
    global TODAY
    TODAY = dt.datetime.now(AB).date()

def check_source(src, last_hash=None):
    """Read one source. Returns one of:
       {"kind": "structured", "events": [...]}            official reader worked
       {"kind": "ai", "hash": h, "text": text, "url": u}  page changed; needs the AI reader
       {"kind": "unchanged", "hash": h}                   page text same as last time
    Raises on network errors so the caller can report the failure."""
    refresh_today()
    kind = src["kind"]
    if kind in READERS:
        try:
            return {"kind": "structured", "events": READERS[kind](src)}
        except Exception as e:
            if src.get("fallback") != "ai_page": raise
            _p(f"  structured reader failed ({e}); falling back to AI reader")
    url = src.get("page", src["url"]) if kind in READERS else src["url"]
    text = page_text(fetch(url))
    h = hashlib.sha256(norm_ws(text).encode()).hexdigest()
    if last_hash == h: return {"kind": "unchanged", "hash": h}
    return {"kind": "ai", "hash": h, "text": text, "url": url}

class Store:
    """events.json + pending.json + change log, with the merge rules in one place."""
    def __init__(self):
        refresh_today()
        self.events_doc = jload(DATA / "events.json", {"events": []})
        self.pending_doc = jload(DATA / "pending.json", {"pending": [], "rejected_ids": []})
        self.by_id = {e["id"]: e for e in self.events_doc["events"]}
        self.pend_ids = {e["id"] for e in self.pending_doc["pending"]} | set(self.pending_doc.get("rejected_ids", []))
        self.changes = []; self.added = self.updated = self.queued = 0

    def merge_structured(self, events):
        for ev in events:
            old = self.by_id.get(ev["id"])
            if old is None:
                self.by_id[ev["id"]] = ev; self.added += 1; self.changes.append({"kind": "new", "id": ev["id"]})
            elif old.get("origin", "").startswith("watcher:") and (old.get("start"), old.get("end"), old.get("location")) != (ev.get("start"), ev.get("end"), ev.get("location")):
                ev["history"] = old.get("history", []) + [{"changed": TODAY.isoformat(), "was": {"start": old.get("start"), "end": old.get("end"), "location": old.get("location")}}]
                self.by_id[ev["id"]] = ev; self.updated += 1; self.changes.append({"kind": "changed", "id": ev["id"]})
            elif old.get("origin", "").startswith("watcher:"):
                old["source"]["checked"] = TODAY.isoformat()

    def queue(self, items):
        for ev in items:
            if ev["id"] in self.by_id or ev["id"] in self.pend_ids: continue
            self.pending_doc["pending"].append(ev); self.pend_ids.add(ev["id"]); self.queued += 1

    def save(self):
        cutoff = TODAY - dt.timedelta(days=60)
        def keep(e):
            if not e.get("origin", "").startswith("watcher:"): return True
            try: return dt.date.fromisoformat(str(e.get("end") or e["start"])[:10]) >= cutoff
            except ValueError: return True
        self.events_doc["events"] = sorted([e for e in self.by_id.values() if keep(e)], key=lambda e: str(e["start"]))
        self.events_doc["generated"] = dt.datetime.now(AB).isoformat(timespec="seconds")
        jsave(DATA / "events.json", self.events_doc)
        jsave(DATA / "pending.json", self.pending_doc)
        if self.changes:
            log = jload(DATA / "changes.json", {"changes": []})
            stamp = dt.datetime.now(AB).isoformat(timespec="minutes")
            log["changes"] = (log["changes"] + [dict(c, at=stamp) for c in self.changes])[-500:]
            jsave(DATA / "changes.json", log)

def ai_process(src, text, store, parties):
    """Run the AI reader on page text and queue verified items. Returns number queued."""
    before = store.queued
    store.queue(ai_items_to_pending(src, ai_extract(src, text), text, parties))
    return store.queued - before

# ---------------------------------------------------------------- main (stand-alone / server cron)
def run(only=None, stale_hours=None):
    sources = jload(ROOT / "watcher/sources.json", {})["sources"]
    parties = jload(DATA / "parties.json", {"parties": []})["parties"]
    state = jload(STATE, {})
    store = Store()
    report = {"ok": [], "failed": [], "unchanged": []}
    now = dt.datetime.now(AB)
    for src in sources:
        if only and src["id"] not in only: continue
        st = state.get(src["id"], {})
        if stale_hours and st.get("checked") and now - dt.datetime.fromisoformat(st["checked"]) < dt.timedelta(hours=stale_hours):
            continue  # a PC checked it recently
        _p(f"- {src['id']} ({src['kind']})")
        try:
            r = check_source(src, st.get("hash"))
            if r["kind"] == "structured": store.merge_structured(r["events"])
            elif r["kind"] == "unchanged": report["unchanged"].append(src["id"]); _p("  unchanged since last check")
            else: ai_process(src, r["text"], store, parties)
            state[src["id"]] = {"hash": r.get("hash", st.get("hash")), "checked": now.isoformat(timespec="minutes"), "by": "server"}
            report["ok"].append(src["id"])
        except Exception as e:
            _p(f"  [failed] {type(e).__name__}: {e}")
            report["failed"].append({"id": src["id"], "error": f"{type(e).__name__}: {e}"})
    store.save(); jsave(STATE, state)
    report.update(added=store.added, updated=store.updated, queued=store.queued, checked_at=now.isoformat(timespec="minutes"))
    jsave(DATA / "watch_report.json", report)
    _p(json.dumps({k: v for k, v in report.items() if k != "unchanged"}, indent=1))
    return report

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--fixture-dir")
    ap.add_argument("--stale-hours", type=float, help="skip sources a PC checked within this many hours (server backup mode)")
    a = ap.parse_args()
    if a.fixture_dir: FIXTURES = pathlib.Path(a.fixture_dir)
    run(set(a.only) if a.only else None, a.stale_hours)
