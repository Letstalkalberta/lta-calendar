"""Automatic updates from GitHub Releases.

The build pipeline attaches "LTA-Watcher-Setup-<version>.exe" to each release. This checks the
latest release, and if it's newer, downloads the installer and runs it silently. The installer
closes the running app, updates it and starts it again.
"""
import json, os, subprocess, sys, tempfile, urllib.request
from . import UPDATE_REPO, VERSION, APP_NAME

def _v(s): return tuple(int(x) for x in s.lstrip("v").split(".")[:3] if x.isdigit())

def latest():
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", headers={"User-Agent": APP_NAME, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r: rel = json.loads(r.read())
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe") and "setup" in a["name"].lower()), None)
    return rel.get("tag_name", ""), asset

def check_and_install(install=False):
    try: tag, asset = latest()
    except Exception as e: return {"ok": False, "message": f"Couldn't check for updates ({e})."}
    if not tag or _v(tag) <= _v(VERSION): return {"ok": True, "current": VERSION, "latest": tag or VERSION, "update": False, "message": "You have the latest version."}
    if not install: return {"ok": True, "current": VERSION, "latest": tag, "update": True, "message": f"Version {tag.lstrip('v')} is available."}
    if not asset: return {"ok": False, "message": f"Version {tag.lstrip('v')} is published, but its installer is still being built. Try again in a few minutes."}
    if os.name != "nt": return {"ok": False, "message": "Updates install on Windows only."}
    path = os.path.join(tempfile.gettempdir(), asset["name"])
    urllib.request.urlretrieve(asset["browser_download_url"], path)
    subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], close_fds=True)
    return {"ok": True, "message": "Installing the update. LTA Watcher will restart in a moment.", "installing": True}
