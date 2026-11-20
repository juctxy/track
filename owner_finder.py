"""
owner_finder.py — find which player just pulled a tracked card
==============================================================
Used by track4.py. Given a card_catalog_id + rarity that just went up in the
pool, searches players' Luminabot collections for the freshly pulled copy.

How it stays fast (see the notes in the chat for the reasoning):
  1. user_id.csv is loaded ONCE into memory (reloaded only if the file's mtime changes).
  2. A player's collection is sorted newest-first, so a brand-new pull is on
     page 1. We only fetch more pages while the *entire* page is still fresh
     (e.g. a 10-pull), never blindly.
  3. Every page we fetch is indexed. If a player pulled 10 cards at once,
     finding the first one lets the other 9 be resolved with ZERO requests.
  4. Players seen with recent activity (and recent matches) are searched FIRST.
  5. Bounded concurrency + a global requests/sec gate that backs off on 429
     and slowly recovers. Never 15,039 requests at once.
  6. Each copy (userCardId) is attributed at most once -> no duplicate or
     mis-attributed announcements.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import tempfile
import time
from typing import Awaitable, Callable
from urllib.parse import urlencode

import aiohttp

# ── Settings ──────────────────────────────────────────────────────────────────
USER_CSV_PATH  = r"D:\TrackBot\user_id.csv"
COLLECTION_URL = "https://luminabot.net/api/players/{user_id}/collection"
INVENTORY_URL  = "https://luminabot.net/api/players/{user_id}/inventory"

COLLECTION_PAGE_SIZE = 20    # the site's own front end uses 20
MAX_PAGES_PER_PLAYER = 3     # only used while a whole page is still "fresh"

SCAN_CONCURRENCY = 12        # max in-flight collection requests (steady, non-bursting)
SCAN_MAX_RPS     = 14.0      # ceiling for requests/second (comfortable, avoids 429 rate limits)
SCAN_MIN_RPS     = 4.0       # floor after repeated 429s
REQUEST_TIMEOUT  = 8         # seconds per request
REQUEST_RETRIES  = 2

# track4.py waits this long for an owner before sending the plain message
# (the owner is then added by EDITING that message if found later).
# The tracking alert is time-sensitive. Send it immediately and edit in the
# owner later, instead of holding the subscriber's notification for a scan.
OWNER_WAIT_SECONDS       = 0.25
# A full directory scan is about ten minutes at the conservative shared rate.
# It runs in the background after the alert is posted, then edits that alert.
OWNER_SEARCH_MAX_SECONDS = 12 * 60.0
OWNER_MAX_REPORTED       = 3      # cap when the pool jumps by many at once
OWNER_GRACE_SECONDS      = 0.75   # after the 1st match, collect other in-flight matches

# "Just pulled" = the copy's inventoryTs is within this many seconds before the
# detection. Without this, the first player who merely OWNS the card would match.
REQUIRE_FRESH        = True
FRESH_WINDOW_SECONDS = 180

ACTIVE_WINDOW_SECONDS = 1800   # players seen pulling within 30 min go first
INDEX_TTL_SECONDS     = 900
HOT_LIST_SIZE         = 500
FORBIDDEN_ABORT_AFTER = 25     # this many NON-private 403s in a row => treat as a site-wide block
PRIVATE_USERS_PATH    = "private_users.txt"   # players with private profiles; never re-requested
HOARD_CACHE_DIR       = r"D:\TrackBot\hoard_cache"
HOARD_CACHE_TTL       = 6 * 60 * 60
FRAME_CACHE_DIR       = r"D:\TrackBot\frame_cache"
FRAME_CACHE_TTL       = 6 * 60 * 60
# ─────────────────────────────────────────────────────────────────────────────


class _RateGate:
    """Spaces requests >= 1/rps apart. On 429: pause, and halve rps at most once
    per second (a burst of simultaneous 429s counts as ONE event). Recovers
    multiplicatively after a run of successes."""

    def __init__(self, rps: float):
        self.max_rps = rps
        self.rps = rps
        self._next = 0.0
        self._ok = 0
        self._last_cut = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        start = max(now, self._next)
        self._next = start + 1.0 / self.rps
        if start > now:
            await asyncio.sleep(start - now)

    def penalize(self, seconds: float) -> None:
        now = time.monotonic()
        self._next = max(self._next, now + seconds)
        self._ok = 0
        if now - self._last_cut >= 1.0:
            old_rps = self.rps
            self.rps = max(SCAN_MIN_RPS, self.rps * 0.75)
            self._last_cut = now
            print(f"⚠️  Collection API rate limit: reducing scan rate {old_rps:.1f} → {self.rps:.1f} req/s")

    def success(self) -> None:
        self._ok += 1
        if self._ok >= 15 and self.rps < self.max_rps:
            self.rps = min(self.max_rps, self.rps * 1.15)
            self._ok = 0


def _unwrap(data) -> tuple[list, int, int] | None:
    """Accept {"cards": [...], "pagination": {...}} or a bare list."""
    if isinstance(data, list):
        return data, 1, len(data)
    if isinstance(data, dict) and isinstance(data.get("cards"), list):
        pag = data.get("pagination") or {}
        try:
            total_pages = int(pag.get("totalPages", 1))
        except (TypeError, ValueError):
            total_pages = 1
        try:
            total_count = int(pag.get("totalCount", len(data["cards"])))
        except (TypeError, ValueError):
            total_count = len(data["cards"])
        return data["cards"], total_pages, total_count
    return None


def _ts_seconds(value) -> float | None:
    """inventoryTs is epoch milliseconds."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 1e11 else v


def _retry_after(value) -> float:
    try:
        return min(max(float(value), 0.5), 30.0)
    except (TypeError, ValueError):
        return 2.0


def collection_url(user_id: str, *, page: int = 1, page_size: int = COLLECTION_PAGE_SIZE,
                   rarity: str = "", series: str = "", character: str = "", banner: str = "") -> str:
    """Build the one collection URL used by both owner and hoard searches."""
    params = {"page": page, "pageSize": page_size}
    if rarity:
        params["rarity"] = rarity
    if series:
        params["seriesSearch"] = series
    if character:
        params["characterSearch"] = character
    if banner:
        params["banner"] = banner
    return f"{COLLECTION_URL.format(user_id=user_id)}?{urlencode(params)}"


def hoard_cache_filename(character: str, series: str, rarity: str = "", banner: str = "") -> str:
    """Stable, filesystem-safe cache key for one exact search."""
    r_part = rarity.strip().upper() if rarity and rarity.strip().upper() not in ("ALL", "ANY") else "ALL"
    b_part = banner.strip().capitalize() if banner and banner.strip().upper() not in ("ALL", "ANY") else "ALL"
    raw = f"{character.strip()}_{series.strip()}_{r_part}_{b_part}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_") or "search"
    return f"hoard_{safe}.json"


KNOWN_FRAMES: set[str] = {
    "101EXE (Gift)", "Amor (Gift)", "Ancient Ruins (Gift)", "Apple", "Apple Orange",
    "Aqualis Blue", "Aqualis Sunset", "Aqualis Teal", "Aqualis Twilight", "Aqualis White",
    "Ascension (Gift)", "Autumn Bloom Black", "Autumn Bloom Green", "Autumn Bloom Teal",
    "Autumn Bloom Twilight", "Autumn Bloom White", "Baron (Gift)", "Blood (Gift)",
    "Blood Negative", "Bloodbriar Lavender", "Butterfly (Gift)", "Calla (Gift)",
    "Calla Emerald", "Calla Pink", "Calla Scarlet", "Celestial (Gift)", "Chocolates (Gift)",
    "Church (Gift)", "Cocoa (Gift)", "Coffin (Gift)", "Coffin Green", "Cookie (Gift)",
    "Crystalis Purple", "Cupid (Gift)", "Curtain (Gift)", "Cybersigil (Gift)",
    "Cybersigil Emerald", "Cybersigil Red", "Devil (Gift)", "Dewdrop (Gift)",
    "Duality (Gift)", "Empyrean (Gift)", "Enma Cottoncandy", "Enma Orange", "Enma Scarlet",
    "Enma White", "Equilibrium (Gift)", "Everbloom (Gift)", "Fangs Blue", "Fangs Emerald",
    "Fangs Negative", "Fire Temple Midnight", "Froggy Black", "Froggy Fuchsia",
    "Froggy Green Orange", "Fruits Blue", "Fruits Orange", "Fruits Pink", "Fruits Purple",
    "Fruits Red", "Fruits Yellow", "Garden Rose Blood", "Garden Rose Glitch",
    "Garden Rose Iridescent", "Garden Rose Scarlet", "Garden Rose White", "Harbinger (Gift)",
    "Hawaii (Gift)", "Haze (Gift)", "Heavens Feel (Gift)", "Hydrangea (Gift)",
    "Iridium (Gift)", "Kitty (Gift)", "Kitty Emerald", "Kitty Yellow", "Laces (Gift)",
    "Lacrimosa Red", "Lilith (Gift)", "Livestream (Gift)", "Lotus (Gift)", "Lovesick (Gift)",
    "Lumiphone (Gift)", "Magick (Gift)", "Magick Black Red", "Manga Green", "Manga Red",
    "Mariposa (Gift)", "Mortifera (Gift)", "Music Scarlet", "Navigation (Gift)",
    "Neon Rainbow", "Neon Scarlet", "Ocean Rainbow", "Ocean Scarlet", "Odette (Gift)",
    "Ornate Scarlet", "Palm Quire (Gift)", "Planetary (Gift)", "Poppies (Gift)",
    "Postal Stamp (Gift)", "Prismatic", "Prismatic White", "Pumpkin (Gift)", "Puppy (Gift)",
    "Queen of Hearts (Gift)", "Queen of Hearts Black Gold", "Queen of Hearts Scarlet",
    "Romance Black", "Rosen Lauren Black", "Rosen Lauren Grey", "Rosen Lauren Scarlet",
    "Sakura Grace Black", "Secret Wings (Gift)", "Secret Wings Red", "Secret Wings Scarlet",
    "Secret Wings White", "Selene (Gift)", "Seraphim (Gift)", "Serenity Iridescent",
    "Siren (Gift)", "Siren Fuchsia", "Siren Teal", "Spirit (Gift)", "Stargaze Black",
    "Stargaze Blue", "Stargaze Iridescent", "Stargaze White", "Starshell (Gift)",
    "Stellar (Gift)", "Stellar Emerald", "Stellar Green", "Stellar Scarlet",
    "Strawberry Bliss (Gift)", "Summer Treats (Gift)", "Sweetie (Gift)", "System Emerald",
    "Teatime (Gift)", "Thunder Beige", "Thunder Black Purple", "Thunder Fuchsia",
    "Thunder White", "Ticket (Gift)", "Toploader (Gift)", "Trick Or Treat (Gift)",
    "Trigger White", "Tropical (Gift)", "Tsuki (Gift)", "Tsuki Black", "Tsuki Iridescent",
    "Tsuki Orange", "Wanted", "Wanted Black", "Wanted White", "Wedding Black",
    "Wedding Blood", "Wisteria", "Wisteria (Gift)", "Wisteria Black", "Wisteria Gold",
    "Wisteria Iridescent", "Wisteria Pink", "Xoxo Iridescent", "Zafkiel", "Zafkiel Iridescent",
    "Zafkiel Yellow", "Zenith (Gift)"
}


def parse_frame_targets(frame_query: str) -> list[str]:
    """Split comma-separated frame names and clean them up."""
    parts = [p.strip() for p in frame_query.split(",") if p.strip()]
    cleaned = []
    seen = set()
    for p in parts:
        c = p[2:].strip() if p.lower().startswith("f:") else p
        if c.lower() not in seen:
            seen.add(c.lower())
            cleaned.append(c)
    return cleaned


def frame_cache_filename(frames: list[str]) -> str:
    """Stable, filesystem-safe cache key for a frame search."""
    normalized = sorted(f.strip().lower() for f in frames if f.strip())
    raw = "_".join(normalized) or "all_frames"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")
    if len(safe) > 100:
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        safe = f"{safe[:90]}_{h}"
    return f"frame_{safe}.json"


def _atomic_json_write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".hoard-", suffix=".json", dir=os.path.dirname(path), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


class OwnerFinder:
    def __init__(self, get_session: Callable[[], Awaitable[aiohttp.ClientSession]],
                 csv_path: str = USER_CSV_PATH):
        self._get_session = get_session
        self._csv_path = csv_path
        self._names: dict[str, str] = {}      # user_id -> username
        self._order: list[str] = []           # CSV order
        self._mtime: float | None = None
        self._gate = _RateGate(SCAN_MAX_RPS)
        self._hot: dict[str, float] = {}      # user_id -> last activity (epoch s)
        self._recent: dict[int, list[dict]] = {}  # catalog id -> fresh copies seen
        self._claimed: dict[str, float] = {}  # userCardId -> time attributed
        self._private: set[str] = self._load_private()   # profiles that answered "private"
        self._forbidden_run = 0               # consecutive NON-private 403s (site-wide block detector)
        self._blocked = False

    # ── private profiles (403 "Profile is private") ──────────────────────────
    @staticmethod
    def _load_private() -> set:
        try:
            with open(PRIVATE_USERS_PATH, encoding="utf-8") as f:
                return {ln.strip() for ln in f if ln.strip().isdigit()}
        except OSError:
            return set()

    def _mark_private(self, uid: str) -> None:
        if uid in self._private:
            return
        self._private.add(uid)
        try:
            with open(PRIVATE_USERS_PATH, "a", encoding="utf-8") as f:
                f.write(uid + "\n")
        except OSError:
            pass

    # ── user_id.csv ──────────────────────────────────────────────────────────
    def load_users(self, force: bool = False) -> int:
        """Load user_id.csv into memory. Cheap no-op unless the file changed."""
        try:
            mtime = os.path.getmtime(self._csv_path)
        except OSError as e:
            print(f"⚠️  Can't read {self._csv_path}: {e!r}")
            return len(self._names)
        if not force and mtime == self._mtime:
            return len(self._names)
        names: dict[str, str] = {}
        try:
            with open(self._csv_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    uid = (row.get("user_id") or "").strip()
                    if uid.isdigit() and uid not in names:
                        names[uid] = (row.get("username") or "").strip() or "unknown"
        except (OSError, csv.Error) as e:
            print(f"⚠️  Couldn't load {self._csv_path}: {e!r}")
            return len(self._names)
        self._names, self._order, self._mtime = names, list(names), mtime
        print(f"👥  Loaded {len(names)} player(s) from {self._csv_path}")
        return len(names)

    # ── bookkeeping ──────────────────────────────────────────────────────────
    def _touch(self, uid: str, ts: float) -> None:
        if ts > self._hot.get(uid, 0):
            self._hot[uid] = ts
        if len(self._hot) > HOT_LIST_SIZE * 2:
            keep = sorted(self._hot.items(), key=lambda kv: -kv[1])[:HOT_LIST_SIZE]
            self._hot = dict(keep)

    def _index_add(self, card: dict, uid: str, ts: float, cid: int) -> None:
        ucid = str(card.get("userCardId") or "")
        if not ucid:
            return
        lst = self._recent.setdefault(cid, [])
        if any(e["ucid"] == ucid for e in lst):
            return
        lst.append({"uid": uid, "ucid": ucid, "ts": ts, "rarity": card.get("rarity")})
        if len(lst) > 50:
            del lst[0]

    def _prune(self, now: float) -> None:
        cutoff = now - INDEX_TTL_SECONDS
        for cid in list(self._recent):
            kept = [e for e in self._recent[cid] if e["ts"] >= cutoff]
            if kept:
                self._recent[cid] = kept
            else:
                del self._recent[cid]
        for ucid in [k for k, t in self._claimed.items() if t < now - 3600]:
            del self._claimed[ucid]

    def _candidates(self):
        """Hot players (recently active / recently matched) first, then CSV order."""
        now = time.time()
        hot = sorted(((u, t) for u, t in self._hot.items()
                      if t >= now - ACTIVE_WINDOW_SECONDS and u in self._names
                      and u not in self._private),
                     key=lambda kv: -kv[1])
        seen = set()
        for uid, _ in hot:
            seen.add(uid)
            yield uid
        for uid in self._order:
            if uid not in seen and uid not in self._private:
                yield uid

    # ── HTTP ─────────────────────────────────────────────────────────────────
    async def _fetch_page(self, uid: str, page: int, rarity: str,
                          *, page_size: int = COLLECTION_PAGE_SIZE,
                          series: str = "", character: str = "", banner: str = ""):
        url = collection_url(uid, page=page, page_size=page_size, rarity=rarity,
                             series=series, character=character, banner=banner)
        session = await self._get_session()
        # The collection endpoint needs a logged-in session. LUMINA_COOKIE comes
        # from .env (never hard-code it, never commit it); read per request so
        # you can paste a fresh one without restarting.
        headers = {"Referer": f"https://luminabot.net/players/{uid}?tab=collection"}
        cookie = os.environ.get("LUMINA_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        for attempt in range(REQUEST_RETRIES + 1):
            await self._gate.wait()
            retry_after = None
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    status = resp.status
                    if status == 200:
                        data = await resp.json(content_type=None)
                    else:
                        data = await resp.text() if status == 403 else None
                        retry_after = resp.headers.get("Retry-After")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                print(f"⚠️  Collection request failed for {uid} (attempt {attempt + 1}): {e!r}")
                await asyncio.sleep(0.3 * (attempt + 1))
                continue

            if status == 403 and "private" in (data or "").lower():
                self._mark_private(uid)         # normal: this player hid their collection
                print(f"ℹ️  Collection is private for {uid}")
                return None
            if status == 403:
                self._forbidden_run += 1
                if self._forbidden_run >= FORBIDDEN_ABORT_AFTER and not self._blocked:
                    self._blocked = True
                    print(f"🚫  {FORBIDDEN_ABORT_AFTER} consecutive HTTP 403s from the collection endpoint — "
                          f"looks like a site-wide refusal (not 'Profile is private'). Owner search aborted; run probe_api.py.")
                return None
            self._forbidden_run = 0
            self._blocked = False
            if status == 200:
                self._private.discard(uid)
                self._gate.success()
                return _unwrap(data)            # (cards, total_pages) or None
            if status == 429:
                self._gate.penalize(_retry_after(retry_after))
                continue
            if status in (400, 404):
                return None                     # missing/private player — skip
            await asyncio.sleep(0.3 * (attempt + 1))
        return None

    # ── core ─────────────────────────────────────────────────────────────────
    async def _check_player(self, uid: str, card_id: int, rarity: str, detected_at: float,
                            series: str = "", character: str = ""):
        """Return the matching card dict if this player just pulled card_id."""
        cutoff = detected_at - FRESH_WINDOW_SECONDS
        now = time.time()
        for page in range(1, MAX_PAGES_PER_PLAYER + 1):
            res = await self._fetch_page(uid, page, rarity, series=series, character=character)
            if res is None:
                return None
            cards, total_pages, _total_count = res
            page_all_fresh = bool(cards)
            match = None
            for c in cards:
                ts = _ts_seconds(c.get("inventoryTs"))
                try:
                    cid = int(c.get("cardCatalogId"))
                except (TypeError, ValueError):
                    cid = None
                if ts is not None:
                    if ts >= now - ACTIVE_WINDOW_SECONDS:
                        self._touch(uid, ts)
                    if cid is not None and ts >= now - INDEX_TTL_SECONDS:
                        self._index_add(c, uid, ts, cid)
                if ts is not None and ts < cutoff:
                    page_all_fresh = False
                # The correct comparison: cardCatalogId, NOT id / userCardId.
                if (match is None and cid == card_id
                        and str(c.get("userCardId")) not in self._claimed
                        and (not REQUIRE_FRESH or ts is None or ts >= cutoff)):
                    match = {"user_card_id": str(c.get("userCardId") or ""), "ts": ts or now}
                # (no early return: finish the page so the rest of a 10-pull gets indexed)
            if match:
                return match
            # Newest-first order: only look deeper if this whole page was fresh.
            if not page_all_fresh or page >= total_pages:
                break
        return None

    async def find_owners(self, card_id, rarity: str, detected_at: float | None = None,
                          want: int = 1, max_seconds: float = OWNER_SEARCH_MAX_SECONDS,
                          series: str = "", character: str = "") -> list[dict]:
        """Return up to `want` players who just pulled this card:
        [{"user_id", "username", "user_card_id", "ts"}, ...]. Never raises."""
        try:
            return await self._find_owners(int(card_id), rarity,
                                           detected_at or time.time(), max(1, want), max_seconds,
                                           series, character)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️  find_owners error for card #{card_id}: {e!r}")
            return []

    async def collection_total(self, uid: str, rarity: str, series: str, character: str, banner: str) -> int | None:
        """Return pagination.totalCount for one filtered collection, or None on failure."""
        result = await self._fetch_page(uid, 1, rarity, page_size=1,
                                        series=series, character=character, banner=banner)
        return result[2] if result is not None else None

    async def fetch_inventory(self, uid: str) -> dict | None:
        """Fetch /api/players/{uid}/inventory. Returns parsed dict or None on failure/private."""
        url = INVENTORY_URL.format(user_id=uid)
        session = await self._get_session()
        headers = {
            "Referer": f"https://luminabot.net/players/{uid}?tab=inventory",
            "Accept": "application/json",
        }
        cookie = os.environ.get("LUMINA_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        for attempt in range(REQUEST_RETRIES + 1):
            await self._gate.wait()
            retry_after = None
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    status = resp.status
                    if status == 200:
                        data = await resp.json(content_type=None)
                    else:
                        data = await resp.text() if status == 403 else None
                        retry_after = resp.headers.get("Retry-After")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                print(f"⚠️  Inventory request failed for {uid} (attempt {attempt + 1}): {e!r}")
                await asyncio.sleep(0.3 * (attempt + 1))
                continue

            if status == 403 and "private" in (data or "").lower():
                self._mark_private(uid)
                return None
            if status == 403:
                self._forbidden_run += 1
                if self._forbidden_run >= FORBIDDEN_ABORT_AFTER and not self._blocked:
                    self._blocked = True
                    print(f"🚫  {FORBIDDEN_ABORT_AFTER} consecutive HTTP 403s from the inventory endpoint — "
                          f"looks like a site-wide refusal (not 'Profile is private').")
                return None
            self._forbidden_run = 0
            self._blocked = False
            if status == 200:
                self._private.discard(uid)
                self._gate.success()
                return data if isinstance(data, dict) else None
            if status == 429:
                self._gate.penalize(_retry_after(retry_after))
                continue
            if status in (400, 404):
                return None
            await asyncio.sleep(0.3 * (attempt + 1))
        return None

    async def fetch_frames(self, uid: str) -> list[dict] | None:
        """Return the items list inside the frames section of the player's inventory, or None on failure."""
        data = await self.fetch_inventory(uid)
        if data is None:
            return None
        sections = data.get("sections")
        if not isinstance(sections, list):
            return []
        for sec in sections:
            if isinstance(sec, dict) and (sec.get("kind") == "frames" or str(sec.get("title", "")).lower() == "frames"):
                items = sec.get("items")
                return items if isinstance(items, list) else []
        return []

    async def _find_owners(self, card_id: int, rarity: str, detected_at: float,
                           want: int, max_seconds: float, series: str, character: str) -> list[dict]:
        self.load_users()
        if not self._names:
            return []
        print(f"🔎 Owner lookup: card #{card_id}, rarity={rarity}, character={character!r}, series={series!r}, players={len(self._names)}")
        self._blocked = False
        self._forbidden_run = 0
        self._prune(time.time())
        cutoff = detected_at - FRESH_WINDOW_SECONDS

        # 1) Zero-request shortcut: copies already seen on earlier scans.
        if REQUIRE_FRESH:
            hits = []
            for e in sorted(self._recent.get(card_id, []), key=lambda e: -e["ts"]):
                if e["ts"] >= cutoff and e["rarity"] == rarity and e["ucid"] not in self._claimed:
                    hits.append(e)
                    if len(hits) >= want:
                        break
            if hits:
                out = []
                for e in hits:
                    self._claimed[e["ucid"]] = time.time()
                    self._touch(e["uid"], e["ts"])
                    out.append({"user_id": e["uid"], "username": self._names.get(e["uid"], "unknown"),
                                "user_card_id": e["ucid"], "ts": e["ts"]})
                return out

        # 2) Bounded-concurrency scan, hot players first.
        found: dict[str, dict] = {}
        done = asyncio.Event()
        it = self._candidates()
        loop = asyncio.get_running_loop()
        grace_set = False

        async def worker():
            nonlocal grace_set
            while not done.is_set() and not self._blocked:
                uid = next(it, None)          # sync iterator: safe across tasks
                if uid is None:
                    return
                hit = await self._check_player(uid, card_id, rarity, detected_at, series, character)
                if hit and uid not in found and str(hit["user_card_id"]) not in self._claimed:
                    self._claimed[hit["user_card_id"]] = time.time()
                    self._touch(uid, hit["ts"])
                    found[uid] = {"user_id": uid, "username": self._names.get(uid, "unknown"), **hit}
                    print(f"✅ Owner lookup: #{card_id} matched {found[uid]['username']} ({uid})")
                    if len(found) >= want:
                        done.set()
                    elif not grace_set:       # arrival order wins; give parallel matches a moment
                        grace_set = True
                        loop.call_later(OWNER_GRACE_SECONDS, done.set)

        workers = [asyncio.ensure_future(worker()) for _ in range(SCAN_CONCURRENCY)]
        all_done = asyncio.ensure_future(asyncio.gather(*workers, return_exceptions=True))
        evt = asyncio.ensure_future(done.wait())
        try:
            await asyncio.wait({all_done, evt}, timeout=max_seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            done.set()
            for t in (*workers, all_done, evt):
                t.cancel()
            await asyncio.gather(*workers, all_done, evt, return_exceptions=True)
        results = sorted(found.values(), key=lambda o: -o["ts"])
        print(f"ℹ️  Owner lookup complete: card #{card_id}, matches={len(results)}, blocked={self._blocked}")
        return results


class HoardScanner:
    """Resumable, rate-limited all-player collection scan using OwnerFinder's HTTP path."""
    def __init__(self, finder: OwnerFinder, cache_dir: str = HOARD_CACHE_DIR):
        self.finder = finder
        self.cache_dir = cache_dir
        self._active_searches: dict[tuple, asyncio.Task] = {}

    def _path(self, character: str, series: str, rarity: str = "", banner: str = "") -> str:
        return os.path.join(self.cache_dir, hoard_cache_filename(character, series, rarity, banner))

    def _load(self, path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    async def search(self, *, character: str, series: str, rarity: str = "", banner: str = "",
                     on_progress: Callable[[dict], Awaitable[None]] | None = None) -> dict:
        character = character.strip()
        series = series.strip()
        rarity = rarity.strip().upper() if rarity and rarity.strip().upper() not in ("ALL", "ANY") else ""
        banner = banner.strip().capitalize() if banner and banner.strip().upper() not in ("ALL", "ANY") else ""
        key = (character.lower(), series.lower(), rarity, banner.lower())

        if key in self._active_searches:
            task = self._active_searches[key]
            return await task

        loop = asyncio.get_running_loop()
        task = loop.create_task(self._do_search(
            character=character, series=series, rarity=rarity, banner=banner,
            on_progress=on_progress
        ))
        self._active_searches[key] = task
        try:
            return await task
        finally:
            self._active_searches.pop(key, None)

    async def _do_search(self, *, character: str, series: str, rarity: str = "", banner: str = "",
                         on_progress: Callable[[dict], Awaitable[None]] | None = None) -> dict:
        self.finder.load_users()
        players = self.finder._names
        if not players:
            raise RuntimeError("No valid players were loaded from user_id.csv")
        path = self._path(character, series, rarity, banner)
        cached = self._load(path)
        now = time.time()
        search = {"character": character, "series": series, "rarity": rarity, "banner": banner}
        same_search = cached and cached.get("search") == search
        if same_search and cached.get("status") == "complete" and now - float(cached.get("updated_at", 0)) < HOARD_CACHE_TTL:
            cached["cache_hit"] = True
            cached["leaderboard"] = sorted(cached.get("results", []), key=lambda r: (-r["total_count"], r["username"].lower(), r["user_id"]))
            cached["successful_requests"] = cached.get("successful_requests", cached.get("total_players", len(players)))
            cached["failed_requests"] = 0
            return cached

        prior = cached if same_search else {}
        results = {str(r["user_id"]): r for r in prior.get("results", []) if str(r.get("user_id", "")) in players}

        raw_scanned = prior.get("scanned_user_ids")
        if isinstance(raw_scanned, list):
            scanned = set(str(uid) for uid in raw_scanned if str(uid) in players)
        else:
            # If migrating from legacy cache without scanned_user_ids, only trust confirmed hits
            scanned = set(results.keys())

        # Any player in user_id.csv not yet scanned must be scanned
        todo = [uid for uid in players if uid not in scanned]
        failed_network: set[str] = set()

        state = {
            "search": search,
            "started_at": prior.get("started_at", now),
            "updated_at": now,
            "status": "partial",
            "results": list(results.values()),
            "scanned_user_ids": list(scanned),
            "pending_user_ids": todo,
            "total_players": len(players),
            "successful_requests": len(scanned),
            "failed_requests": 0,
            "cache_hit": False,
        }
        save_lock = asyncio.Lock()
        processed = 0

        async def checkpoint() -> None:
            state["results"] = list(results.values())
            state["scanned_user_ids"] = list(scanned)
            state["pending_user_ids"] = [uid for uid in players if uid not in scanned]
            state["successful_requests"] = len(scanned)
            state["failed_requests"] = len(failed_network)
            state["updated_at"] = time.time()
            async with save_lock:
                await asyncio.to_thread(_atomic_json_write, path, state)

        queue: asyncio.Queue[str] = asyncio.Queue()
        for uid in todo:
            queue.put_nowait(uid)

        stop_progress = asyncio.Event()

        async def progress_loop() -> None:
            if not on_progress:
                return
            while not stop_progress.is_set():
                try:
                    await asyncio.wait_for(stop_progress.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    pass
                if stop_progress.is_set():
                    break
                try:
                    prog = {
                        "scanned": len(scanned),
                        "total": len(players),
                        "found": len(results),
                        "rps": self.finder._gate.rps,
                        "processed": processed,
                    }
                    await on_progress(prog)
                except Exception:
                    pass

        async def worker() -> None:
            nonlocal processed
            while True:
                try:
                    uid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    count = await self.finder.collection_total(uid, rarity, series, character, banner)
                    if count is None:
                        if uid in self.finder._private:
                            # User profile is private; successfully checked
                            scanned.add(uid)
                            failed_network.discard(uid)
                        else:
                            # Transient network/server error; retry on next scan
                            failed_network.add(uid)
                    else:
                        scanned.add(uid)
                        failed_network.discard(uid)
                        if count > 0:
                            results[uid] = {"username": players[uid], "user_id": uid, "total_count": count}
                        else:
                            results.pop(uid, None)
                except Exception as exc:
                    print(f"⚠️  Hoard lookup failed for {uid}: {exc!r}")
                    failed_network.add(uid)
                finally:
                    queue.task_done()
                    processed += 1
                    if processed % 100 == 0:
                        await checkpoint()

        r_label = rarity or "All"
        b_label = banner or "All"
        print(f"🏆 Hoard scan: {len(todo)} player(s) to scan out of {len(players)}, {character!r} / {series!r} / {r_label} / {b_label}")
        progress_task = asyncio.create_task(progress_loop())
        try:
            await asyncio.gather(*(worker() for _ in range(SCAN_CONCURRENCY)))
        finally:
            stop_progress.set()
            await progress_task

        state["successful_requests"] = len(scanned)
        state["failed_requests"] = len(failed_network)
        state["leaderboard"] = sorted(results.values(), key=lambda r: (-r["total_count"], r["username"].lower(), r["user_id"]))
        state["status"] = "complete" if len(scanned) >= len(players) else "partial"
        await checkpoint()
        print(f"🏆 Hoard scan complete: successes={state['successful_requests']}, failures={state['failed_requests']}, results={len(results)}")
        return state


class FrameScanner:
    """Resumable, rate-limited all-player inventory frame scan using OwnerFinder's HTTP path."""
    def __init__(self, finder: OwnerFinder, cache_dir: str = FRAME_CACHE_DIR):
        self.finder = finder
        self.cache_dir = cache_dir
        self._active_searches: dict[tuple, asyncio.Task] = {}

    def _path(self, frames: list[str]) -> str:
        return os.path.join(self.cache_dir, frame_cache_filename(frames))

    def _load(self, path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    async def search(self, *, frames: list[str],
                     on_progress: Callable[[dict], Awaitable[None]] | None = None) -> dict:
        frames = parse_frame_targets(",".join(frames))
        if not frames:
            raise ValueError("At least one frame name must be specified.")
        key = tuple(sorted(f.lower() for f in frames))

        if key in self._active_searches:
            task = self._active_searches[key]
            return await task

        loop = asyncio.get_running_loop()
        task = loop.create_task(self._do_search(frames=frames, on_progress=on_progress))
        self._active_searches[key] = task
        try:
            return await task
        finally:
            self._active_searches.pop(key, None)

    async def _do_search(self, *, frames: list[str],
                         on_progress: Callable[[dict], Awaitable[None]] | None = None) -> dict:
        self.finder.load_users()
        players = self.finder._names
        if not players:
            raise RuntimeError("No valid players were loaded from user_id.csv")

        path = self._path(frames)
        cached = self._load(path)
        now = time.time()

        norm_key = sorted(f.lower() for f in frames)
        cached_norm = sorted(f.lower() for f in cached.get("frames", [])) if cached else []
        same_search = cached and cached_norm == norm_key

        if same_search and cached.get("status") == "complete" and now - float(cached.get("updated_at", 0)) < FRAME_CACHE_TTL:
            cached["cache_hit"] = True
            cached["leaderboard"] = sorted(cached.get("results", []), key=lambda r: (-r["total_count"], r["username"].lower(), r["user_id"]))
            cached["successful_requests"] = cached.get("successful_requests", cached.get("total_players", len(players)))
            cached["failed_requests"] = 0
            return cached

        prior = cached if same_search else {}
        results = {str(r["user_id"]): r for r in prior.get("results", []) if str(r.get("user_id", "")) in players}

        raw_scanned = prior.get("scanned_user_ids")
        if isinstance(raw_scanned, list):
            scanned = set(str(uid) for uid in raw_scanned if str(uid) in players)
        else:
            scanned = set(results.keys())

        todo = [uid for uid in players if uid not in scanned]
        failed_network: set[str] = set()

        state = {
            "frames": frames,
            "started_at": prior.get("started_at", now),
            "updated_at": now,
            "status": "partial",
            "results": list(results.values()),
            "scanned_user_ids": list(scanned),
            "pending_user_ids": todo,
            "total_players": len(players),
            "successful_requests": len(scanned),
            "failed_requests": 0,
            "cache_hit": False,
        }
        save_lock = asyncio.Lock()
        processed = 0

        async def checkpoint() -> None:
            state["results"] = list(results.values())
            state["scanned_user_ids"] = list(scanned)
            state["pending_user_ids"] = [uid for uid in players if uid not in scanned]
            state["successful_requests"] = len(scanned)
            state["failed_requests"] = len(failed_network)
            state["updated_at"] = time.time()
            async with save_lock:
                await asyncio.to_thread(_atomic_json_write, path, state)

        queue: asyncio.Queue[str] = asyncio.Queue()
        for uid in todo:
            queue.put_nowait(uid)

        stop_progress = asyncio.Event()

        async def progress_loop() -> None:
            if not on_progress:
                return
            while not stop_progress.is_set():
                try:
                    await asyncio.wait_for(stop_progress.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    pass
                if stop_progress.is_set():
                    break
                try:
                    prog = {
                        "scanned": len(scanned),
                        "total": len(players),
                        "found": len(results),
                        "rps": self.finder._gate.rps,
                        "processed": processed,
                    }
                    await on_progress(prog)
                except Exception:
                    pass

        norm_targets = {f.casefold(): f for f in frames}

        async def worker() -> None:
            nonlocal processed
            while True:
                try:
                    uid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    items = await self.finder.fetch_frames(uid)
                    if items is None:
                        if uid in self.finder._private:
                            scanned.add(uid)
                            failed_network.discard(uid)
                        else:
                            failed_network.add(uid)
                    else:
                        scanned.add(uid)
                        failed_network.discard(uid)
                        matched_count = 0
                        breakdown: dict[str, int] = {}
                        for item in items:
                            name = item.get("name") or ""
                            key_part = (item.get("key") or "").removeprefix("f:").strip()
                            if name:
                                KNOWN_FRAMES.add(name)
                            if name.casefold() in norm_targets or key_part.casefold() in norm_targets:
                                canonical = norm_targets.get(name.casefold()) or norm_targets.get(key_part.casefold()) or name
                                try:
                                    cnt = int(item.get("count", 1))
                                except (TypeError, ValueError):
                                    cnt = 1
                                breakdown[canonical] = breakdown.get(canonical, 0) + cnt
                                matched_count += cnt
                        if matched_count > 0:
                            results[uid] = {
                                "username": players[uid],
                                "user_id": uid,
                                "total_count": matched_count,
                                "breakdown": breakdown,
                            }
                        else:
                            results.pop(uid, None)
                except Exception as exc:
                    print(f"⚠️  Frame lookup failed for {uid}: {exc!r}")
                    failed_network.add(uid)
                finally:
                    queue.task_done()
                    processed += 1
                    if processed % 100 == 0:
                        await checkpoint()

        frames_label = ", ".join(frames)
        print(f"🖼️ Frame scan: {len(todo)} player(s) to scan out of {len(players)}, frames={frames_label!r}")
        progress_task = asyncio.create_task(progress_loop())
        try:
            await asyncio.gather(*(worker() for _ in range(SCAN_CONCURRENCY)))
        finally:
            stop_progress.set()
            await progress_task

        state["successful_requests"] = len(scanned)
        state["failed_requests"] = len(failed_network)
        state["leaderboard"] = sorted(results.values(), key=lambda r: (-r["total_count"], r["username"].lower(), r["user_id"]))
        state["status"] = "complete" if len(scanned) >= len(players) else "partial"
        await checkpoint()
        print(f"🖼️ Frame scan complete: successes={state['successful_requests']}, failures={state['failed_requests']}, results={len(results)}")
        return state
