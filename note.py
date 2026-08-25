"""
Remove all "note" fields from monitors.json and overwrite the file in place.

Also repairs a known encoding bug: earlier bot versions saved monitors.json
without forcing UTF-8, so on Windows it got written as cp1252 — corrupting
any "—" (em dash) characters in card labels. This script recovers the file
either way and always re-saves it as proper UTF-8.

Usage:
    1. Put this script in the SAME FOLDER as your monitors.json
       (or edit MONITORS_PATH below to point to it).
    2. python strip_notes.py
"""

import json

MONITORS_PATH = "monitors.json"

try:
    with open(MONITORS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
except UnicodeDecodeError:
    # File was previously saved using the OS default encoding (cp1252 on
    # Windows) instead of UTF-8 — cp1252 can read it back correctly.
    print("⚠️  File wasn't valid UTF-8 — re-reading as cp1252 (Windows default) instead.")
    with open(MONITORS_PATH, "r", encoding="cp1252") as f:
        data = json.load(f)

removed = 0
for card in data.values():
    for user_prefs in card.get("users", {}).values():
        if "note" in user_prefs:
            del user_prefs["note"]
            removed += 1

with open(MONITORS_PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"✅ Removed {removed} 'note' field(s) from {MONITORS_PATH}.")
print("✅ File re-saved as UTF-8 — any '—' characters should look correct now.")