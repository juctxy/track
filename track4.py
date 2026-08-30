"""
Lumina Card Pool Monitor — Discord Bot
=======================================
Commands:
  /monitor      — track one or more cards (by ID, or search by name) + rarity alerts
  /find         — look up cards by ID or name, no tracking
  /status       — see all YOUR monitored cards
  /stop         — stop tracking a card (with autocomplete)
  /stopall      — stop tracking ALL your cards at once
  /setchannel   — (admin) lock the bot to the current channel
  /unlock       — (admin) remove the channel restriction
  /purgeuser    — (admin) delete ALL messages from a given user ID, server-wide
  /join         — join a voice channel and stay (voice_feature.py)
  /leave        — disconnect from voice (voice_feature.py)
  /say          — make the bot send a message (say.py)
  /upload       — (bot owner only) save + push monitors.json / lumina_cards.csv to git now

Setup:
  1. pip install discord.py python-dotenv aiohttp
  2. Create a file named ".env" in the SAME FOLDER as this script, containing:
       DISCORD_BOT_TOKEN=your-real-token-here
     Never share this .env file or commit it to GitHub.
     If using git, add ".env" to your .gitignore file.
  3. python track.py
"""

import asyncio
import csv
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from urllib.error import HTTPError, URLError

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()  # reads the .env file in this folder and loads it into os.environ —
                # MUST run before importing AI, since AI.py reads GEMINI_API_KEY
                # from the environment the moment it's imported.

from voice import setup_voice_commands  # /join, /leave — defined in their own file
from say import setup_say_commands      # /say — defined in its own file 
from AI import handle_ai_message        # AI auto-chat — defined in its own file

# ── Settings ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
INTERVAL  = 3      # seconds between pool polls, per card
SYNC_INTERVAL = 360000  # seconds between catalog syncs (~1 hour) — much less frequent than pool polling

# Max simultaneous pool-fetch requests in flight at once, across ALL tracked
# cards. Combined with the per-card stagger in poll_loop(), this keeps our
# request pattern smooth instead of bursty — the site's rate limiter cares
# about how many requests land in the same instant, not just the average
# rate, so this is what actually reduces 429s (see fetch_pool + poll_loop).
POOL_FETCH_CONCURRENCY = 3

CONFIG_PATH    = "bot_config.json"  # stores the locked channel id per guild
MONITORS_PATH  = "monitors.json"    # stores tracked cards + subscribers, survives restarts
CARDS_CSV_PATH = "lumina_cards.csv" # local card catalog, appended to by catalog_sync_loop
MAIN_PATH = "track4.py"  # used in /upload to show which file triggered the git push

# ── Git sync (share data files between two people running the bot) ──────────
#
# Model this supports: only ONE of you runs the bot at a time (a "baton
# pass"). Whoever starts the bot pulls the latest data first; whoever stops
# it pushes their final state back. This is NOT safe for two bot processes
# running simultaneously against the same repo — see the chat writeup for
# why.
GIT_AUTO_SYNC     = True   # flip to False to disable all git pull/push behavior
GIT_PUSH_INTERVAL = 300    # seconds between periodic "push if changed" checks while running

# Every path listed here gets committed+pushed together as one commit, and
# pulled together at startup. Add more paths here if you want other files
# synced the same way (e.g. bot_config.json), but keep .env OUT of this list
# forever — see the git-history leak earlier in this conversation for why.
GIT_SYNC_PATHS = [MONITORS_PATH, CARDS_CSV_PATH, MAIN_PATH]

# Subset of GIT_SYNC_PATHS that should ALWAYS defer to whatever's on
# GitHub when starting up — any local uncommitted edit to these gets
# discarded before pulling, instead of being stashed-and-reapplied. These
# are runtime data files nobody should be hand-editing between runs; if
# you (or a test) left a stray edit sitting in one, it should lose to the
# real synced state, not silently survive a pull. track4.py is
# deliberately NOT in this list, since you might have genuine in-progress
# code edits open when you start the bot.
GIT_HARD_RESET_ON_PULL = [MONITORS_PATH, CARDS_CSV_PATH]

# ─────────────────────────────────────────────────────────────────────────────

POOL_URL       = "https://luminabot.net/api/cards/pool?cardCatalogId={card_id}"
CARDS_LIST_URL = "https://luminabot.net/api/cards"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "application/json",
    "Referer":    "https://luminabot.net/cards",
}

# Rarity key mapping from API
# "3"=R  "4"=SR  "5"=SSR  "6"=LR  "7"=UR
RARITIES = {
    "3": ("R",   "🟦"),
    "4": ("SR",  "🟪"),
    "5": ("SSR", "🟨"),
    "6": ("LR",  "⬛"),
    "7": ("UR",  "🟥"),
}

# Display order is independent from the API key mapping above —
# we always want to show R, SR, SSR, UR, LR in that order regardless
# of which numeric key each rarity happens to use.
DISPLAY_ORDER = ["3", "4", "5", "7", "6"]  # R, SR, SSR, UR, LR

# Convenience: build display string for a counts dict using RARITIES as single source of truth
def fmt_pool(by_rar: dict) -> str:
    return "  ".join(
        f"{RARITIES[key][1]} {RARITIES[key][0]} `{by_rar.get(key, 0)}`"
        for key in DISPLAY_ORDER
    )

# monitors[card_id] = {
#     "prev":  counts_dict,
#     "task":  asyncio.Task,            # not persisted, rebuilt on startup
#     "users": {
#         user_id: {
#             "note":       str,
#             "alert_r", "alert_sr", "alert_ssr", "alert_ur", "alert_lr": bool,
#             "channel_id": int,        # resolved to a TextChannel when sending
#         }
#     }
# }
monitors: dict[str, dict] = {}

# guild_channel_locks[guild_id] = channel_id (int) that the bot is restricted to.
# If a guild has no entry, the bot works in any channel of that guild.
guild_channel_locks: dict[str, int] = {}


# ── Atomic JSON writes ────────────────────────────────────────────────────────
#
# Writing straight to CONFIG_PATH/MONITORS_PATH with open(path, "w") is NOT
# safe: "w" mode truncates the file to empty the instant it's opened, before
# any new data is written. If json.dump() then fails partway through (bad
# data, disk full, process killed, anything) the file is left empty/corrupt
# and the previous good data is gone for good — this is exactly what caused
# a prior data-loss incident (see conversation/changelog).
#
# _atomic_write_json() avoids this: it writes the complete new file under a
# temp name in the same directory, and only swaps it into place with
# os.replace() once the write has fully succeeded. os.replace() is atomic on
# the same filesystem, so the real path is always either the old good file
# or the new good file — never a truncated/partial one.

def _atomic_write_json(path: str, data, compact: bool = False) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if compact:
                # No indent, no spaces after ',' / ':' — purely a file-size
                # optimization for files we never hand-edit (monitors.json).
                # Cuts the on-disk size roughly in half vs indent=2 alone.
                json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
            else:
                json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)  # atomic swap, same filesystem
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# ── Git sync helpers ──────────────────────────────────────────────────────────
#
# Deliberately dumb and defensive: this is a convenience for a two-person
# "one of us runs it at a time" workflow, not a general sync engine. It
# must NEVER crash the bot or block the event loop — a failed git command
# just gets printed and ignored, same philosophy as the JSON save helpers.

def _run_git(*args: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a git command in the script's own directory. Returns (ok, output)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except Exception as e:
        return False, repr(e)


def git_pull_monitors_sync() -> None:
    """Call ONCE at startup, before anything reads monitors.json,
    lumina_cards.csv, or track4.py. Uses --autostash so any leftover
    local edit (e.g. to track4.py) doesn't block the pull, and --rebase
    to avoid merge-commit noise for these files."""
    if not GIT_AUTO_SYNC:
        return

    # Discard any local uncommitted edits to the "always trust GitHub"
    # data files BEFORE pulling. Without this, --autostash below would
    # protect a stray local edit by stashing it, pulling, then reapplying
    # it on top — silently undoing whatever the pull just brought in.
    for path in GIT_HARD_RESET_ON_PULL:
        ok, out = _run_git("checkout", "--", path)
        if not ok and out.strip():
            # Fine if the file doesn't exist yet / isn't tracked yet —
            # only worth printing anything else unexpected.
            if "did not match any file" not in out and "pathspec" not in out:
                print(f"⚠️  Couldn't reset local {path} before pull: {out}")

    ok, out = _run_git("pull", "--rebase", "--autostash")
    if ok:
        print("🔄  git pull: up to date with remote before starting.")
    else:
        # Not fatal — bot still starts with whatever's on disk locally.
        # Common causes: no git repo here, no remote configured yet, or
        # no network. Printed so it's not silent.
        print(f"⚠️  git pull failed (continuing with local files): {out}")


def _git_push_monitors_sync(reason: str) -> None:
    if not GIT_AUTO_SYNC:
        return
    # Only stage paths that actually changed (tracked-and-modified, or
    # brand new/untracked) — avoids empty commits from the periodic check
    # firing when nothing's new.
    changed_paths = []
    for path in GIT_SYNC_PATHS:
        if not os.path.exists(path):
            continue
        unchanged, _ = _run_git("diff", "--quiet", "--", path)
        # git diff --quiet exits 0 = no changes. _run_git's `ok` is True
        # only on exit 0, so "unchanged" here really means "no diff".
        status_ok, status_out = _run_git("status", "--porcelain", "--", path)
        is_untracked = status_ok and status_out.strip().startswith("??")
        if not unchanged or is_untracked:
            changed_paths.append(path)

    if not changed_paths:
        return  # nothing to push

    _run_git("add", "--", *changed_paths)
    files_desc = ", ".join(changed_paths)
    ok, out = _run_git("commit", "-m", f"Sync {files_desc} ({reason})")
    if not ok:
        if "nothing to commit" not in out.lower():
            print(f"⚠️  git commit failed: {out}")
        return
    ok, out = _run_git("push")
    if ok:
        print(f"⬆️   Pushed {files_desc} to git ({reason}).")
    else:
        print(f"⚠️  git push failed — your changes are committed locally but NOT on GitHub yet: {out}")


async def git_push_monitors(reason: str) -> None:
    await asyncio.get_running_loop().run_in_executor(None, _git_push_monitors_sync, reason)


async def git_sync_loop():
    """Periodic safety net: pushes monitors.json every GIT_PUSH_INTERVAL
    seconds if it changed. This exists so that if the bot process dies
    uncleanly (crash, power loss, kill -9) you don't lose more than
    ~GIT_PUSH_INTERVAL seconds of monitor changes — the on-shutdown push
    in the __main__ block is what handles the normal Ctrl+C / /stop case."""
    if not GIT_AUTO_SYNC:
        return
    while True:
        await asyncio.sleep(GIT_PUSH_INTERVAL)
        await git_push_monitors("periodic")


# ── Config persistence ────────────────────────────────────────────────────────

def load_config():
    global guild_channel_locks
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            guild_channel_locks = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        guild_channel_locks = {}


def _save_config_sync():
    try:
        _atomic_write_json(CONFIG_PATH, guild_channel_locks)
    except Exception as e:
        # Broad except is intentional: previously this only caught OSError,
        # so an unexpected error (e.g. an encoding issue) would propagate
        # out of the executor thread uncaught. Now it's logged and swallowed
        # so it can never crash the calling task, and the OLD file on disk
        # is left untouched thanks to _atomic_write_json.
        print(f"⚠️  Failed to save {CONFIG_PATH}: {e!r}")


async def save_config():
    # Runs the blocking file write in a thread instead of directly on the
    # event loop — low-frequency (admin-only), but free to fix and keeps
    # every disk write in this file off the hot path consistently.
    await asyncio.get_running_loop().run_in_executor(None, _save_config_sync)


# ── Monitors persistence ──────────────────────────────────────────────────────
#
# save_monitors() is the ONE place allowed to actually touch disk — it's
# lock-guarded so two callers can never interleave writes to the same file,
# and it always runs off the event loop via an executor thread so a slow
# disk (or a big monitors.json) can't stall every other command / poll_loop
# in the bot while it writes.
#
# poll_loop (the hottest, highest-frequency caller — fires on every
# detected pool change) does NOT call save_monitors() directly. It calls
# mark_monitors_dirty() instead, which just sets a flag (no I/O at all),
# and monitors_autosave_loop() coalesces however many changes land in a
# ~2s window into a single write. This does NOT delay the actual Discord
# ping — that's sent immediately regardless — it only delays *persistence*,
# which is invisible to users. Trade-off: if the process crashes within
# that ~2s window, the very latest "prev" snapshot for whatever just
# changed might not have made it to disk yet. Since pool counts only ever
# go up, the only realistic consequence is a duplicate ping for that one
# card on the next poll after a restart — not a missed notification, not
# data loss. User-facing commands (/monitor, /stop, /stopall) are NOT
# debounced — they call save_monitors() directly so a just-registered
# monitor can't vanish to a mistimed crash.

_monitors_dirty      = False
_monitors_save_lock  = asyncio.Lock()

# The 5 alert booleans a user sets per card, packed into one small int
# instead of 5 separate "alert_x": true/false keys — see _pack_alerts().
ALERT_KEYS = ("alert_r", "alert_sr", "alert_ssr", "alert_ur", "alert_lr")


def _pack_alerts(prefs: dict) -> int:
    bits = 0
    for i, key in enumerate(ALERT_KEYS):
        if prefs.get(key):
            bits |= (1 << i)
    return bits


def _unpack_alerts(bits) -> dict:
    bits = bits or 0
    return {key: bool(bits & (1 << i)) for i, key in enumerate(ALERT_KEYS)}


# ── monitors.json on-disk schema ────────────────────────────────────────────
#
# The in-memory `monitors` dict stays card_id -> {prev, users, label, ...}
# (that's the shape everything else in this file — poll_loop, /status,
# /stop, autocomplete — already expects, so none of that has to change).
#
# What changes is how it's SERIALIZED. The old format nested a full "users"
# dict under every single card, so a user_id got written out once per card
# they track — for someone tracking 20 cards, their 18-digit Discord ID was
# repeated 20 times. The current format flips that around:
#
#   {
#     "users": {
#       "<user_id>": {
#         "<card_id>": {"label": "...", "a": <bitmask>, "ch": <channel_id>, "n"?: <note>}
#       }
#     },
#     "cards": { "<card_id>": {"prev", "label", "character", "series"} }
#   }
#
# Each user_id is written exactly ONCE on disk no matter how many cards they
# track, and each card's shared/changing data (prev counts, character,
# series) lives in "cards" exactly once. The 5 alert booleans are bit-packed
# into one int, and empty notes are dropped entirely.
#
# DISPLAY: rather than handing this to json.dump(indent=2) — which would
# blow every single key (label/a/ch) onto its own line and make a 40-card
# user span 160+ lines — _render_monitors_json() below builds the text by
# hand: one line per user_id header, one line per card underneath it
# (compact inner object), and the "cards" reference table at the bottom.
# This is still 100% valid JSON (json.load parses it identically either
# way) — only the whitespace/layout is custom, purely so a human opening
# the file can actually read it: scroll to a user_id, see every card they
# track, one per row, label right there on the line.

def _serialize_monitors() -> dict:
    cards: dict[str, dict] = {}
    users: dict[str, dict] = {}
    for card_id, mon in monitors.items():
        cards[card_id] = {
            "prev":      mon.get("prev"),
            "label":     mon.get("label"),
            "character": mon.get("character"),
            "series":    mon.get("series"),
        }
        for user_id, prefs in mon.get("users", {}).items():
            # "label" is duplicated here (also lives in cards[card_id]) on
            # purpose — it's what lets a row like `"36854": {...}` under a
            # user_id read as "Lee Hakhyun #1" without flipping down to the
            # cards table to look it up. Small size cost, worth it for
            # readability, which is the whole point of this layout.
            entry = {
                "label": mon.get("label"),
                "a":     _pack_alerts(prefs),
                "ch":    prefs.get("channel_id"),
            }
            note = prefs.get("note")
            if note:  # skip the key entirely when there's no note to save
                entry["n"] = note
            users.setdefault(user_id, {})[card_id] = entry
    return {"cards": cards, "users": users}


def _render_monitors_json(data: dict) -> str:
    """Hand-built pretty-printer: one user_id per header line, one card per
    indented line underneath it, still valid JSON throughout (each
    inner object is produced by json.dumps, just placed on its own line)."""
    cards = data.get("cards", {})
    users = data.get("users", {})

    lines = ["{"]

    lines.append('  "users": {')
    user_ids = list(users.keys())
    for ui, user_id in enumerate(user_ids):
        user_comma = "," if ui < len(user_ids) - 1 else ""
        card_map = users[user_id]
        lines.append(f'    "{user_id}": {{')
        card_ids = list(card_map.keys())
        for ci, card_id in enumerate(card_ids):
            card_comma = "," if ci < len(card_ids) - 1 else ""
            entry_json = json.dumps(card_map[card_id], separators=(",", ":"), ensure_ascii=False)
            lines.append(f'      "{card_id}": {entry_json}{card_comma}')
        lines.append(f'    }}{user_comma}')
    lines.append('  },')

    lines.append('  "cards": {')
    card_ids = list(cards.keys())
    for ci, card_id in enumerate(card_ids):
        comma = "," if ci < len(card_ids) - 1 else ""
        card_json = json.dumps(cards[card_id], separators=(",", ":"), ensure_ascii=False)
        lines.append(f'    "{card_id}": {card_json}{comma}')
    lines.append('  }')

    lines.append("}")
    return "\n".join(lines)


def _save_monitors_sync():
    """Blocking implementation — always call through save_monitors(),
    never directly, so it stays off the event loop and lock-protected."""
    try:
        text = _render_monitors_json(_serialize_monitors())
        directory = os.path.dirname(os.path.abspath(MONITORS_PATH)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, MONITORS_PATH)  # atomic swap, same as _atomic_write_json
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        # Broad except is intentional — see _save_config_sync for why.
        # The atomic temp-file + os.replace above guarantees this failure
        # leaves the existing monitors.json on disk untouched.
        print(f"⚠️  Failed to save {MONITORS_PATH}: {e!r}")


async def save_monitors():
    """Write monitors.json now (off the event loop, lock-guarded). Use this
    from user-facing commands. For the poll_loop hot path, use
    mark_monitors_dirty() instead — see module note above."""
    global _monitors_dirty
    async with _monitors_save_lock:
        await asyncio.get_running_loop().run_in_executor(None, _save_monitors_sync)
    _monitors_dirty = False


def mark_monitors_dirty():
    """Zero-I/O: flags monitors.json as stale. monitors_autosave_loop()
    picks this up and coalesces it with any other changes in the same
    ~2s window into a single write, instead of one blocking write per
    individual card change."""
    global _monitors_dirty
    _monitors_dirty = True


async def monitors_autosave_loop():
    while True:
        await asyncio.sleep(2)
        if _monitors_dirty:
            await save_monitors()


def load_monitors_raw() -> dict:
    try:
        with open(MONITORS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_cards_and_users_raw() -> tuple[dict, dict]:
    """Returns (cards_raw, users_raw) in the new schema's shape, reading
    either a new-format monitors.json or migrating an old-format one
    (card_id -> {prev, users: {user_id: {...}}, label, character, series})
    so upgrading the bot doesn't lose anyone's existing monitors."""
    raw = load_monitors_raw()

    if "cards" in raw or "users" in raw:
        return raw.get("cards", {}), raw.get("users", {})

    # Old format — migrate in memory. Nothing is written to disk here;
    # the very next save_monitors() call will persist it in the new shape.
    cards_raw: dict[str, dict] = {}
    users_raw: dict[str, dict] = {}
    for card_id, data in raw.items():
        cards_raw[card_id] = {
            "prev":      data.get("prev"),
            "label":     data.get("label"),
            "character": data.get("character"),
            "series":    data.get("series"),
        }
        for user_id, prefs in data.get("users", {}).items():
            entry = {"a": _pack_alerts(prefs), "ch": prefs.get("channel_id")}
            note = prefs.get("note")
            if note:
                entry["n"] = note
            users_raw.setdefault(user_id, {})[card_id] = entry
    return cards_raw, users_raw


async def restore_monitors():
    """Called once on startup. Rebuilds the monitors dict from disk and
    restarts a poll_loop task for every card that was being tracked."""
    cards_raw, users_raw = _load_cards_and_users_raw()

    # Invert users_raw (user_id -> {card_id: entry}) back into the
    # per-card "users" dict shape the rest of the file works with
    # (card_id -> {user_id: prefs}).
    card_users: dict[str, dict] = {}
    for user_id, card_map in users_raw.items():
        for card_id, entry in card_map.items():
            prefs = _unpack_alerts(entry.get("a"))
            prefs["note"]       = entry.get("n", "")
            prefs["channel_id"] = entry.get("ch")
            card_users.setdefault(card_id, {})[user_id] = prefs

    for card_id, data in cards_raw.items():
        # Prefer a fresh lookup against the current CSV (in case it was
        # updated since the bot last saved), falling back to the values
        # stored on disk if the id has since disappeared from the catalog.
        card = lookup_card_by_id(card_id)
        monitors[card_id] = {
            "prev":      data.get("prev"),
            "users":     card_users.get(card_id, {}),
            "label":     card_display(card, fallback_id=card_id) if card else data.get("label"),
            "character": card.get("character") if card else data.get("character"),
            "series":    card.get("series")    if card else data.get("series"),
        }
        task = client.loop.create_task(poll_loop(card_id, client))
        monitors[card_id]["task"] = task
    if cards_raw:
        print(f"🔁  Restored {len(cards_raw)} monitored card(s) from {MONITORS_PATH}")


# ── API fetch ─────────────────────────────────────────────────────────────────
#
# One shared aiohttp session (connection reuse) + one global semaphore that
# caps how many pool-fetch requests can be in flight at the same instant,
# regardless of how many cards are tracked. This is what actually smooths
# out request bursts — see also the per-card stagger in poll_loop().

_http_session: aiohttp.ClientSession | None = None
_pool_semaphore = asyncio.Semaphore(POOL_FETCH_CONCURRENCY)


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(headers=HEADERS)
    return _http_session


async def fetch_pool(card_id: str, retries: int = 2, retry_delay: float = 0.75) -> dict | None:
    """Fetch pool counts for one card. Retries transient network errors and
    429s a couple times before giving up. Logs the real reason on failure
    (except 429s, which are expected under load and not worth logging every
    time) so persistent problems are visible in the console instead of just
    looking "random"."""
    session = await get_http_session()
    url = POOL_URL.format(card_id=card_id)

    for attempt in range(retries + 1):
        try:
            async with _pool_semaphore:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None) if status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            print(f"⚠️  fetch_pool error for card #{card_id} "
                  f"(attempt {attempt + 1}/{retries + 1}): {e!r}")
            if attempt < retries:
                await asyncio.sleep(retry_delay)
            continue

        if status == 200:
            if data.get("ok"):
                return data.get("counts", {})
            # Server responded fine but says this id has no pool data —
            # not a network problem, so retrying won't help.
            reason = data.get("message") or data.get("error") or data
            print(f"⚠️  Pool API returned ok=false for card #{card_id}: {reason}")
            return None

        if status == 429:  # rate limited — expected under load, retry quietly
            if attempt < retries:
                await asyncio.sleep(retry_delay)
            continue

        print(f"⚠️  fetch_pool HTTP {status} for card #{card_id} "
              f"(attempt {attempt + 1}/{retries + 1})")
        if status in (403, 404):
            return None  # won't succeed on retry, don't waste time
        if attempt < retries:
            await asyncio.sleep(retry_delay)

    return None


# ── Card catalog (loaded from lumina_cards.csv, instead of a guessed live
# search API) ───────────────────────────────────────────────────────────────
#
# lumina_cards.csv columns: id, name, series
#   "name" packs the character AND image number together, e.g. "Jessica #3".
# We split that into a separate character / image number here so users can
# search/filter on either piece.

# card_catalog[card_id] = {"id": str, "character": str, "image": int|None, "series": str}
card_catalog: dict[str, dict] = {}

# _character_index[character.lower()] = [card_id, card_id, ...]  (for exact-name lookups)
_character_index: dict[str, list[str]] = {}


def _split_name(name: str) -> tuple[str, int | None]:
    """Split a CSV 'name' like 'Jessica #3' into ('Jessica', 3).
    If there's no '#imageNumber' suffix, returns (name, None)."""
    name = (name or "").strip()
    if "#" in name:
        base, _, num = name.rpartition("#")
        base = base.strip()
        num  = num.strip()
        if base and num.isdigit():
            return base, int(num)
    return name, None


# Recovers an already-resolved card id from one comma-separated segment of
# the `character` field. Matches both a bare id ("29222") and our own
# autocomplete-picked display format ("29222 · Jessica #3 — K-Pop Soloists")
# — see _confirmed_id_from_segment(). Anchored at the start, so it never
# matches an inline image suffix like "Xanxus #1" (that "#1" isn't at
# position 0, so it's left alone for the name-search path).
_ID_PREFIX_RE = re.compile(r"^(\d+)")


def _confirmed_id_from_segment(segment: str) -> str | None:
    """Collapse one already-typed comma segment back down to a bare card id,
    if it's either a plain numeric id or one of our own autocomplete-picked
    '<id> · <label>' entries (see _character_autocomplete_choices). Returns
    None if it's still free-typed name text that hasn't been resolved to a
    specific card yet — callers should keep those segments as-is rather
    than discarding them, since the user may still be typing a name."""
    segment = segment.strip()
    m = _ID_PREFIX_RE.match(segment)
    if m and m.group(1) in card_catalog:
        return m.group(1)
    return None


def load_card_catalog():
    """Load lumina_cards.csv into memory. Called once at startup. Safe to call
    again later (e.g. from an admin /reload command) to pick up CSV edits."""
    global card_catalog, _character_index
    catalog: dict[str, dict] = {}
    index: dict[str, list[str]] = {}
    try:
        with open(CARDS_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = str(row.get("id", "")).strip()
                if not cid:
                    continue
                character, image = _split_name(row.get("name", ""))
                series = (row.get("series") or "").strip()
                rec = {
                    "id": cid, "character": character, "character_lower": character.lower(),
                    "image": image, "series": series,
                }
                catalog[cid] = rec
                index.setdefault(character.lower(), []).append(cid)
        print(f"📚  Loaded {len(catalog)} card(s) from {CARDS_CSV_PATH}")
    except (FileNotFoundError, OSError, csv.Error) as e:
        print(f"⚠️  Couldn't load {CARDS_CSV_PATH}: {e!r} (name search will return no results)")
    card_catalog, _character_index = catalog, index


def lookup_card_by_id(card_id: str) -> dict | None:
    """Look up a card's character/image/series by its exact id, from the CSV."""
    return card_catalog.get(str(card_id))


def search_cards(character: str = "", character_exact: bool = False,
                  image: int | None = None) -> list[dict]:
    """Search the local CSV catalog by character name, optionally narrowed to
    an exact character-name match and/or a specific image number."""
    character = (character or "").strip()
    if not character:
        return []
    needle = character.lower()

    if character_exact:
        results = [card_catalog[cid] for cid in _character_index.get(needle, [])]
    else:
        # character_lower is precomputed once at catalog-load time (see
        # load_card_catalog / sync_new_cards) instead of being recomputed
        # here on every single call — this runs on every autocomplete
        # keystroke, so avoiding ~38k redundant .lower() calls per search
        # matters a lot for latency. Falls back to computing it on the fly
        # only if a record somehow doesn't have it (defensive, shouldn't
        # normally happen).
        results = [
            rec for rec in card_catalog.values()
            if needle in (rec.get("character_lower") or rec["character"].lower())
        ]

    if image is not None:
        results = [rec for rec in results if rec["image"] == image]

    results.sort(key=lambda rec: (rec["character"].lower(), rec["image"] or 0))
    return results


def card_label(card: dict | None, fallback_id: str = "") -> str:
    """Display label, e.g. 'Jessica #3 — K-Pop Soloists'. Used in /find,
    /status, /stop, disambiguation lists, and ping messages."""
    if card is None:
        return f"Card #{fallback_id}"
    name   = card.get("character", "?")
    img    = card.get("image")
    series = card.get("series", "?")
    name_part = f"{name} #{img}" if img is not None else name
    return f"{name_part} — {series}"


# Kept as an alias — earlier revisions used two slightly different-looking
# label helpers, other code below still refers to card_display() by name.
card_display = card_label


def short_card_label(card: dict | None, fallback_id: str = "") -> str:
    """Compact label WITHOUT the series, e.g. 'Kim Dokja #2' (or just
    'Kim Dokja' if there's no image number, or 'Card #123' if we don't have
    catalog data for it at all).

    Discord pastes an autocomplete suggestion's *name* directly into the
    input box the instant a user clicks it — there's no separate "value
    shown in the box" vs "value shown in the dropdown", so whatever we put
    in Choice.name is exactly what ends up in the field. This helper is
    what /monitor's and /stop's autocomplete use to build that name, so the
    series never lingers in the box after a selection is made.
    """
    if card is None:
        return f"Card #{fallback_id}" if fallback_id else "?"
    name = card.get("character", "?")
    img  = card.get("image")
    return f"{name} #{img}" if img is not None else name


# ── Catalog sync — scrape newly added cards into lumina_cards.csv ────────────
#
# NOTE: the exact JSON field names below (id / cardCatalogId, name /
# character+imageNumber, series) are our best guess for this endpoint —
# we don't have a confirmed schema. Use the /synccards admin command once
# to test it: it prints a sample of what it saw, so you can see quickly
# whether cards were actually parsed or whether the field names need
# adjusting in _parse_card_item() below.

def _unwrap_card_list(data) -> list:
    """The endpoint's response envelope isn't confirmed — handle a plain
    list, or a dict with the cards under a few likely keys."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("cards", "data", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _parse_card_item(item: dict) -> dict | None:
    """Normalize one raw API card entry into our {"id","character","image",
    "series"} shape. Tries a "name" field first (e.g. 'Jessica #3', matching
    the CSV directly), then falls back to separate character/imageNumber
    fields if that's what the API actually returns."""
    cid = item.get("cardCatalogId", item.get("id"))
    if cid is None:
        return None
    cid = str(cid).strip()
    if not cid.isdigit():
        return None

    if item.get("name"):
        character, image = _split_name(str(item["name"]))
    else:
        character = str(item.get("character", "")).strip()
        image     = item.get("imageNumber", item.get("image"))
        try:
            image = int(image) if image is not None else None
        except (TypeError, ValueError):
            image = None

    series = str(item.get("series", "")).strip()
    if not character:
        return None
    return {"id": cid, "character": character, "image": image, "series": series}


def fetch_card_list_page(page: int, page_size: int = 100) -> list[dict] | None:
    """Fetch one page of the card catalog. Returns a list of raw (unparsed)
    API items, or None on a network/parse failure."""
    url = f"{CARDS_LIST_URL}?page={page}&pageSize={page_size}"
    try:
        req  = urllib.request.Request(url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return _unwrap_card_list(data)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def _append_cards_to_csv(new_cards: list[dict]):
    """Append newly discovered cards to the CSV file on disk, in the same
    quoted format as the existing file."""
    if not new_cards:
        return
    file_exists = os.path.exists(CARDS_CSV_PATH)

    # If the file already exists but doesn't end in a newline, appending
    # would glue our first new row onto the end of the last existing line.
    if file_exists:
        with open(CARDS_CSV_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                needs_newline = f.read(1) not in (b"\n", b"\r")
            else:
                needs_newline = False
        if needs_newline:
            with open(CARDS_CSV_PATH, "a", encoding="utf-8") as f:
                f.write("\n")

    with open(CARDS_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        if not file_exists:
            writer.writerow(["id", "name", "series"])
        for rec in new_cards:
            name = f"{rec['character']} #{rec['image']}" if rec["image"] is not None else rec["character"]
            writer.writerow([rec["id"], name, rec["series"]])


def sync_new_cards(max_pages: int = 50, page_size: int = 100) -> tuple[int, str]:
    """Scrape from the site's newest cards down to whatever id we already
    have, and append anything new to lumina_cards.csv. Assumes page 1 lists
    the most recently added cards first (newest → oldest) — matching how
    the existing CSV happens to be sorted. Runs synchronously; call it from
    an executor thread, not directly on the event loop.

    Returns (num_added, message).
    """
    local_max_id = max((int(cid) for cid in card_catalog), default=0)

    new_cards: list[dict] = []
    seen_ids: set[str] = set()
    caught_up = False

    for page in range(1, max_pages + 1):
        items = fetch_card_list_page(page, page_size=page_size)
        if items is None:
            return len(new_cards), f"⚠️ Network/parse error fetching page {page} — stopped early."
        if not items:
            break  # ran out of pages

        for raw in items:
            rec = _parse_card_item(raw)
            if rec is None:
                continue
            rec["character_lower"] = rec["character"].lower()
            cid = int(rec["id"])
            if cid <= local_max_id:
                caught_up = True
                continue
            if rec["id"] not in card_catalog and rec["id"] not in seen_ids:
                seen_ids.add(rec["id"])
                new_cards.append(rec)

        if caught_up:
            break

    if new_cards:
        _append_cards_to_csv(new_cards)
        for rec in new_cards:
            card_catalog[rec["id"]] = rec
            _character_index.setdefault(rec["character"].lower(), []).append(rec["id"])

    return len(new_cards), (
        f"✅ Added {len(new_cards)} new card(s)." if new_cards
        else "ℹ️ No new cards found (already up to date)."
    )


async def catalog_sync_loop():
    """Background task: re-scrape the card list roughly once an hour and
    append anything new to lumina_cards.csv. Much less frequent than the
    per-card 3s pool polling — this is just keeping the name/id catalog
    fresh, not watching for pool changes."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            count, msg = await asyncio.get_running_loop().run_in_executor(None, sync_new_cards)
            if count:
                print(f"📚  Catalog sync: {msg}")
        except Exception as e:
            print(f"⚠️  catalog_sync_loop error: {e!r}")


# ── Background poll loop (one per unique card_id) ─────────────────────────────

async def poll_loop(card_id: str, client: discord.Client):
    # Stagger this card's polling so N tracked cards don't all fire their
    # HTTP request in the same instant every INTERVAL seconds. Same total
    # request rate, same freshness per card — just spread evenly across the
    # window instead of arriving as one burst, which is what was tripping
    # the rate limiter. Offset is derived from the card id so it's stable
    # across restarts.
    offset = (int(card_id) % 1000) / 1000 * INTERVAL
    await asyncio.sleep(offset)

    while card_id in monitors:
        await asyncio.sleep(INTERVAL)
        if card_id not in monitors:
            break

        try:
            counts = await fetch_pool(card_id)
            if counts is None:
                continue

            mon      = monitors[card_id]
            prev     = mon.get("prev") or {}
            prev_rar = prev.get("byRarity", {})
            by_rar   = counts.get("byRarity", {})

            # Check which rarities increased
            increased = {}
            for key, (label, emoji) in RARITIES.items():
                old = prev_rar.get(key, 0)
                new = by_rar.get(key, 0)
                if new > old:
                    increased[key] = (old, new, label, emoji)

            if increased:
                card_disp = mon.get("label") or f"Card #{card_id}"
                for user_id, prefs in list(mon["users"].items()):
                    alert_map = {
                        "3": prefs.get("alert_r",   False),
                        "4": prefs.get("alert_sr",  False),
                        "5": prefs.get("alert_ssr", False),
                        "7": prefs.get("alert_ur",  False),
                        "6": prefs.get("alert_lr",  False),
                    }

                    pings = []
                    for key, (old, new, label, emoji) in increased.items():
                        if alert_map.get(key):
                            pings.append(
                                f"⭐ **{card_disp}** - {emoji} **{label}** (`{old}` → `{new}`)"
                            )

                    if pings:
                        try:
                            note       = prefs.get("note", "")
                            # A Discord mention is just "<@id>" — Discord's client
                            # resolves the display name itself. fetch_user() would
                            # make a full API round-trip for no reason (we never
                            # use anything but .mention from it), which only adds
                            # latency to the most time-sensitive path in the bot.
                            mention    = f"<@{user_id}>"
                            channel_id = prefs.get("channel_id")
                            channel    = client.get_channel(channel_id)
                            if channel is None:
                                channel = await client.fetch_channel(channel_id)
                            # Note shown in header so user knows which character this is
                            header  = f"**[{note}]** " if note else ""
                            await channel.send(
                                f"{mention} {header} " + " ".join(pings)
                            )
                        except Exception:
                            pass

            monitors[card_id]["prev"] = counts
            if increased:
                mark_monitors_dirty()  # coalesced into one write ~every 2s by monitors_autosave_loop()

        except Exception as e:
            # Never let an unexpected error kill this card's polling task —
            # log it and just try again on the next loop iteration.
            print(f"⚠️  poll_loop error for card #{card_id}: {e!r}")
            continue


# ── Discord bot ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # required to read message text for AUTO_RESPONSES below
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


# ── Auto-responses ───────────────────────────────────────────────────────────
#
# Simple exact-text trigger -> reply mapping. Edit this dict freely: add,
# remove, or change entries as you like.
#
#   - Matching is on the trimmed message content, case-insensitive.
#   - Keys are the exact text that must be said (not "contains" — the whole
#     message content has to match after trimming whitespace).
#   - Values are the text the bot replies with.
AUTO_RESPONSES: dict[str, str] = {
    "<@&1524388897973473450>": "https://tenor.com/l1hv4rqfPJP.gif",
    # "your trigger text here": "your reply text here",
}


@client.event
async def on_ready():
    load_config()
    load_card_catalog()
    await get_http_session()
    await restore_monitors()
    await tree.sync()
    client.loop.create_task(catalog_sync_loop())
    client.loop.create_task(monitors_autosave_loop())
    client.loop.create_task(git_sync_loop())
    print(f"✅  Bot ready: {client.user}")
    print(f"⏱️  Polling every {INTERVAL}s (staggered), max {POOL_FETCH_CONCURRENCY} concurrent pool fetches, "
          f"syncing card catalog every {SYNC_INTERVAL}s")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return  # ignore other bots (and ourselves) to avoid reply loops

    text = message.content.strip().lower()
    reply = AUTO_RESPONSES.get(text)
    if reply:
        await message.channel.send(reply)
        return  # don't also let the AI chime in on an auto-response hit

    # No is_allowed_channel here on purpose: /setchannel locks slash
    # commands like /monitor to one channel, but the AI chat should work
    # everywhere regardless of that lock.
    await handle_ai_message(message, client)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)

    # "Unknown interaction" / expired interaction — usually caused by either
    # a duplicate bot process responding to the same interaction first, or
    # the 3-second ack window being missed. Nothing we can do for this one
    # specific interaction, so just log it quietly instead of a traceback.
    if isinstance(original, discord.NotFound) and getattr(original, "code", None) == 10062:
        print(f"⚠️  Interaction expired before we could respond (command: {interaction.command and interaction.command.name}). "
              f"If this keeps happening, make sure only ONE instance of the bot is running.")
        return

    print(f"⚠️  Unhandled app command error in '{interaction.command and interaction.command.name}': {original!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ Something went wrong running that command.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Something went wrong running that command.", ephemeral=True)
    except discord.HTTPException:
        pass


# ── Channel-lock check ────────────────────────────────────────────────────────

def is_allowed_channel(interaction: discord.Interaction) -> bool:
    """Return True if this interaction is happening in the channel the bot
    is locked to for this guild (or if no lock is set for this guild)."""
    if interaction.guild is None:
        return True  # DMs: no guild lock applies

    guild_id = str(interaction.guild_id)
    locked_channel_id = guild_channel_locks.get(guild_id)
    if locked_channel_id is None:
        return True
    return interaction.channel_id == locked_channel_id


def is_allowed_channel_for_message(message: discord.Message) -> bool:
    """Same check as is_allowed_channel(), but for a plain Message instead
    of a slash-command Interaction — used by the AI chat feature, since
    on_message doesn't get an Interaction object."""
    if message.guild is None:
        return True  # DMs: no guild lock applies

    guild_id = str(message.guild.id)
    locked_channel_id = guild_channel_locks.get(guild_id)
    if locked_channel_id is None:
        return True
    return message.channel.id == locked_channel_id


async def enforce_channel_lock(interaction: discord.Interaction) -> bool:
    """Call at the top of every public command. Returns True if the command
    should proceed, False if it was blocked (and a message was already sent)."""
    if is_allowed_channel(interaction):
        return True

    guild_id = str(interaction.guild_id)
    locked_channel_id = guild_channel_locks.get(guild_id)
    await interaction.response.send_message(
        f"🚫 This bot can only be used in <#{locked_channel_id}>.",
        ephemeral=True
    )
    return False


def is_admin(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


# Cached after the first lookup — the bot's owner doesn't change at
# runtime, so there's no need to hit Discord's API on every /upload call.
_cached_owner_ids: set[int] | None = None


async def is_bot_owner(user: discord.abc.User) -> bool:
    """discord.Client (unlike commands.Bot) has no built-in is_owner() —
    this fetches the application's owner directly. Handles both a
    single-owner app and a team-owned app (any team member counts)."""
    global _cached_owner_ids
    if _cached_owner_ids is None:
        try:
            info = await client.application_info()
        except Exception as e:
            print(f"⚠️  Couldn't fetch application info for owner check: {e!r}")
            return False
        if info.team is not None:
            _cached_owner_ids = {member.id for member in info.team.members}
        else:
            _cached_owner_ids = {info.owner.id}
    return user.id in _cached_owner_ids


# ── /join, /leave (registered from voice_feature.py) ────────────────────────────
# Kept in its own file so voice behavior can be edited without touching this
# one. is_admin is still passed through for signature compatibility, but
# /join and /leave no longer check it — they're open to everyone.
setup_voice_commands(tree, is_admin)

# ── /say (registered from say.py) ────────────────────────────────────────────
# Same pattern as voice_feature.py above — kept in its own file, wired in with
# the shared tree + is_admin() check.
setup_say_commands(tree, is_admin)



# ── /setchannel (admin) ───────────────────────────────────────────────────────

@tree.command(name="setchannel", description="(Admin) Lock the bot to this channel only")
async def cmd_setchannel(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    if not is_admin(interaction):
        await interaction.response.send_message(
            "🚫 Only server administrators can use this command.", ephemeral=True
        )
        return

    guild_channel_locks[str(interaction.guild_id)] = interaction.channel_id
    await save_config()

    await interaction.response.send_message(
        f"🔒 Bot commands are now restricted to {interaction.channel.mention} in this server.",
        ephemeral=True
    )


# ── /unlock (admin) ───────────────────────────────────────────────────────────

@tree.command(name="unlock", description="(Admin) Remove the channel restriction for this server")
async def cmd_unlock(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    if not is_admin(interaction):
        await interaction.response.send_message(
            "🚫 Only server administrators can use this command.", ephemeral=True
        )
        return

    guild_id = str(interaction.guild_id)
    if guild_id in guild_channel_locks:
        del guild_channel_locks[guild_id]
        await save_config()
        await interaction.response.send_message(
            "🔓 Channel restriction removed. The bot can now be used in any channel.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "ℹ️ This server doesn't have a channel restriction set.", ephemeral=True
        )


# ── /upload (bot owner only) ─────────────────────────────────────────────────
#
# Deliberately gated on the BOT's owner (the Discord application's owner),
# not is_admin() — is_admin() only checks "administrator in this particular
# server," but this pushes local data files to a shared GitHub repo that's
# not scoped per-server. Only the person actually running the bot should
# trigger a push remotely.
#
# Unlike /shutdown (which would also stop the bot), this just forces an
# immediate save + git push and keeps running — useful right before you're
# about to close VS Code or hand off to your friend, without needing
# terminal access or waiting for the periodic GIT_PUSH_INTERVAL push.

@tree.command(name="upload", description="(Bot owner only) Save and push monitors.json / lumina_cards.csv to git now")
async def cmd_upload(interaction: discord.Interaction):
    if not await is_bot_owner(interaction.user):
        await interaction.response.send_message(
            "🚫 Only the bot's owner can use this command.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    # Flush any pending monitor changes to disk right now, instead of
    # waiting on monitors_autosave_loop()'s ~2s debounce window.
    await save_monitors()

    # Capture whether the push actually did anything, so the reply is
    # honest about "pushed" vs "nothing had changed."
    ok, out = await asyncio.get_running_loop().run_in_executor(
        None, _run_git, "status", "--porcelain", *GIT_SYNC_PATHS
    )
    something_changed = ok and out.strip() != ""

    await git_push_monitors("remote /upload command")
    print(f"⬆️   /upload invoked by {interaction.user} ({interaction.user.id}).")

    if something_changed:
        await interaction.followup.send(
            "✅ Pushed the latest monitors.json / lumina_cards.csv to GitHub.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "ℹ️ Nothing had changed — GitHub already has the latest version.", ephemeral=True
        )


# ── /synccards (admin) ────────────────────────────────────────────────────────

@tree.command(name="synccards", description="Re-scrape luminabot.net for newly added cards right now")
async def cmd_synccards(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    before = len(card_catalog)
    count, msg = await asyncio.get_running_loop().run_in_executor(None, sync_new_cards)
    await interaction.followup.send(
        f"{msg}\n📚 Catalog now has **{len(card_catalog)}** card(s) (was {before}).\n"
        + ("If this keeps returning 0 even when you know new cards exist, the API's response "
           "shape may not match what `_parse_card_item()` expects — check the bot's console log."
           if count == 0 else ""),
        ephemeral=False
    )


# ── shared helpers used by /monitor and /find ─────────────────────────────────

async def _character_autocomplete_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice]:
    """Search the local CSV catalog as the user types a character name,
    respecting whatever image/character_exact they've already filled in on
    the same command. The CSV lookup is in-memory, so this runs synchronously
    — no network round-trip needed.

    Option B (mixed typing + autocomplete): only the segment after the last
    comma is the "live" token being searched. Every earlier segment is
    normalized back down to a bare id via _confirmed_id_from_segment() —
    this matters because Discord replaces the box's visible text with the
    *label* of whatever you clicked, not the id, so without this the id
    from an earlier pick would be lost the moment you kept typing. Segments
    that are still free-typed, unresolved names pass through unchanged.

    Suggestions use short_card_label() (no series) — this is what actually
    lands in the input box the instant a user clicks, so the box never ends
    up showing series text, even briefly.

    Two Discord platform limits this can't work around:
      - We only ever see the field's current text, never cursor position,
        so "the token being edited" is always assumed to be the last one.
      - Each suggestion is capped at 100 characters by Discord, so long
        chains eventually can't fit — see the length guard below.
    """
    parts = current.split(",")
    tail  = parts[-1].strip()
    if len(tail) < 2:
        return []

    prefix_segments = [
        (_confirmed_id_from_segment(p) or p.strip())
        for p in parts[:-1]
        if p.strip()
    ]
    prefix_str = (", ".join(prefix_segments) + ", ") if prefix_segments else ""

    # Too close to Discord's 100-char autocomplete limit to safely append
    # another id without risking truncating one already in the prefix —
    # bail out rather than emit a corrupted value. The user can still keep
    # typing manually past this point (no length limit on raw text).
    if len(prefix_str) > 80:
        return []

    prefix_ids = {s for s in prefix_segments if s.isdigit()}

    ns = interaction.namespace  # lets us read the other options already typed
    results = search_cards(
        character=tail,
        character_exact=bool(getattr(ns, "character_exact", False)),
        image=getattr(ns, "image", None),
    )

    choices = []
    for c in results:
        if c["id"] in prefix_ids:  # already picked earlier in this field — don't offer it twice
            continue
        # short_card_label (no series) — this is what actually lands in the
        # input box the moment the user clicks, so it stays series-free.
        display = f"{c['id']} · {short_card_label(c)}"
        choices.append(app_commands.Choice(
            name=(prefix_str + display)[:100],
            value=(prefix_str + c["id"])[:100],
        ))
        if len(choices) >= 25:  # Discord's max choices per autocomplete response
            break
    return choices


def _register_monitor(card_id: str, card: dict | None, user_id: str, note: str,
                       alert_r: bool, alert_sr: bool, alert_ssr: bool,
                       alert_ur: bool, alert_lr: bool, channel_id: int,
                       counts: dict) -> str:
    """Create or join a monitor for one card_id. Returns its display label."""
    label     = card_display(card, fallback_id=card_id)
    character = card.get("character") if card else None
    series    = card.get("series") if card else None

    if card_id not in monitors:
        monitors[card_id] = {
            "prev": counts, "users": {},
            "label": label, "character": character, "series": series,
        }
        task = client.loop.create_task(poll_loop(card_id, client))
        monitors[card_id]["task"] = task
    else:
        # keep label/character/series fresh in case the CSV changed
        monitors[card_id]["label"]     = label
        monitors[card_id]["character"] = character
        monitors[card_id]["series"]    = series

    monitors[card_id]["users"][user_id] = {
        "note":       note,
        "alert_r":    alert_r,
        "alert_sr":   alert_sr,
        "alert_ssr":  alert_ssr,
        "alert_ur":   alert_ur,
        "alert_lr":   alert_lr,
        "channel_id": channel_id,
    }
    return label


def _resolve_character_names(
    character: str, character_exact: bool, image: int | None
) -> tuple[list[tuple[str, dict]], list[str]]:
    """Resolve a (possibly comma-separated) `character` field into concrete
    cards, using the local CSV catalog. Each comma-separated entry is either
    a numeric card id (as handed back by autocomplete) or a name to search.

    Returns (targets, issue_lines):
      - targets: list of (card_id, card_dict) — one entry per name that
        matched exactly one card
      - issue_lines: human-readable strings describing any name that matched
        zero cards (not found) or more than one (ambiguous, needs narrowing)
    """
    names   = [x.strip() for x in character.split(",") if x.strip()]
    targets: list[tuple[str, dict]] = []
    issues:  list[str] = []
    seen_ids: set[str] = set()

    for raw_name in names[:25]:
        confirmed_id = _confirmed_id_from_segment(raw_name)
        if confirmed_id:
            if confirmed_id not in seen_ids:  # skip a card picked twice in one field
                seen_ids.add(confirmed_id)
                targets.append((confirmed_id, lookup_card_by_id(confirmed_id)))
            continue

        # Allow an inline "#N" per name (matching the CSV's own display
        # format, e.g. "Xanxus #1") to override the command-level `image`.
        name, inline_image = _split_name(raw_name)
        this_image = inline_image if inline_image is not None else image

        results = search_cards(character=name, character_exact=character_exact, image=this_image)

        if not results:
            issues.append(f"❌ No cards found matching **{raw_name}**.")
        elif len(results) > 1:
            preview = "\n".join(f"  • {card_label(c)}  `#{c['id']}`" for c in results[:5])
            more = f"\n  …and {len(results) - 5} more." if len(results) > 5 else ""
            issues.append(
                f"🔎 **{name}** matched {len(results)} cards — narrow it down "
                f"(add `image`/`character_exact`), or pick one from autocomplete:\n{preview}{more}"
            )
        elif results[0]["id"] not in seen_ids:  # skip a card picked twice in one field
            seen_ids.add(results[0]["id"])
            targets.append((results[0]["id"], results[0]))

    return targets, issues


# ── /monitor ──────────────────────────────────────────────────────────────────

@tree.command(name="monitor", description="Track one or more Lumina card pools and get pinged on changes")
@app_commands.describe(
    card_ids        = "One or more exact card catalog IDs, comma-separated — skips the name search below and lets you track many at once",
    character       = "Character name(s) — comma-separated for multiple, or pick suggestions as you type (ignored if card_ids is set)",
    image           = "Specific image/artwork number (optional, narrows the search)",
    character_exact = "Match the character name exactly, not partially (optional)",
    note            = "Label to identify these cards in pings (e.g. 'Kim Dokja SSR')",
    alert_r         = "Ping when R increases",
    alert_sr        = "Ping when SR increases",
    alert_ssr       = "Ping when SSR increases",
    alert_ur        = "Ping when UR increases",
    alert_lr        = "Ping when LR increases",
)
async def cmd_monitor(
    interaction:     discord.Interaction,
    card_ids:        str  = "",
    character:       str  = "",
    image:           int | None = None,
    character_exact: bool = False,
    note:            str  = "",
    alert_r:         bool = False,
    alert_sr:        bool = False,
    alert_ssr:       bool = False,
    alert_ur:        bool = False,
    alert_lr:        bool = False,
):
    if not await enforce_channel_lock(interaction):
        return

    if not any([alert_r, alert_sr, alert_ssr, alert_ur, alert_lr]):
        await interaction.response.send_message(
            "⚠️ Please enable at least one alert: R, SR, SSR, UR, or LR.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    # pairs of (card_id, card_dict_or_None) to register — resolved either
    # from raw IDs (card_ids) or from a name search (character) against the
    # local lumina_cards.csv catalog
    targets: list[tuple[str, dict | None]] = []
    issues:  list[str] = []  # non-fatal problems (name not found / ambiguous) to surface at the end

    if card_ids.strip():
        seen_ids: set[str] = set()
        ids = []
        for x in card_ids.split(","):
            x = x.strip()
            if x and x not in seen_ids:  # skip a card id repeated in the list
                seen_ids.add(x)
                ids.append(x)
        bad = [x for x in ids if not x.isdigit()]
        if bad:
            await interaction.followup.send(
                f"⚠️ `card_ids` must be numbers separated by commas. Not valid: {', '.join(bad)}",
                ephemeral=True
            )
            return
        for cid in ids[:25]:
            card = lookup_card_by_id(cid)
            targets.append((cid, card))

    elif character.strip():
        targets, issues = _resolve_character_names(character, character_exact, image)
        if not targets:
            # Nothing usable at all — just report the issues and stop.
            await interaction.followup.send(
                "\n\n".join(issues) if issues else f"❌ No cards found matching **{character}**.",
                ephemeral=True
            )
            return
        # If some names resolved and some didn't, we still proceed with the
        # ones that worked — `issues` gets folded into the final summary below.
    else:
        await interaction.followup.send(
            "⚠️ Provide either `card_ids` (one or more numeric IDs, comma-separated) "
            "or a `character` name to search for.",
            ephemeral=True
        )
        return

    on_labels = []
    if alert_r:   on_labels.append("🟦 R")
    if alert_sr:  on_labels.append("🟪 SR")
    if alert_ssr: on_labels.append("🟨 SSR")
    if alert_ur:  on_labels.append("🟥 UR")
    if alert_lr:  on_labels.append("🌈 LR")

    user_id   = str(interaction.user.id)
    note_line = f"📝 Note: **{note}**\n" if note else ""
    result_lines = list(issues)  # surface any "not found"/"ambiguous" names first
    any_changed  = False

    for card_id, card in targets:
        counts = await fetch_pool(card_id)
        if counts is None:
            result_lines.append(
                f"❌ `#{card_id}` — couldn't fetch pool data "
                f"(<https://luminabot.net/api/cards/pool?cardCatalogId={card_id}>)"
            )
            continue

        label = _register_monitor(
            card_id, card, user_id, note,
            alert_r, alert_sr, alert_ssr, alert_ur, alert_lr,
            interaction.channel_id, counts,
        )
        any_changed = True
        by_rar = counts.get("byRarity", {})
        result_lines.append(
            f"✅ **{label}** (`#{card_id}`)\n"
            f"> 📦 Total `{counts.get('total', '?')}`  {fmt_pool(by_rar)}"
        )

    if any_changed:
        await save_monitors()

    header = (
        f"{note_line}"
        f"🔔 Alerts: {' · '.join(on_labels)}\n"
        f"⏱️ Checking every **{INTERVAL}s**\n\n"
    )
    await interaction.followup.send(header + "\n".join(result_lines), ephemeral=True)


@cmd_monitor.autocomplete("character")
async def cmd_monitor_autocomplete(interaction: discord.Interaction, current: str):
    return await _character_autocomplete_choices(interaction, current)


# ── /status ─────────────────────────────────────────────────────────────────

@tree.command(name="status", description="See all cards YOU are currently tracking")
async def cmd_status(interaction: discord.Interaction):
    if not await enforce_channel_lock(interaction):
        return

    user_id  = str(interaction.user.id)
    my_cards = [(cid, mon) for cid, mon in monitors.items() if user_id in mon["users"]]

    if not my_cards:
        await interaction.response.send_message(
            "You're not tracking any cards yet. Use `/monitor` to start!",
            ephemeral=True
        )
        return

    lines = []
    for card_id, mon in my_cards:
        prefs  = mon["users"][user_id]
        counts = mon.get("prev") or {}
        by_rar = counts.get("byRarity", {})
        note   = prefs.get("note", "")

        on_labels = []
        if prefs.get("alert_r"):   on_labels.append("🟦 R")
        if prefs.get("alert_sr"):  on_labels.append("🟪 SR")
        if prefs.get("alert_ssr"): on_labels.append("🟨 SSR")
        if prefs.get("alert_ur"):  on_labels.append("🟥 UR")
        if prefs.get("alert_lr"):  on_labels.append("🌈 LR")

        note_line   = f" — 📝 *{note}*" if note else ""
        card_label_ = mon.get("label") or f"Card #{card_id}"
        lines.append(
            f"**{card_label_}** (`#{card_id}`){note_line}\n"
            f"> Alerts: {' · '.join(on_labels)}\n"
            f"> 📦 Total `{counts.get('total','?')}`  {fmt_pool(by_rar)}"
        )

    # Discord caps a single message at 2000 characters — group cards into
    # chunks so we never try to send a message over that limit.
    DISCORD_MSG_LIMIT = 2000
    header = f"📋 **Your tracked cards ({len(my_cards)}):**\n\n"

    chunks: list[str] = []
    current = header
    for line in lines:
        candidate = current + ("" if current == header else "\n\n") + line
        if len(candidate) > DISCORD_MSG_LIMIT - 50:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)

    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


# ── /stop ─────────────────────────────────────────────────────────────────────

@tree.command(name="stop", description="Stop tracking one or more cards")
@app_commands.describe(card_id="Card ID(s) to stop tracking, comma-separated — pick suggestions as you type")
async def cmd_stop(interaction: discord.Interaction, card_id: str):
    if not await enforce_channel_lock(interaction):
        return

    user_id = str(interaction.user.id)

    # Comma-separated multi-stop, same Option B chaining as /monitor's
    # character field: each segment is either a bare id you typed, or one
    # of our own "<id> · <label>" autocomplete-picked entries.
    seen_ids: set[str] = set()
    ids: list[str] = []
    for raw in card_id.split(","):
        cid = _confirmed_id_from_segment(raw) or raw.strip()
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            ids.append(cid)

    if not ids:
        await interaction.response.send_message("⚠️ Provide at least one card id.", ephemeral=True)
        return

    lines: list[str] = []
    any_changed = False

    for cid in ids[:25]:
        if cid not in monitors or user_id not in monitors[cid]["users"]:
            lines.append(f"❌ You're not tracking `#{cid}`.")
            continue

        note  = monitors[cid]["users"][user_id].get("note", "")
        label = monitors[cid].get("label") or f"Card #{cid}"
        del monitors[cid]["users"][user_id]
        any_changed = True

        if not monitors[cid]["users"]:
            monitors[cid]["task"].cancel()
            del monitors[cid]
            lines.append(f"🛑 Stopped tracking **{label}**" + (f" (*{note}*)" if note else "") + ".")
        else:
            remaining = len(monitors[cid]["users"])
            lines.append(
                f"🔕 Unsubscribed from **{label}**" + (f" (*{note}*)" if note else "")
                + f". ({remaining} other user(s) still watching)"
            )

    if any_changed:
        await save_monitors()

    # Discord caps a single message at 2000 characters — chunk just in case.
    DISCORD_MSG_LIMIT = 2000
    chunks: list[str] = []
    current_chunk = ""
    for line in lines:
        candidate = current_chunk + ("\n" if current_chunk else "") + line
        if len(candidate) > DISCORD_MSG_LIMIT - 20:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = candidate
    chunks.append(current_chunk)

    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@cmd_stop.autocomplete("card_id")
async def stop_autocomplete(interaction: discord.Interaction, current: str):
    """Show only the cards this user is currently tracking. Supports the
    same Option B multi-select chaining as /monitor's character field:
    only the segment after the last comma is searched, and earlier
    segments are normalized back to bare ids (via _confirmed_id_from_segment)
    so a chain of picks stays parseable.

    Suggestions use short_card_label() (no series), same as /monitor, so
    the box never ends up showing series text, even briefly."""
    user_id = str(interaction.user.id)

    parts = current.split(",")
    tail  = parts[-1].strip().lower()

    prefix_segments = [
        (_confirmed_id_from_segment(p) or p.strip())
        for p in parts[:-1]
        if p.strip()
    ]
    prefix_str = (", ".join(prefix_segments) + ", ") if prefix_segments else ""
    if len(prefix_str) > 80:  # same 100-char Discord ceiling guard as /monitor
        return []
    prefix_ids = {s for s in prefix_segments if s.isdigit()}

    choices = []
    for cid, mon in monitors.items():
        if user_id not in mon["users"] or cid in prefix_ids:
            continue
        # Prefer a fresh catalog lookup (has the image number) over the
        # stored "label", which was built with card_label() and therefore
        # includes the series — see short_card_label(). Falls back to the
        # bare character name saved on the monitor if the id has since
        # disappeared from the catalog.
        card = lookup_card_by_id(cid)
        base = short_card_label(card, fallback_id=cid) if card else (mon.get("character") or f"Card #{cid}")
        note = mon["users"][user_id].get("note", "")
        # Strip commas — a note is free text you typed when running
        # /monitor and could contain one (e.g. "SSR, batch 2"), which would
        # otherwise corrupt the comma-based multi-select parsing exactly
        # like series names can for /monitor's character field.
        display_label = (base + (f" — {note}" if note else "")).replace(",", "")
        if tail and tail not in display_label.lower() and tail not in cid:
            continue
        display = f"{cid} · {display_label}"
        choices.append(app_commands.Choice(
            name=(prefix_str + display)[:100],
            value=(prefix_str + cid)[:100],
        ))
        if len(choices) >= 25:
            break
    return choices


# ── /stopall ──────────────────────────────────────────────────────────────────

@tree.command(name="stopall", description="Stop tracking ALL your cards at once")
async def cmd_stopall(interaction: discord.Interaction):
    if not await enforce_channel_lock(interaction):
        return

    user_id  = str(interaction.user.id)
    my_cards = [cid for cid, mon in monitors.items() if user_id in mon["users"]]

    if not my_cards:
        await interaction.response.send_message(
            "You're not tracking any cards.", ephemeral=True
        )
        return

    for card_id in my_cards:
        del monitors[card_id]["users"][user_id]
        if not monitors[card_id]["users"]:
            monitors[card_id]["task"].cancel()
            del monitors[card_id]

    await save_monitors()
    await interaction.response.send_message(
        f"🛑 Stopped tracking all **{len(my_cards)}** card(s).", ephemeral=True
    )


# ── /purgeuser (admin) ────────────────────────────────────────────────────────
#
# Deletes every message from a given Discord user ID across all text channels
# (and, optionally, threads) in this server. This works fine even if the user
# has already left — a Discord message carries its author's ID forever, and
# channel.purge()'s check= callback filters on that ID directly, no live
# Member object required.
#
# Caveats worth knowing:
#   • Requires the bot to have "Manage Messages" (for bulk delete) and
#     "Read Message History" in a channel — channels without both are
#     skipped and listed in the summary instead of silently failing.
#   • Discord's bulk-delete endpoint only works on messages < 14 days old.
#     discord.py's purge() automatically falls back to one-at-a-time deletes
#     for older messages, which is much slower and more rate-limit-bound —
#     a purge that has to walk back through months of history can take a
#     while to finish.
#   • This is irreversible, so it's gated to server administrators only,
#     same bar as /setchannel.

@tree.command(name="purgeuser", description="(Admin) Delete ALL messages from a specific user ID in this server")
@app_commands.describe(
    user_id="The user's numeric Discord ID (right-click their name → Copy User ID). Works even if they left the server.",
    include_threads="Also search inside threads, not just top-level channels (default: True)",
)
async def cmd_purgeuser(interaction: discord.Interaction, user_id: str, include_threads: bool = True):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    if not is_admin(interaction):
        await interaction.response.send_message(
            "🚫 Only server administrators can use this command.", ephemeral=True
        )
        return

    if not user_id.isdigit():
        await interaction.response.send_message(
            "⚠️ `user_id` must be a numeric Discord user ID "
            "(enable Developer Mode, then right-click the user → Copy User ID).",
            ephemeral=True,
        )
        return

    target_id = int(user_id)
    await interaction.response.defer(ephemeral=True, thinking=True)

    def is_target(m: discord.Message) -> bool:
        return m.author.id == target_id

    # Gather every place messages could live: all text channels, plus their
    # active threads, plus their archived threads (archived ones aren't in
    # the channel's cached .threads list and have to be fetched explicitly).
    channels: list = list(interaction.guild.text_channels)
    if include_threads:
        for ch in interaction.guild.text_channels:
            channels.extend(ch.threads)
            try:
                async for th in ch.archived_threads(limit=None):
                    channels.append(th)
            except discord.Forbidden:
                pass  # no perms to list archived threads here — purge loop below will just skip it

    total_deleted = 0
    skipped: list[str] = []
    errored: list[str] = []

    for ch in channels:
        perms = ch.permissions_for(interaction.guild.me)
        if not (perms.manage_messages and perms.read_message_history):
            skipped.append(getattr(ch, "name", str(ch.id)))
            continue
        try:
            deleted = await ch.purge(limit=None, check=is_target, bulk=True)
            total_deleted += len(deleted)
        except discord.Forbidden:
            skipped.append(getattr(ch, "name", str(ch.id)))
        except Exception as e:
            errored.append(f"{getattr(ch, 'name', ch.id)} ({e})")

    lines = [
        f"🧹 Deleted **{total_deleted}** message(s) from user `{target_id}` "
        f"across {len(channels)} channel(s)/thread(s)."
    ]
    if skipped:
        shown = ", ".join(skipped[:15]) + (" …" if len(skipped) > 15 else "")
        lines.append(f"⏭️ Skipped (missing permissions): {shown}")
    if errored:
        shown = ", ".join(errored[:10]) + (" …" if len(errored) > 10 else "")
        lines.append(f"⚠️ Errors: {shown}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ── /find ─────────────────────────────────────────────────────────────────────

@tree.command(name="find", description="Look up Lumina cards (search only — doesn't start tracking)")
@app_commands.describe(
    card_id         = "One or more exact card catalog IDs, comma-separated — skips the name search below",
    character       = "Character name(s) — comma-separated for multiple, or pick suggestions as you type (ignored if card_id is set)",
    image           = "Specific image/artwork number (optional, narrows the search)",
    character_exact = "Match the character name exactly, not partially (optional)",
)
async def cmd_find(
    interaction:     discord.Interaction,
    card_id:         str  = "",
    character:       str  = "",
    image:           int | None = None,
    character_exact: bool = False,
):
    if not await enforce_channel_lock(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    lines = []

    if card_id.strip():
        ids = [x.strip() for x in card_id.split(",") if x.strip()]
        bad = [x for x in ids if not x.isdigit()]
        if bad:
            await interaction.followup.send(
                f"⚠️ `card_id` must be numbers separated by commas. Not valid: {', '.join(bad)}",
                ephemeral=True
            )
            return

        for cid in ids[:25]:
            card   = lookup_card_by_id(cid)
            counts = await fetch_pool(cid)
            if card is None and counts is None:
                lines.append(f"❌ `#{cid}` — not found.")
                continue
            name   = card_label(card, fallback_id=cid)
            total  = counts.get("total", "?") if counts else "?"
            by_rar = counts.get("byRarity", {}) if counts else {}
            lines.append(f"**{name}**  `#{cid}`\n> 📦 Total `{total}`  {fmt_pool(by_rar)}")

    elif character.strip():
        names     = [x.strip() for x in character.split(",") if x.strip()]
        results   = []
        seen_ids: set[str] = set()
        not_found = []
        for raw_name in names[:25]:
            confirmed_id = _confirmed_id_from_segment(raw_name)
            if confirmed_id:
                if confirmed_id not in seen_ids:
                    seen_ids.add(confirmed_id)
                    results.append(card_catalog[confirmed_id])
                continue
            # Allow an inline "#N" per name (e.g. "Xanxus #1") to override
            # the command-level `image`, matching the CSV's own display format.
            name, inline_image = _split_name(raw_name)
            this_image = inline_image if inline_image is not None else image
            matches = search_cards(character=name, character_exact=character_exact, image=this_image)
            if not matches:
                not_found.append(raw_name)
            else:
                for m in matches:
                    if m["id"] not in seen_ids:
                        seen_ids.add(m["id"])
                        results.append(m)

        if not results:
            await interaction.followup.send(f"❌ No cards found matching **{character}**.", ephemeral=True)
            return
        if not_found:
            lines.append("❌ No cards found matching: " + ", ".join(f"**{n}**" for n in not_found))

        for c in results[:25]:
            cid    = c["id"]
            counts = await fetch_pool(cid)
            total  = counts.get("total", "?") if counts else "?"
            by_rar = counts.get("byRarity", {}) if counts else {}
            lines.append(f"**{card_label(c)}**  `#{cid}`\n> 📦 Total `{total}`  {fmt_pool(by_rar)}")

    else:
        await interaction.followup.send(
            "⚠️ Provide either `card_id` (one or more numeric IDs, comma-separated) "
            "or a `character` name to search for.",
            ephemeral=True
        )
        return

    # Discord caps a single message at 2000 characters — chunk like /status does.
    DISCORD_MSG_LIMIT = 2000
    header = f"🔎 **Found {len(lines)} card(s):**\n\n"
    chunks: list[str] = []
    current_chunk = header
    for line in lines:
        candidate = current_chunk + ("" if current_chunk == header else "\n\n") + line
        if len(candidate) > DISCORD_MSG_LIMIT - 50:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = candidate
    chunks.append(current_chunk)

    await interaction.followup.send(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@cmd_find.autocomplete("character")
async def cmd_find_autocomplete(interaction: discord.Interaction, current: str):
    return await _character_autocomplete_choices(interaction, current)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ DISCORD_BOT_TOKEN environment variable is not set.\n"
            "Set it before running, e.g.:\n"
            "  export DISCORD_BOT_TOKEN=\"your-token-here\"   (Linux/macOS)\n"
            "  set DISCORD_BOT_TOKEN=your-token-here          (Windows cmd)"
        )

    # Get the latest monitors.json from GitHub before we load anything —
    # this is what makes "whoever starts the bot has the latest data" work.
    git_pull_monitors_sync()

    try:
        client.run(BOT_TOKEN)
    finally:
        # Runs on Ctrl+C, /stop-the-process, or a clean discord.py shutdown —
        # NOT on kill -9 or a hard crash, which is what git_sync_loop's
        # periodic push is for. This is the "hand the baton back" push.
        print("💾  Pushing final monitors.json state to git before exit...")
        _git_push_monitors_sync("shutdown")