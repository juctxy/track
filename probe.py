"""
probe_api.py — diagnoses the collection endpoint.
Run:  python probe_api.py <user_id> [second_user_id]
Uses the SAME headers as track4.py (full Chrome UA) so results match the bot.
"""
import asyncio, os, sys, time, aiohttp
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
BOT_HEADERS = {"User-Agent": UA, "Accept": "application/json", "Referer": "https://luminabot.net/cards"}
B = "https://luminabot.net/api/players/{u}/collection"
Q = "?page=1&pageSize=20&rarity=SSR"

async def get(s, url, headers):
    async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
        body = await r.text()
        return r.status, body, r.headers

def cards(body):
    import json
    try: d = json.loads(body)
    except Exception: return None, None
    return (d.get("cards", []), d.get("pagination")) if isinstance(d, dict) else (d, None)

async def main(uid, uid2):
    async with aiohttp.ClientSession() as s:
        print("[0] control: pool endpoint with bot headers (known to work in the bot)")
        st, body, h = await get(s, "https://luminabot.net/api/cards/pool?cardCatalogId=43093", BOT_HEADERS)
        print(f"    status={st}  server={h.get('server')}")

        cookie = os.environ.get("LUMINA_COOKIE", "").strip()
        print(f"    LUMINA_COOKIE in .env: {'set (' + str(len(cookie)) + ' chars, not shown)' if cookie else 'NOT set'}")
        authed = {**BOT_HEADERS, "Referer": f"https://luminabot.net/players/{uid}?tab=collection"}
        if cookie: authed["Cookie"] = cookie
        variants = {
            **({"with LUMINA_COOKIE":     authed} if cookie else {}),
            "bot headers":               BOT_HEADERS,
            "referer=/players/<id>":     {**BOT_HEADERS, "Referer": f"https://luminabot.net/players/{uid}"},
            "referer=/profile/<id>":     {**BOT_HEADERS, "Referer": f"https://luminabot.net/profile/{uid}"},
            "+Origin, +Accept-Language": {**BOT_HEADERS, "Origin": "https://luminabot.net", "Accept-Language": "en-US,en;q=0.9"},
        }
        print("[1] collection endpoint, header variants")
        working = None
        for name, hdr in variants.items():
            st, body, h = await get(s, B.format(u=uid) + Q, hdr)
            print(f"    {name:28s} status={st}  server={h.get('server')}  cf-mitigated={h.get('cf-mitigated')}")
            if st != 200 and name == "bot headers": print("      body:", body[:300].replace("\n", " "))
            if st == 200 and working is None: working = hdr
        if uid2:
            st, body, _ = await get(s, B.format(u=uid2) + Q, BOT_HEADERS)
            print(f"[1b] second user {uid2}: status={st}  (differs from first => per-player privacy, not a site-wide block)")
        if working is None:
            print("\nEvery variant failed. If cookie was NOT set: put LUMINA_COOKIE=__Secure-next-auth.session-token=... "
                  "(just that one cookie) in .env and rerun. If it WAS set and still 403 with a Cloudflare 'server' header, "
                  "the site is challenging automated clients; that's a block I won't help get around, so ask the site owner for API access.")
            return

        c, pag = cards((await get(s, B.format(u=uid) + Q, working))[1])
        ts = [x.get("inventoryTs") for x in c or []]
        print(f"[2] cards={len(c or [])} pagination={pag}")
        print(f"    newest-first by inventoryTs? {ts == sorted(ts, reverse=True) if len(ts) > 1 else 'n/a (<2 cards)'}")
        cat = c[0]["cardCatalogId"] if c else None
        for size in (50, 100):
            st, body, _ = await get(s, B.format(u=uid) + Q.replace("pageSize=20", f"pageSize={size}"), working)
            print(f"[3] pageSize={size}: status={st} returned={len((cards(body)[0]) or [])}")
        base = len(c or [])
        for p in ("cardCatalogId", "catalogId", "cardId", "search", "character"):
            val = cat if "ard" in p or "atalog" in p else "a"
            st, body, _ = await get(s, B.format(u=uid) + Q + f"&{p}={val}", working)
            got = cards(body)[0] or []
            ids = {x.get("cardCatalogId") for x in got}
            real = bool(got) and len(got) < base and ids <= {cat}
            print(f"[4] {p}={val}: status={st} returned={len(got)} (unfiltered={base})" + ("  <-- REAL FILTER" if real else ""))

        print("[5] burst: 60 requests, concurrency 15")
        sem = asyncio.Semaphore(15); codes = {}; t = time.time()
        async def one():
            async with sem:
                st, _, h = await get(s, B.format(u=uid) + Q, working)
                codes[st] = codes.get(st, 0) + 1
                if st == 429: print("    429 headers:", {k: v for k, v in h.items() if k.lower().startswith(("retry", "x-rate"))})
        await asyncio.gather(*(one() for _ in range(60)))
        el = time.time() - t
        print(f"    {codes} in {el:.1f}s -> ~{60/el:.0f} req/s (only meaningful if mostly 200s)")

asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))