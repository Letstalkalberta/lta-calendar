#!/usr/bin/env python3
"""Build the NationBuilder-ready calendar page with the current data baked in.

The page loads live data from the calendar server when CONFIG.apiBase is set.
The baked-in copy is the fallback (and what the page shows before the server exists).

Outputs:
  web/lta-political-calendar.html          full page (for previewing)
  web/lta-political-calendar.nationbuilder.html   only the block to paste into NationBuilder
"""
import json, pathlib, re

ROOT = pathlib.Path(__file__).parent
tpl = (ROOT / "web/calendar.template.html").read_text()
events = json.loads((ROOT / "data/events.json").read_text())
parties = json.loads((ROOT / "data/parties.json").read_text())

def js(obj):
    # Safe to embed inside <script>: stop "</script>" and line separators from breaking out.
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

page = tpl.replace('/*__EVENTS__*/{"events":[]}', js(events)).replace('/*__PARTIES__*/{"parties":[]}', js(parties))
(ROOT / "web/lta-political-calendar.html").write_text(page)

m = re.search(r"<!-- =+ LTA POLITICAL CALENDAR — START.*?<!-- =+ LTA POLITICAL CALENDAR — END =+ -->", page, re.S)
(ROOT / "web/lta-political-calendar.nationbuilder.html").write_text(m.group(0) + "\n")
print("built", len(events["events"]), "events,", len(parties["parties"]), "parties")
