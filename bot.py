# /opt/crypto-bot/bot.py
# Crypto Team Trading View — fully commented bot

# ------------- Standard libs -------------
import os, io, math, random, hashlib, asyncio, time, datetime as dt
from collections import defaultdict

# ------------- Third-party libs -------------
import aiohttp           # async HTTP (CoinGecko, Coinbase)
import aiosqlite         # tiny SQLite DB
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import base64

# Matplotlib (headless) for sparklines
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Pandas + mplfinance for candlestick charts
import pandas as pd
import mplfinance as mpf

# Discord
import discord
from discord import app_commands, Embed, Colour
from discord.ext import tasks

# ============================================================
# ============= 1) ENV / CONFIG / CONSTANTS ==================
# ============================================================

load_dotenv(dotenv_path="/opt/crypto-bot/.env")

TOKEN = os.getenv("DISCORD_TOKEN")                  # Discord bot token
GUILD_ID = int(os.getenv("GUILD_ID", "0"))          # Server to register slash commands
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))  # Where to post
ANNOUNCE_THREAD_ID = os.getenv("ANNOUNCE_THREAD_ID") or ""        # Optional thread under the channel
TEST_ALERT_2PCT = (os.getenv("TEST_ALERT_2PCT", "true").lower() == "true")

DB_PATH = "/opt/crypto-bot/crypto_bot.sqlite"       # SQLite file

# CoinGecko API — free Demo key gets 100 calls/min vs 5-15 without
CG_API_KEY = os.getenv("CG_API_KEY", "")
CG_BASE = "https://api.coingecko.com"

# Universe / filtering for /pick
TOP_N = 1000
EXCLUDE_STABLE = {
    "USDT","USDC","DAI","FDUSD","PYUSD","TUSD","USDP","GUSD","EURS","EURT","XAUT",
    "PAXG","USDE","USDX","FRAX","USTC"
}
EXCLUDE_MAINSTREAM = {"BTC","ETH","DOGE"}
ATH_MIN_DISCOUNT = 30.0       # must be at least 30% below ATH
NO_REPEAT_WEEKS = 8           # don't repeat picks for this many weeks
REQUIRE_COINBASE = True       # only coins that are tradable on Coinbase Exchange

# Category-based scoring: boost AI/utility, penalize meme
AI_CATEGORIES = [
    "artificial-intelligence", "ai-agents", "ai-applications", "ai-framework",
    "bittensor-ecosystem", "bittensor-subnets",
]
MEME_CATEGORIES = [
    "meme-token", "ai-meme-coins", "solana-meme-coins", "base-meme-coins",
    "dog-themed-coins", "cat-themed-coins", "frog-themed-coins", "duck-themed-coins",
    "parody-meme-coins", "bitcoin-meme", "sui-meme", "ton-meme-coins", "tron-meme",
    "chinese-meme", "ip-meme", "desci-meme",
]

# Biweekly (odd week Friday 10:00 UTC)
BIWEEKLY_CRON = {"weekday": 4, "hour": 10, "minute": 0}

# ============================================================
# ============= 2) DISCORD CLIENT SETUP ======================
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None

# ============================================================
# ============= 3) COINGECKO HELPERS (prices, lists) =========
# ============================================================

_market_cache = None
_market_cache_time = 0
_symbol_image_map: dict[str, str] = {}

async def cg_get(session, url, retries=3, max_wait=15):
    headers = {"User-Agent": "CryptoTeamBot/1.0"}
    if CG_API_KEY:
        headers["x-cg-demo-api-key"] = CG_API_KEY
    wait = 5
    for _ in range(retries):
        async with session.get(url, headers=headers, timeout=30) as r:
            if r.status == 429:
                retry_after = r.headers.get("Retry-After")
                sleep_for = min(int(retry_after) if (retry_after and retry_after.isdigit()) else wait, max_wait)
                print(f"[cg] 429 rate limited. sleeping {sleep_for}s")
                await asyncio.sleep(sleep_for)
                wait = min(wait * 2, max_wait)
                continue
            r.raise_for_status()
            return await r.json()
    raise RuntimeError(f"CoinGecko GET failed after retries: {url}")

async def top_markets(session):
    """
    Top N by market cap, filtered:
      - exclude stables and mainstream (BTC/ETH/DOGE)
      - must be ≥ ATH_MIN_DISCOUNT below ATH
    Cached 5 minutes.
    """
    global _market_cache, _market_cache_time
    now = time.time()
    if _market_cache and now - _market_cache_time < 600:
        return _market_cache

    page_size = 250
    needed_pages = max(1, math.ceil(TOP_N / page_size))
    markets: list[dict] = []

    for page in range(1, needed_pages + 1):
        remaining = TOP_N - len(markets)
        if remaining <= 0:
            break
        per_page = page_size if remaining > page_size else remaining
        url = (
            f"{CG_BASE}/api/v3/coins/markets"
            f"?vs_currency=usd&order=market_cap_desc&per_page={per_page}&page={page}&sparkline=false"
        )
        data = await cg_get(session, url)
        markets.extend(data or [])
        if len(markets) >= TOP_N or not data:
            break
        await asyncio.sleep(0.4)

    data = markets[:TOP_N]
    out = []
    for c in data:
        sym = (c.get("symbol") or "").upper()
        if not sym or sym in EXCLUDE_STABLE or sym in EXCLUDE_MAINSTREAM:
            continue

        # 30%+ below ATH gate
        ok_ath = False
        ath_change_pct = c.get("ath_change_percentage")
        try:
            if isinstance(ath_change_pct, (int, float)) and not math.isnan(ath_change_pct):
                ok_ath = (ath_change_pct <= -ATH_MIN_DISCOUNT)
            else:
                ath, price = c.get("ath"), c.get("current_price")
                ok_ath = ath and price and (price <= (1 - ATH_MIN_DISCOUNT/100) * ath)
        except Exception:
            ok_ath = False
        if not ok_ath:
            continue

        ath_discount = abs(ath_change_pct) if isinstance(ath_change_pct, (int, float)) and not math.isnan(ath_change_pct) else 50.0
        coin_id = c["id"]
        is_ai = coin_id in _ai_coin_ids
        is_meme = coin_id in _meme_coin_ids
        out.append({
            "id": coin_id,
            "symbol": sym,
            "name": c.get("name"),
            "rank": c.get("market_cap_rank"),
            "price": c.get("current_price"),
            "ath": c.get("ath"),
            "ath_discount": ath_discount,
            "price_change_24h": c.get("price_change_percentage_24h") or 0.0,
            "volume": c.get("total_volume") or 0.0,
            "market_cap": c.get("market_cap") or 1.0,
            "is_ai": is_ai,
            "is_meme": is_meme,
        })

    out = [x for x in sorted(out, key=lambda d: d["rank"] or 9999) if x["rank"]]
    _market_cache, _market_cache_time = out, now
    return out

# ============================================================
# ============= 4) COINBASE PRODUCT FILTER ===================
# ============================================================

_coinbase_bases: set[str] = set()
_coinbase_bases_ts = 0.0

async def coinbase_symbols() -> set[str]:
    """
    Fetch base symbols tradable on Coinbase Exchange. Cached 24h.
    """
    global _coinbase_bases, _coinbase_bases_ts
    now = time.time()
    if _coinbase_bases and now - _coinbase_bases_ts < 24*3600:
        return _coinbase_bases

    url = "https://api.exchange.coinbase.com/products"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=30) as r:
            r.raise_for_status()
            data = await r.json()

    bases = {d.get("base_currency","").upper()
             for d in data if (d.get("status") or "").lower() == "online"}
    _coinbase_bases = {b for b in bases if b}
    _coinbase_bases_ts = now
    return _coinbase_bases

async def filter_availability(session, coins):
    """
    Filter candidates to symbols available on Coinbase Exchange.
    """
    if not REQUIRE_COINBASE:
        return coins
    cb = await coinbase_symbols()
    return [c for c in coins if c["symbol"] in cb]

# ============================================================
# ============= 4b) COINGECKO CATEGORY FILTER ================
# ============================================================

_ai_coin_ids: set[str] = set()
_meme_coin_ids: set[str] = set()
_category_cache_ts = 0.0

async def _fetch_category_ids(session, category: str) -> set[str]:
    url = (
        f"{CG_BASE}/api/v3/coins/markets"
        f"?vs_currency=usd&category={category}&order=market_cap_desc&per_page=250&page=1"
    )
    try:
        data = await cg_get(session, url)
        return {c["id"] for c in (data or []) if c.get("id")}
    except Exception:
        return set()

async def refresh_category_caches(session):
    global _ai_coin_ids, _meme_coin_ids, _category_cache_ts
    now = time.time()
    if _ai_coin_ids and now - _category_cache_ts < 24 * 3600:
        return
    ai_ids: set[str] = set()
    meme_ids: set[str] = set()
    for cat in AI_CATEGORIES[:2]:
        ai_ids |= await _fetch_category_ids(session, cat)
        await asyncio.sleep(8)
    meme_ids |= await _fetch_category_ids(session, "meme-token")
    await asyncio.sleep(8)
    if ai_ids:
        _ai_coin_ids = ai_ids
    if meme_ids:
        _meme_coin_ids = meme_ids
    _category_cache_ts = now
    print(f"[cat] refreshed: {len(_ai_coin_ids)} AI ids, {len(_meme_coin_ids)} meme ids")

# ============================================================
# ============= 5) DATABASE (SQLite) =========================
# ============================================================

async def ensure_db():
    """
    Create tables on first run; set default alert rule (5x).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS buys(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, symbol TEXT, coin_name TEXT,
                usd REAL, qty REAL, dt_utc TEXT
            );""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS picks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, coin_name TEXT, dt_utc TEXT
            );""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_rules(
                symbol TEXT PRIMARY KEY,   -- '' = global default
                multiple REAL NOT NULL
            );""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_state(
                symbol TEXT PRIMARY KEY,
                last_step INTEGER NOT NULL
            );""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coinbase_keys(
                user_id TEXT PRIMARY KEY,
                api_key_enc TEXT NOT NULL,
                api_secret_enc TEXT NOT NULL
            );""")
        await db.execute("INSERT OR IGNORE INTO alert_rules(symbol, multiple) VALUES('', 5.0)")
        await db.commit()

def _fernet() -> Fernet:
    key = hashlib.sha256(TOKEN.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))

async def store_coinbase_keys(user_id: str, api_key: str, api_secret: str):
    f = _fernet()
    enc_key = f.encrypt(api_key.encode()).decode()
    enc_secret = f.encrypt(api_secret.encode()).decode()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO coinbase_keys(user_id, api_key_enc, api_secret_enc) VALUES(?,?,?)",
            (user_id, enc_key, enc_secret))
        await db.commit()

async def load_coinbase_keys(user_id: str) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT api_key_enc, api_secret_enc FROM coinbase_keys WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    if not row:
        return None
    f = _fernet()
    return f.decrypt(row[0].encode()).decode(), f.decrypt(row[1].encode()).decode()

async def delete_coinbase_keys(user_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM coinbase_keys WHERE user_id=?", (user_id,))
        await db.commit()

def fetch_coinbase_balances_sync(api_key: str, api_secret: str) -> dict[str, float] | None:
    try:
        from coinbase.rest import RESTClient
        client = RESTClient(api_key=api_key, api_secret=api_secret)
        accounts = client.get_accounts()
        balances = {}
        for acct in accounts.accounts or []:
            bal = float(acct.available_balance.get("value", 0))
            currency = acct.available_balance.get("currency", "").upper()
            if bal > 0 and currency:
                balances[currency] = balances.get(currency, 0.0) + bal
        return balances
    except Exception as e:
        print(f"[coinbase] error fetching balances: {e}")
        return None

async def get_symbol_costs_and_qty():
    """
    Return {SYMBOL: (avg_cost, qty)} from aggregated buys.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT symbol, SUM(usd), SUM(qty) FROM buys GROUP BY symbol")
        out = {}
        for sym, usd_sum, qty_sum in await cur.fetchall():
            if qty_sum and qty_sum > 0:
                out[(sym or "").upper()] = (float(usd_sum)/float(qty_sum), float(qty_sum))
        return out

async def get_alert_multiple(db, symbol):
    """
    Read per-symbol alert multiple; fall back to default.
    """
    cur = await db.execute("SELECT multiple FROM alert_rules WHERE symbol=? COLLATE NOCASE", (symbol,))
    row = await cur.fetchone()
    if row: return float(row[0])
    cur = await db.execute("SELECT multiple FROM alert_rules WHERE symbol=''", ())
    row = await cur.fetchone()
    return float(row[0] if row else 5.0)

async def set_alert_multiple(symbol, multiple):
    """
    Upsert per-symbol or default (symbol = '' when None).
    """
    key = (symbol or "")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alert_rules(symbol, multiple) VALUES(?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET multiple=excluded.multiple",
            (key, multiple)
        )
        await db.commit()

async def remove_alert_rule(symbol):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alert_rules WHERE symbol=? COLLATE NOCASE", (symbol,))
        await db.commit()

# ============================================================
# ============= 6) GENERAL UTILS =============================
# ============================================================

def iso_week_seed(now=None):
    """
    Deterministic seed used for /pick to keep it consistent for a week.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.date()}|week{now.isocalendar().week}"

def _parse_iso_utc(s: str) -> dt.datetime:
    """
    Robust ISO parser that always returns timezone-aware UTC.
    """
    if not s:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    s = s.strip().replace("Z","+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except Exception:
        try:
            d = dt.datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    else:
        d = d.astimezone(dt.timezone.utc)
    return d

async def recent_symbols():
    """
    Return set of symbols picked in the last NO_REPEAT_WEEKS.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=NO_REPEAT_WEEKS)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT symbol, dt_utc FROM picks ORDER BY id DESC LIMIT 200")
        rows = await cur.fetchall()
    out = set()
    for s, when in rows:
        d = _parse_iso_utc((when or "").replace("Z","+00:00"))
        if d > cutoff:
            out.add((s or "").upper())
    return out

def _score_coin(c: dict) -> float:
    """
    Score a candidate coin for pick quality. Higher = better potential.
    Strongly favors AI/utility infrastructure tokens over meme coins.
    """
    score = 1.0

    # --- Category signal (strongest factor) ---
    if c.get("is_ai"):
        score *= 5.0
    if c.get("is_meme"):
        score *= 0.05

    # Rank tier: top coins are more established / liquid
    rank = c.get("rank") or 9999
    if rank <= 100:   score *= 3.0
    elif rank <= 300: score *= 2.0
    elif rank <= 500: score *= 1.5

    # ATH discount sweet spot: 35–80% below ATH = beaten down but not dead
    discount = c.get("ath_discount", 50.0)
    if 35 <= discount <= 80:  score *= 2.0
    elif discount > 95:       score *= 0.4

    # 24h momentum: gentle uptrend preferred; avoid free-falling or already-parabolic
    change_24h = c.get("price_change_24h", 0.0)
    if 0 <= change_24h <= 10:   score *= 1.5
    elif change_24h > 20:       score *= 0.7
    elif change_24h < -10:      score *= 0.6

    # Volume / market cap ratio: prefer actively traded coins
    volume = c.get("volume", 0.0)
    market_cap = c.get("market_cap", 1.0) or 1.0
    vol_ratio = volume / market_cap
    if vol_ratio >= 0.05:   score *= 1.5
    elif vol_ratio < 0.01:  score *= 0.4

    return max(score, 0.01)


async def pick_coin():
    """
    Build candidate pool (filters + no-repeat), then choose 1 using a seeded
    weighted-random draw that strongly favours AI/utility tokens.
    """
    async with aiohttp.ClientSession() as session:
        try:
            await refresh_category_caches(session)
        except Exception as e:
            print(f"[pick] category refresh failed (non-fatal): {e}")
        cands = await top_markets(session)
        cands = await filter_availability(session, cands)
        if not cands:
            return None, "no-candidates", 0, 0

        recent = await recent_symbols()
        pool = [c for c in cands if c["symbol"] not in recent] or cands

        seed = iso_week_seed()
        seed_int = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % (2**32)
        rnd = random.Random(seed_int)
        weights = [_score_coin(c) for c in pool]
        choice = rnd.choices(pool, weights=weights, k=1)[0]
        return choice, seed, len(cands), len(pool)

async def post_in_announce_channel(content: str | None = None, *, embed: discord.Embed | None = None, file: discord.File | None = None):
    """
    Helper to post to your announce channel (or thread if provided).
    """
    if not ANNOUNCE_CHANNEL_ID:
        return None
    ch = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
    if ANNOUNCE_THREAD_ID:
        try:
            thread = bot.get_channel(int(ANNOUNCE_THREAD_ID)) or await bot.fetch_channel(int(ANNOUNCE_THREAD_ID))
            return await thread.send(content, embed=embed, file=file)
        except Exception as e:
            warn = f"(thread post failed: {e})\n"
            return await ch.send((warn + (content or "")) or None, embed=embed, file=file)
    return await ch.send(content, embed=embed, file=file)

# ============================================================
# ============= 7) PRICE MAP + ID MAP + SPARKLINES ===========
# ============================================================

_prices_cache: dict[str, float] = {}
_changes_cache: dict[str, float] = {}
_prices_cache_ts = 0.0
PRICES_CACHE_TTL = 300

async def _refresh_prices_cache():
    global _prices_cache, _changes_cache, _prices_cache_ts, _symbol_image_map
    now = time.time()
    if _prices_cache and now - _prices_cache_ts < PRICES_CACHE_TTL:
        return
    prices: dict[str, float] = {}
    changes: dict[str, float] = {}
    async with aiohttp.ClientSession() as session:
        for page in range(1, 5):
            data = await cg_get(
                session,
                f"{CG_BASE}/api/v3/coins/markets"
                f"?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
            )
            for m in data:
                sym = (m.get("symbol") or "").upper()
                if sym and sym not in prices:
                    prices[sym] = float(m["current_price"])
                    changes[sym] = float(m.get("price_change_percentage_24h") or 0.0)
                img = m.get("image")
                if sym and img and sym not in _symbol_image_map:
                    _symbol_image_map[sym] = img
            await asyncio.sleep(1.5)
    if prices:
        _prices_cache = prices
        _changes_cache = changes
        _prices_cache_ts = now

async def fetch_prices_map(target_syms: set[str]) -> dict[str, float]:
    await _refresh_prices_cache()
    target_syms = {s.upper() for s in target_syms}
    return {s: _prices_cache[s] for s in target_syms if s in _prices_cache}

async def fetch_prices_and_changes(target_syms: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    await _refresh_prices_cache()
    target_syms = {s.upper() for s in target_syms}
    prices = {s: _prices_cache[s] for s in target_syms if s in _prices_cache}
    changes = {s: _changes_cache.get(s, 0.0) for s in target_syms if s in _prices_cache}
    return prices, changes

# --- Symbol -> CoinGecko ID map (for fetching history/ohlc) ---
_symbol_id_map: dict[str, str] = {}
_symbol_id_map_ts = 0.0

async def get_symbol_id_map() -> dict[str, str]:
    """
    Cache CoinGecko IDs for symbols (top 750). Refreshed every 6h.
    """
    global _symbol_id_map, _symbol_id_map_ts
    now = time.time()
    if _symbol_id_map and now - _symbol_id_map_ts < 6*3600:
        return _symbol_id_map

    out: dict[str,str] = {}
    async with aiohttp.ClientSession() as session:
        page_count = max(1, math.ceil(TOP_N / 250))
        for page in range(1, page_count + 1):
            data = await cg_get(session,
                f"{CG_BASE}/api/v3/coins/markets"
                f"?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false")
            for m in data:
                sym = (m.get("symbol") or "").upper()
                cid = m.get("id")
                if sym and cid and sym not in out:
                    out[sym] = cid
            await asyncio.sleep(0.4)
    _symbol_id_map, _symbol_id_map_ts = out, now
    return out

async def fetch_7d_prices(symbol: str) -> list[float] | None:
    """
    Return last-7d hourly-ish USD prices for sparkline.
    """
    sym = symbol.upper()
    idmap = await get_symbol_id_map()
    cid = idmap.get(sym)
    if not cid:
        return None
    url = f"{CG_BASE}/api/v3/coins/{cid}/market_chart?vs_currency=usd&days=7&interval=hourly"
    async with aiohttp.ClientSession() as s:
        data = await cg_get(s, url)
    prices = [p[1] for p in data.get("prices", []) if isinstance(p, (list, tuple)) and len(p) == 2]
    return prices or None

def make_sparkline_png(prices: list[float], sym: str) -> io.BytesIO:
    """
    Tiny line chart for alerts (green up / red down).
    """
    color = "green" if prices and prices[-1] >= prices[0] else "red"
    plt.figure(figsize=(5.2, 2.0), dpi=160)
    plt.plot(prices, linewidth=2, color=color)
    if prices:
        plt.axhline(prices[0], linewidth=1, color="#777777", alpha=0.3)
    plt.title(f"{sym} · last 7 days", fontsize=10)
    plt.xticks([]); plt.yticks([])
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close()
    buf.seek(0)
    return buf

def build_alert_embed(sym: str, price: float, cost: float, qty: float, multiple: float, step_now: int) -> Embed:
    """
    Green/red embed describing the alert.
    """
    pct = ((price / cost) - 1) * 100 if cost > 0 else 0.0
    colour = Colour.green() if price >= cost else Colour.red()
    e = Embed(
        title=f"🚨 {sym} alert",
        description=f"Crossed **{step_now*multiple:.2f}×** your avg cost",
        colour=colour,
    )
    e.add_field(name="Price", value=f"${price:,.6f}", inline=True)
    e.add_field(name="Avg Cost", value=f"${cost:,.6f}", inline=True)
    e.add_field(name="P/L %", value=f"{pct:+.2f}%", inline=True)
    e.set_footer(text="Next alert when it crosses the next multiple")
    return e

# ---- OHLC (candlesticks) for /chart ----
async def fetch_ohlc(symbol: str, days: str = "30") -> list[list[float]] | None:
    """
    Return rows of [ms, open, high, low, close] for last `days` (1,7,14,30,90,180,365,max).
    """
    sym = symbol.upper()
    idmap = await get_symbol_id_map()
    cid = idmap.get(sym)
    if not cid:
        return None
    url = f"{CG_BASE}/api/v3/coins/{cid}/ohlc?vs_currency=usd&days={days}"
    async with aiohttp.ClientSession() as s:
        data = await cg_get(s, url)
    return data or None

def make_candle_png(ohlc_rows: list[list[float]], sym: str, ma: tuple[int, ...] = ()):
    """
    Render candlestick PNG with optional moving averages.
    """
    df = pd.DataFrame(ohlc_rows, columns=["Date", "Open", "High", "Low", "Close"])
    df["Date"] = pd.to_datetime(df["Date"], unit="ms", utc=True).dt.tz_convert("UTC")
    df.set_index("Date", inplace=True)

    mc = mpf.make_marketcolors(up="g", down="r", wick="inherit", edge="inherit", volume="in")
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", facecolor="#0e0f14")

    buf = io.BytesIO()

    # Build kwargs so we only pass `mav` when present
    plot_kwargs = dict(
        type="candle",
        volume=False,
        style=style,
        figscale=1.1,
        figratio=(12, 6),
        title=f"{sym} · USD",
        savefig=dict(fname=buf, format="png", bbox_inches="tight"),
    )
    if ma:  # <-- only add mav if provided
        plot_kwargs["mav"] = ma

    mpf.plot(df, **plot_kwargs)

    buf.seek(0)
    return buf

# ============================================================
# ============= 8) DISCORD EVENTS ============================
# ============================================================

@bot.event
async def on_ready():
    """
    Start loops + register slash commands when the bot connects.
    """
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await ensure_db()
    try:
        if GUILD_OBJ:
            await tree.sync(guild=GUILD_OBJ)
            print(f"Commands synced to guild {GUILD_ID}")
        else:
            await tree.sync()
            print("Commands synced globally")
    except Exception as e:
        print("Failed to sync commands:", e)
    scheduler.start()

# ============================================================
# ============= 9) SLASH COMMANDS ============================
# ============================================================

# --- /ping : quick health check ---------------------------------------------
# --- /buy : log a purchase ---------------------------------------------------
@tree.command(guild=GUILD_OBJ, name="buy", description="Log a buy the group made")
@app_commands.describe(symbol="e.g. AVAX", usd="Dollars spent", qty="Quantity purchased", when="YYYY-MM-DD (optional)")
async def buy_cmd(interaction: discord.Interaction, symbol: str, usd: float, qty: float, when: str = None):
    await interaction.response.defer(ephemeral=True)
    symbol = symbol.upper()
    when_dt = (dt.datetime.strptime(when,"%Y-%m-%d") if when else dt.datetime.utcnow()).replace(tzinfo=dt.timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO buys(user_id,symbol,coin_name,usd,qty,dt_utc) VALUES(?,?,?,?,?,?)",
            (str(interaction.user.id), symbol, "", usd, qty, when_dt.isoformat().replace("+00:00","Z"))
        )
        await db.commit()
    await interaction.followup.send(f"Logged: **{symbol}** — ${usd:.2f} for {qty:g} on {when_dt.date()} ✅", ephemeral=True)

# --- /connect-coinbase : modal for read-only API keys ------------------------

class CoinbaseKeyModal(discord.ui.Modal, title="Link Coinbase (Read-Only)"):
    api_key = discord.ui.TextInput(
        label="API Key",
        placeholder="organizations/{org_id}/apiKeys/{key_id}",
        style=discord.TextStyle.short,
    )
    api_secret = discord.ui.TextInput(
        label="API Secret (Private Key)",
        placeholder="-----BEGIN EC PRIVATE KEY-----\n...",
        style=discord.TextStyle.long,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.api_key.value.strip()
        secret = self.api_secret.value.strip()
        balances = await asyncio.to_thread(fetch_coinbase_balances_sync, key, secret)
        if balances is None:
            await interaction.followup.send(
                "❌ Could not connect — check your key and secret.\nMake sure the key has **View** permission only.",
                ephemeral=True)
            return
        count = len([v for v in balances.values() if v > 0])
        await store_coinbase_keys(str(interaction.user.id), key, secret)
        await interaction.followup.send(
            f"✅ Coinbase linked! Found **{count}** assets.\n"
            "`/history` will now show live balances. Use `/disconnect-coinbase` to unlink.",
            ephemeral=True)

@tree.command(guild=GUILD_OBJ, name="connect-coinbase",
              description="Link your Coinbase read-only API key (opens a secure form)")
async def connect_coinbase_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(CoinbaseKeyModal())

@tree.command(guild=GUILD_OBJ, name="disconnect-coinbase",
              description="Remove your stored Coinbase API keys")
async def disconnect_coinbase_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await delete_coinbase_keys(str(interaction.user.id))
    await interaction.followup.send("✅ Coinbase keys removed. `/history` will use your local buy log.", ephemeral=True)

# --- /history : image-rendered portfolio dashboard ---------------------------

async def _build_portfolio(user_id: str) -> tuple[dict, set, list, str]:
    # Always load local buys for cost basis
    sql = "SELECT rowid, user_id, symbol, usd, qty, dt_utc FROM buys WHERE user_id=? ORDER BY dt_utc ASC"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, (user_id,))
        rows = await cur.fetchall()
    local_agg = defaultdict(lambda: {"usd": 0.0, "qty": 0.0})
    for rid, uid, sym, usd, qty, when in rows:
        s = (sym or "").upper()
        local_agg[s]["usd"] += float(usd)
        local_agg[s]["qty"] += float(qty)

    cb_keys = await load_coinbase_keys(user_id)
    if cb_keys:
        cb_balances = await asyncio.to_thread(fetch_coinbase_balances_sync, cb_keys[0], cb_keys[1])
        if cb_balances:
            symbols = set(cb_balances.keys()) - EXCLUDE_STABLE - {"USD"}
            agg = {}
            for s in symbols:
                if cb_balances[s] <= 0:
                    continue
                cost = local_agg[s]["usd"] if s in local_agg else 0.0
                agg[s] = {"qty": cb_balances[s], "usd": cost}
            symbols = set(agg.keys())
            return agg, symbols, rows, "Live from Coinbase"

    symbols = set(local_agg.keys())
    return dict(local_agg), symbols, rows, "Local buy log"

def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    if p >= 0.01:
        return f"${p:.4f}"
    return f"${p:.6f}"

def _fmt_value(v: float) -> str:
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"

def _render_portfolio_image(display_name, agg, symbols, prices, changes, source_label):
    BG = "#0d0e12"
    CARD = "#16171e"
    HEADER_BG = "#1e1f2a"
    WHITE = "#eaeaea"
    DIM = "#666"
    GREEN = "#34d399"
    RED = "#fb7185"
    ACCENT = "#7c3aed"

    from matplotlib.patches import FancyBboxPatch

    coin_rows = []
    total_cost, total_value = 0.0, 0.0
    for s in sorted(symbols):
        usd = agg[s]["usd"]; qty = agg[s]["qty"]
        avg = (usd / qty) if qty else 0.0
        price = prices.get(s)
        if price is None or qty <= 0:
            continue
        value = price * qty
        total_value += value
        total_cost += usd
        pnl_pct = ((price / avg) - 1) * 100 if usd > 0 and avg > 0 else None
        chg_24h = changes.get(s, 0.0)
        coin_rows.append((s, price, value, pnl_pct, chg_24h))

    coin_rows.sort(key=lambda r: r[2], reverse=True)

    overall_pct = ((total_value / total_cost) - 1) * 100 if total_cost > 0 and total_value > 0 else None

    n = len(coin_rows)
    row_h = 0.42
    header_h = 1.8
    table_header_h = 0.5
    fig_h = header_h + table_header_h + n * row_h + 0.5
    fig_w = 8.2

    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()

    # Card background
    card = FancyBboxPatch((0.12, 0.12), fig_w - 0.24, fig_h - 0.24,
                          boxstyle="round,pad=0.18", facecolor=CARD,
                          edgecolor="#2a2b35", linewidth=1.0)
    ax.add_patch(card)

    # Header area
    hdr_y = fig_h - header_h
    hdr = FancyBboxPatch((0.12, hdr_y), fig_w - 0.24, header_h - 0.12,
                         boxstyle="round,pad=0.18", facecolor=HEADER_BG, edgecolor="none")
    ax.add_patch(hdr)

    # Accent bar
    ax.axhline(y=hdr_y, xmin=0.02, xmax=0.98, color=ACCENT, linewidth=2.5)

    # Title row
    ax.text(0.5, fig_h - 0.5, display_name, fontsize=15, fontweight="bold",
            color=WHITE, va="center", fontfamily="monospace")
    ax.text(fig_w - 0.45, fig_h - 0.5, source_label, fontsize=8,
            color=DIM, va="center", ha="right", fontfamily="monospace",
            style="italic")

    # Total value
    val_str = _fmt_value(total_value)
    ax.text(0.5, fig_h - 1.05, val_str, fontsize=24, fontweight="bold",
            color=WHITE, va="center", fontfamily="monospace")

    if overall_pct is not None:
        pnl_color = GREEN if overall_pct >= 0 else RED
        arrow = "▲" if overall_pct >= 0 else "▼"
        ax.text(0.5 + len(val_str) * 0.24, fig_h - 1.05, f" {arrow} {abs(overall_pct):.1f}%",
                fontsize=13, color=pnl_color, va="center", fontfamily="monospace",
                fontweight="bold")
        ax.text(0.5, fig_h - 1.5, f"Cost ${total_cost:,.2f}",
                fontsize=10, color=DIM, va="center", fontfamily="monospace")

    # Column headers
    cols = [0.45, 2.2, 3.6, 5.2, 6.7, 7.8]
    col_labels = ["COIN", "PRICE", "VALUE", "24H", "P/L"]
    th_y = hdr_y - table_header_h + 0.1
    ax.axhline(y=th_y - 0.08, xmin=0.03, xmax=0.97, color="#2a2b35", linewidth=0.8)
    aligns = ["left", "right", "right", "right", "right"]
    for ci, (cx, label) in enumerate(zip([cols[0], cols[1], cols[2], cols[3], cols[4]], col_labels)):
        ax.text(cx, th_y + 0.18, label, fontsize=8, fontweight="bold",
                color=DIM, va="center", ha=aligns[ci], fontfamily="monospace")

    # Coin rows
    for i, (sym, price, value, pnl_pct, chg_24h) in enumerate(coin_rows):
        y = th_y - 0.12 - (i + 0.5) * row_h

        if i % 2 == 0:
            stripe = FancyBboxPatch((0.22, y - row_h * 0.42), fig_w - 0.44, row_h * 0.88,
                                   boxstyle="round,pad=0.03", facecolor="#1a1b24", edgecolor="none")
            ax.add_patch(stripe)

        # Rank dot
        dot_color = GREEN if (pnl_pct is not None and pnl_pct >= 0) else RED if (pnl_pct is not None) else DIM
        ax.plot(cols[0] - 0.12, y, "o", color=dot_color, markersize=4)

        ax.text(cols[0], y, sym, fontsize=11, fontweight="bold",
                color=WHITE, va="center", ha="left", fontfamily="monospace")
        ax.text(cols[1], y, _fmt_price(price), fontsize=10,
                color="#bbb", va="center", ha="right", fontfamily="monospace")
        ax.text(cols[2], y, _fmt_value(value), fontsize=11,
                color=WHITE, va="center", ha="right", fontfamily="monospace")

        # 24h change
        chg_color = GREEN if chg_24h >= 0 else RED
        chg_arrow = "▲" if chg_24h >= 0 else "▼"
        ax.text(cols[3], y, f"{chg_arrow}{abs(chg_24h):.1f}%", fontsize=10,
                color=chg_color, va="center", ha="right", fontfamily="monospace")

        # P/L
        if pnl_pct is not None:
            pcolor = GREEN if pnl_pct >= 0 else RED
            ax.text(cols[4], y, f"{pnl_pct:+.1f}%", fontsize=10, fontweight="bold",
                    color=pcolor, va="center", ha="right", fontfamily="monospace")
        else:
            ax.text(cols[4], y, "--", fontsize=10, color=DIM,
                    va="center", ha="right", fontfamily="monospace")

    # Footer
    now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ax.text(fig_w / 2, 0.3, now_str, fontsize=7, color="#444",
            va="center", ha="center", fontfamily="monospace")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    buf.seek(0)
    return buf

class PortfolioView(discord.ui.View):
    def __init__(self, user_id: str, display_name: str, rows: list):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.display_name = display_name
        self.rows = rows

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This isn't your portfolio.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="\U0001f504")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        agg, symbols, rows, source_label = await _build_portfolio(self.user_id)
        if not symbols:
            await interaction.followup.send("No data found.", ephemeral=True)
            return
        prices, changes = await fetch_prices_and_changes(symbols)
        buf = await asyncio.to_thread(_render_portfolio_image,
                                      self.display_name, agg, symbols, prices, changes, source_label)
        self.rows = rows
        file = discord.File(buf, filename="portfolio.png")
        await interaction.edit_original_response(attachments=[file], view=self)

    @discord.ui.button(label="Show Buys", style=discord.ButtonStyle.secondary, emoji="\U0001f4dd")
    async def buys_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.rows:
            await interaction.response.send_message("No local buy log (using Coinbase live data).", ephemeral=True)
            return
        lines = []
        for rid, uid, sym, usd, qty, when in self.rows:
            dt_str = (when or "")[:10]
            lines.append(f"`{rid}` **{(sym or '').upper()}** {qty:g} @ ${usd:.2f} - {dt_str}")
        text = "\n".join(lines)[:1900]
        await interaction.response.send_message(f"**Buy Log:**\n{text}", ephemeral=True)

@tree.command(guild=GUILD_OBJ, name="wallet", description="Your portfolio dashboard")
async def history_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    target_id = str(interaction.user.id)
    agg, symbols, rows, source_label = await _build_portfolio(target_id)
    if not symbols:
        await interaction.followup.send(
            "No portfolio data found.\nUse `/buy` to log trades or `/connect-coinbase` to link your account.")
        return
    try:
        prices, changes = await asyncio.wait_for(fetch_prices_and_changes(symbols), timeout=45)
    except (asyncio.TimeoutError, Exception):
        await interaction.followup.send("CoinGecko is busy right now. Try again in a minute.")
        return
    buf = await asyncio.to_thread(_render_portfolio_image,
                                  interaction.user.display_name, agg, symbols, prices, changes, source_label)
    view = PortfolioView(target_id, interaction.user.display_name, rows)
    file = discord.File(buf, filename="portfolio.png")
    await interaction.followup.send(file=file, view=view)

# --- /removebuy : delete a row by ID ----------------------------------------
@tree.command(guild=GUILD_OBJ, name="removebuy", description="Remove a logged buy by row ID")
@app_commands.describe(id="Row ID (see /history)")
async def removebuy_cmd(interaction: discord.Interaction, id: int):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM buys WHERE rowid = ?", (id,))
        await db.commit()
    if cur.rowcount > 0:
        await interaction.followup.send(f"✅ Removed buy entry with ID `{id}`", ephemeral=True)
    else:
        await interaction.followup.send(f"⚠️ No entry found with ID `{id}`", ephemeral=True)

# --- /pick : random biweekly-style pick with filters -------------------------
_pick_lock = asyncio.Lock()
_last_pick = None  # (msg, ts)

@tree.command(guild=GUILD_OBJ, name="pick", description="Random pick (filters + no-repeat 8w)")
async def pick_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    global _last_pick
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    # Basic debounce so multiple taps in a minute reuse the same result
    if _last_pick and (now - _last_pick[1] < 60):
        await interaction.followup.send(_last_pick[0]); return
    async with _pick_lock:
        if _last_pick and (dt.datetime.now(dt.timezone.utc).timestamp() - _last_pick[1] < 60):
            await interaction.followup.send(_last_pick[0]); return
        try:
            choice, seed, cand_count, pool_size = await asyncio.wait_for(pick_coin(), timeout=45)
        except (asyncio.TimeoutError, Exception):
            await interaction.followup.send("CoinGecko is busy right now. Try again in a minute.")
            return
        if not choice:
            msg = "No candidates passed the filters today."
            await interaction.followup.send(msg)
            _last_pick = (msg, dt.datetime.now(dt.timezone.utc).timestamp())
            return
        tag = " 🤖 AI" if choice.get("is_ai") else ""
        msg = (f"🎲 **Random Pick:** {choice['name']} ({choice['symbol']}){tag}  [rank #{choice['rank']}]\n"
               f"Seed: `{seed}` | Universe after filters: {cand_count} | Pool size (no-repeat {NO_REPEAT_WEEKS}w): {pool_size}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO picks(symbol, coin_name, dt_utc) VALUES(?,?,?)",
                             (choice['symbol'], choice['name'], dt.datetime.utcnow().isoformat()+'Z'))
            await db.commit()
        await interaction.followup.send(msg)
        _last_pick = (msg, dt.datetime.now(dt.timezone.utc).timestamp())

# --- ALERT LOOP + COMMANDS ---------------------------------------------------
async def run_alerts_once():
    """
    Check each tracked symbol vs its multiple target.
    Post alert embed (+ sparkline) when we cross a new step threshold.
    """
    try:
        costs = await get_symbol_costs_and_qty()
        if not costs:
            return
        symbols = set(costs.keys())
        prices = await fetch_prices_map(symbols)

        async with aiosqlite.connect(DB_PATH) as db:
            for sym, (cost, qty) in costs.items():
                price = prices.get(sym)
                if price is None or cost <= 0:
                    continue

                multiple = await get_alert_multiple(db, sym)
                if multiple <= 0: multiple = 5.0

                step_now = int(price / (cost * multiple))
                cur = await db.execute("SELECT last_step FROM alert_state WHERE symbol=?", (sym,))
                row = await cur.fetchone()
                last_step = int(row[0]) if row else 0

                # crossed into a new step -> fire
                if step_now > last_step:
                    embed = build_alert_embed(sym, price, cost, qty, multiple, step_now)
                    await post_in_announce_channel(embed=embed)
                    await db.execute(
                        "INSERT INTO alert_state(symbol,last_step) VALUES(?,?) "
                        "ON CONFLICT(symbol) DO UPDATE SET last_step=excluded.last_step",
                        (sym, step_now),
                    )

                # optional +2% test nudge
                if TEST_ALERT_2PCT and price >= cost * 1.02:
                    pct_embed = Embed(
                        title=f"🧪 TEST: {sym} +2% above avg cost",
                        colour=Colour.green())
                    pct_embed.add_field(name="Price", value=f"${price:,.6f}", inline=True)
                    pct_embed.add_field(name="Avg Cost", value=f"${cost:,.6f}", inline=True)
                    await post_in_announce_channel(embed=pct_embed)

            await db.commit()
    except Exception as e:
        print("run_alerts_once error:", e)

@tasks.loop(minutes=30)
async def alert_watcher():
    try:
        await run_alerts_once()
    except Exception as e:
        print(f"[alerts] skipped this cycle: {e}")

@alert_watcher.before_loop
async def _alert_wait():
    await asyncio.sleep(180)

# /alertx — set default or per-symbol multiple (e.g., 5x)
@tree.command(guild=GUILD_OBJ, name="alertx", description="Set X-multiple alerts (global or per-symbol)")
@app_commands.describe(multiple="e.g., 5 for 5x", symbol="Optional symbol (e.g., RSR). Omit to set default.")
async def alertx_cmd(interaction: discord.Interaction, multiple: float, symbol: str = None):
    if multiple <= 0:
        await interaction.response.send_message("Multiple must be > 0.", ephemeral=True); return
    await set_alert_multiple(symbol.upper() if symbol else None, multiple)
    target = (symbol.upper() if symbol else "ALL COINS (default)")
    await interaction.response.send_message(f"✅ Alerts set to **{multiple}×** for **{target}**.", ephemeral=True)

# /alerts — list rules + last fired step
@tree.command(guild=GUILD_OBJ, name="alerts", description="Show alert settings and last fired steps")
async def alerts_cmd(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT symbol, multiple FROM alert_rules ORDER BY symbol")
        rules = await cur.fetchall()
        cur = await db.execute("SELECT symbol, last_step FROM alert_state ORDER BY symbol")
        states = {s:int(st) for s,st in await cur.fetchall()}
    lines = []
    for sym, mult in rules:
        label = "(default)" if sym=="" else sym
        step = states.get(sym if sym else "", 0)
        lines.append(f"• **{label}** → {mult:g}× | last step fired: {step}")
    await interaction.response.send_message("\n".join(lines) if lines else "No alert rules yet. Use `/alertx multiple:5`.", ephemeral=True)

# /chart — candlestick (CoinGecko OHLC + mplfinance)
@tree.command(guild=GUILD_OBJ, name="chart", description="Candlestick chart (CoinGecko OHLC)")
@app_commands.describe(symbol="e.g. RSR", days="1, 7, 14, 30, 90, 180, 365, or max", ma="Moving averages, e.g. 7,20")
async def chart_cmd(interaction: discord.Interaction, symbol: str, days: str = "30", ma: str = ""):
    await interaction.response.defer()
    sym = symbol.upper()
    if days not in {"1","7","14","30","90","180","365","max"}:
        days = "30"

    ma_tuple: tuple[int, ...] = tuple(int(x.strip()) for x in ma.split(",") if x.strip().isdigit()) if ma else ()

    rows = await fetch_ohlc(sym, days=days)
    if not rows:
        try:
            await interaction.followup.send(f"Couldn't fetch OHLC for **{sym}**.", ephemeral=True)
        except Exception:
            pass
        return

    try:
        loop = asyncio.get_event_loop()
        png = await loop.run_in_executor(None, lambda: make_candle_png(rows, sym, ma=ma_tuple))
        file = discord.File(png, filename=f"{sym}_{days}d.png")
        embed = Embed(
            title=f"{sym} chart",
            description=f"Candles: **{days}**; MA: {', '.join(map(str,ma_tuple)) if ma_tuple else 'none'}",
            colour=Colour.blurple()
        )
        embed.set_image(url=f"attachment://{sym}_{days}d.png")
        await interaction.followup.send(embed=embed, file=file)
    except discord.NotFound:
        print(f"[chart] interaction expired before reply could be sent ({sym})")
    except Exception as e:
        try:
            await interaction.followup.send(f"Chart render failed: `{e}`", ephemeral=True)
        except Exception:
            pass

# ============================================================
# ============= 10) SCHEDULER (biweekly pick) =================
# ============================================================

@tasks.loop(minutes=1)
async def scheduler():
    """
    Every minute, if it's Fri 10:00 UTC in an odd ISO week, announce a pick.
    """
    if not ANNOUNCE_CHANNEL_ID:
        return
    now = dt.datetime.now(dt.timezone.utc)
    if now.weekday()==BIWEEKLY_CRON["weekday"] and now.hour==BIWEEKLY_CRON["hour"] and now.minute==BIWEEKLY_CRON["minute"]:
        if now.isocalendar().week % 2 == 1:
            choice, seed, cand_count, pool_size = await pick_coin()
            if choice:
                tag = " 🤖 AI" if choice.get("is_ai") else ""
                content = (f"📣 **Biweekly Pick** → {choice['name']} ({choice['symbol']}){tag} — seed `{seed}`\n"
                           f"Universe:{cand_count}  Pool:{pool_size}  (≥{ATH_MIN_DISCOUNT:.0f}% below ATH, Coinbase-listed)")
                await post_in_announce_channel(content)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT INTO picks(symbol, coin_name, dt_utc) VALUES(?,?,?)",
                                     (choice['symbol'], choice['name'], dt.datetime.utcnow().isoformat()+"Z"))
                    await db.commit()
            # simple guard so we don't announce twice in the same minute
            await asyncio.sleep(70)

# ============================================================
# ============= 11) RUN ======================================
# ============================================================

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing. Check /opt/crypto-bot/.env")

bot.run(TOKEN)

