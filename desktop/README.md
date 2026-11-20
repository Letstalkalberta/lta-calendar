# LTA Watcher (Windows app)

A small Windows app for LTA PCs. It sits in the system tray, checks political websites in the background, and sends what it finds to the LTA hub (the calendar server). Reviewers use the same app to approve events before they go on the public calendar.

## How the pieces fit

```
 LTA PC (LTA Watcher)  ─┐
 LTA PC (LTA Watcher)  ─┼──►  LTA hub (calendar server)  ──►  calendar on letstalkalberta.com
 LTA PC (LTA Watcher)  ─┘        │  runs the AI reader with LTA's key         Google Calendar feeds
                                 │  review queue, sign-ins, audit log         ride board, phone app
                                 └─ backup: checks any site no PC has reached in 24 hours
```

- **The PCs never talk to each other directly.** Each one checks in with the hub every 5 minutes. The hub gives each website to exactly one online PC, and when a PC goes offline its sites move to the others. This works through home routers and firewalls with no setup.
- **Official calendars** (Calgary and Edmonton council systems, party event calendars) are read on the PC and published right away.
- **Everything else** (party news pages, Elections Alberta, newspapers): when the page text changes, the PC sends it to the hub. The hub runs the AI reader with LTA's one key, throws out anything the AI can't back with an exact quote from the page, and puts the rest in the review queue.
- **Reviewers sign in by name.** Approving needs a tick confirming they checked the source. Every action is logged with a name and time.

## The app

| Tab | What it's for |
|---|---|
| Review | Events the AI found. Open the source, fix details, approve or reject |
| Add an event | LTA town halls, or anything confirmed by hand. A source link is required |
| Activity | What this PC checked recently, which LTA PCs are online, AI usage today, recent actions |
| Sources | Every website being watched, which PC it's assigned to, last result. Admins can add or turn off sites |
| Settings | Sign in/out, change password, start with Windows, check for updates, disconnect, quit |

Closing the window keeps it running in the tray. Right-click the tray icon to check now, pause or quit. When new items are waiting for review, signed-in reviewers get a Windows notification.

## Setting up (admin, on the server)

```bash
python3 server/hub_admin.py add-admin "Christopher Scott" cjscottx@gmail.com
python3 server/hub_admin.py add-reviewer "Name" email@example.com   # prints a temporary password
python3 server/hub_admin.py pc-code                                  # one-time code for each new PC
python3 server/hub_admin.py pcs | users | disable-pc <id> | deactivate <email> | reset-password <email>
```

## Installing on an LTA PC

1. Download `LTA-Watcher-Setup-x.y.z.exe` from LTA's GitHub releases.
2. Run it. It installs for the current Windows user (no administrator password needed), and by default starts with Windows.
3. Enter the setup code from the admin and a name for the PC (e.g. "Mirror office laptop").
4. Reviewers sign in under Review and choose their own password.

**Windows warning on first install.** Until LTA buys a code-signing certificate, Windows SmartScreen shows "Windows protected your PC". Click **More info → Run anyway**. Signing removes that warning; it's a paid yearly or monthly service and can be added to the build later.

## Building the installer

Automatic, on GitHub (once LTA's account and repository exist):
- Every push to `main` builds the installer and keeps it under the workflow run's **Artifacts** for testing.
- To release: bump `VERSION` in `desktop/lta_watcher/__init__.py` and commit. Then on github.com go to Releases → Draft a new release, type the tag `v` + the version (e.g. `v1.0.1`), and click Publish. GitHub builds the installer, starts the app as a smoke test, and attaches it to the release. PCs find the update under Settings → Check for updates.
- Set `UPDATE_REPO` in `desktop/lta_watcher/__init__.py` to the real `owner/repository` name.

By hand, on any Windows PC: `powershell -ExecutionPolicy Bypass -File desktop\build.ps1` (needs Python 3.12 and Inno Setup 6).

## Where things are kept on a PC

`%APPDATA%\LTA Watcher\` holds `config.json` (hub address, this PC's ID and access token, the reviewer's sign-in) and `log.txt`. The AI key is never stored on a PC. To retire a PC, run `disable-pc` on the server; its token stops working immediately.

## Testing without Windows

```bash
cd desktop && python3 -m lta_watcher --headless     # opens the screen in a web browser; no tray icon
```
Test switches: `LTA_WATCHER_HOME` (separate settings folder), `LTA_LOCK_PORT` (run two copies), `LTA_FIXTURES` (read saved pages instead of the web), `LTA_HEARTBEAT_SEC`.

## Tested so far

On Linux with two simulated PCs and a hub, using saved pages and a stand-in AI answer: setup codes (wrong and reused codes refused), work split between the PCs and moved when one went offline, council meetings published, the AI's real find queued while a made-up one was thrown out, reviewer sign-in with forced password change, approve with edits (blocked until the "I checked the source" box is ticked), hand-added event (blocked without a source link), and the activity, sources and audit screens.

**Not yet tested:** the Windows build itself (tray icon, window, start-with-Windows, installer, auto-update). These need Windows. The GitHub workflow builds the app and smoke-tests it on Windows the first time it runs.
