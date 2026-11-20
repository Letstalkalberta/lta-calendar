"""Start LTA Watcher (in the background) when the user signs in to Windows."""
import os, sys
KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "LTA Watcher"

def _cmd():
    exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" -m lta_watcher'
    return (f'"{exe}"' if getattr(sys, "frozen", False) else exe) + " --background"

def is_enabled():
    if os.name != "nt": return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k: winreg.QueryValueEx(k, NAME); return True
    except OSError: return False

def set_enabled(on):
    if os.name != "nt": return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on: winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, _cmd())
        else:
            try: winreg.DeleteValue(k, NAME)
            except OSError: pass
