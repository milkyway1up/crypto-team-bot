# /opt/crypto-bot/bot.py
# Crypto Team Trading View — fully commented bot

# ------------- Standard libs -------------
import os, io, math, random, hashlib, asyncio, time, datetime as dt
from collections import defaultdict

# ------------- Third-party libs -------------
import aiohttp           # async HTTP (CoinGecko, Coinbase)
import aiosqlite         # tiny SQLite DB
from dotenv import load_dotenv

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

# Universe / filtering for /pick
TOP_N = 100
EXCLUDE_STABLE = {
    "USDT","USDC","DAI","FDUSD","PYUSD","TUSD","USDP","GUSD","EURS","EURT","XAUT",
    "PAXG","USDE","USDX","FRAX","USTC"
}
EXCLUDE_MAINSTREAM = {"BTC","ETH","DOGE"}
ATH_MIN_DISCOUNT = 30.0       # must be at least 30% below ATH
NO_REPEAT_WEEKS = 8           # don’t repeat picks for this many weeks
REQUIRE_COINBASE = True       # only coins that are tradable on Coinbase Exchange

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

async def cg_get(session, url, retries=3):
    """
    Basic GET with backoff for 429s (CoinGecko).
    """
    headers = {"User-Agent": "CryptoTeamBot/1.0"}
    wait = 3
    for _ in range(retries):
        async with session.get(url, headers=headers, timeout=30) as r:
            if r.status == 429:
                retry_after = r.headers.get("Retry-After")
                sleep_for = int(retry_after) if (retry_after and retry_after.isdigit()) else wait
                print(f"[cg] 429 rate limited. sleeping {sleep_for}s")
                await asyncio.sleep(sleep_for)
                wait = min(wait * 2, 30)
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
    if _market_cache and now - _market_cache_time < 300:
        return _market_cache

    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={TOP_N}&page=1&sparkline=false"
    )
    data = await cg_get(session, url)
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

        out.append({
            "id": c["id"],
            "symbol": sym,
            "name": c.get("name"),
            "rank": c.get("market_cap_rank"),
            "price": c.get("current_price"),
            "ath": c.get("ath"),
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
        await db.execute("INSERT OR IGNORE INTO alert_rules(symbol, multiple) VALUES('', 5.0)")
        await db.commit()

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

async def pick_coin():
    """
    Build candidate pool (filters + no-repeat), then choose 1 using a weekly seed.
    """
    async with aiohttp.ClientSession() as session:
        cands = await top_markets(session)
        cands = await filter_availability(session, cands)
        if not cands:
            return None, "no-candidates", 0, 0

        recent = await recent_symbols()
        pool = [c for c in cands if c["symbol"] not in recent] or cands

        seed = iso_week_seed()
        seed_int = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % (2**32)
        rnd = random.Random(seed_int)
        choice = rnd.choice(pool)
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

async def fetch_prices_map(target_syms: set[str]) -> dict[str, float]:
    """
    Return {SYMBOL: latest_price} for up to top 750 by mktcap.
    """
    target_syms = {s.upper() for s in target_syms}
    out: dict[str, float] = {}
    async with aiohttp.ClientSession() as session:
        for page in (1,2,3):
            data = await cg_get(
                session,
                f"https://api.coingecko.com/api/v3/coins/markets"
                f"?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}&sparkline=false"
            )
            for m in data:
                sym = (m.get("symbol") or "").upper()
                if sym in target_syms and sym not in out:
                    out[sym] = float(m["current_price"])
            if len(out) == len(target_syms):
                break
            await asyncio.sleep(0.6)
    return out

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
        for page in (1,2,3):
            data = await cg_get(session,
                f"https://api.coingecko.com/api/v3/coins/markets"
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
    url = f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days=7&interval=hourly"
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
    e.add_field(name="Qty Tracked", value=f"{qty:g}", inline=True)
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
    url = f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc?vs_currency=usd&days={days}"
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
    alert_watcher.start()

# ============================================================
# ============= 9) SLASH COMMANDS ============================
# ============================================================

# --- /ping : quick health check ---------------------------------------------
@tree.command(guild=GUILD_OBJ, name="ping", description="Health check")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅")

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

# --- /history : portfolio summary as embeds (mobile-friendly) ----------------
@tree.command(guild=GUILD_OBJ, name="history",
              description="Portfolio summary as embeds (optionally filter by user)")
@app_commands.describe(
    user="Optional Discord user to filter by",
    include_buys="Include a compact list of individual buys (default: false)"
)
async def history_cmd(interaction: discord.Interaction,
                      user: discord.User | None = None,
                      include_buys: bool = False):
    await interaction.response.defer()

    # Query buys (optionally by user)
    sql = "SELECT rowid, user_id, symbol, usd, qty, dt_utc FROM buys"
    params = []
    if user:
        sql += " WHERE user_id=?"
        params.append(str(user.id))
    sql += " ORDER BY dt_utc ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

    if not rows:
        who = f" for {user.mention}" if user else ""
        await interaction.followup.send(f"No buys logged{who}.")
        return

    # Aggregate per symbol
    agg = defaultdict(lambda: {"usd": 0.0, "qty": 0.0})
    symbols = set()
    for rid, uid, sym, usd, qty, when in rows:
        s = (sym or "").upper()
        symbols.add(s)
        agg[s]["usd"] += float(usd)
        agg[s]["qty"] += float(qty)

    # Fetch latest prices for those symbols
    prices = await fetch_prices_map(symbols)

    # Build embeds
    embeds: list[discord.Embed] = []

    # Totals first
    total_cost, total_value = 0.0, 0.0
    for s in symbols:
        usd = agg[s]["usd"]; qty = agg[s]["qty"]
        price = prices.get(s)
        if price is not None and qty > 0:
            total_cost += usd
            total_value += price * qty
        else:
            total_cost += usd

    if total_cost > 0 and total_value > 0:
        overall_pct = ((total_value / total_cost) - 1) * 100
        colour = Colour.green() if overall_pct >= 0 else Colour.red()
        totals = Embed(
            title="📊 Portfolio Summary" + (f" — {user.display_name}" if user else ""),
            description=f"**Cost:** ${total_cost:,.2f}\n**Value:** ~${total_value:,.2f}\n**P/L:** {overall_pct:+.2f}%",
            colour=colour
        )
        embeds.append(totals)
    else:
        totals = Embed(
            title="📊 Portfolio Summary" + (f" — {user.display_name}" if user else ""),
            description=f"**Cost:** ${total_cost:,.2f}\n**Value:** n/a",
            colour=Colour.dark_grey()
        )
        embeds.append(totals)

    # One card per symbol
    # (Discord caps at 10 embeds per message; we'll batch-send if needed)
    per_coin_embeds: list[discord.Embed] = []
    for s in sorted(symbols):
        usd = agg[s]["usd"]; qty = agg[s]["qty"]
        avg = (usd / qty) if qty else 0.0
        price = prices.get(s)
        if price is None or qty <= 0:
            e = Embed(title=f"{s}", description="Price unavailable", colour=Colour.dark_grey())
            e.add_field(name="Qty", value=f"{qty:g}", inline=True)
            e.add_field(name="Avg Cost", value=f"${avg:.6f}", inline=True)
            e.add_field(name="Cost", value=f"${usd:.2f}", inline=True)
            per_coin_embeds.append(e)
            continue

        value = price * qty
        pnl_usd = value - usd
        pnl_pct = ((price / avg) - 1) * 100 if avg > 0 else 0.0
        colour = Colour.green() if pnl_usd >= 0 else Colour.red()

        e = Embed(title=f"{s}", colour=colour)
        e.add_field(name="Qty", value=f"{qty:g}", inline=True)
        e.add_field(name="Avg Cost", value=f"${avg:.6f}", inline=True)
        e.add_field(name="Price", value=f"${price:.6f}", inline=True)
        e.add_field(name="Value", value=f"${value:.2f}", inline=True)
        e.add_field(name="Cost", value=f"${usd:.2f}", inline=True)
        e.add_field(name="P/L", value=f"{pnl_usd:+.2f} ({pnl_pct:+.2f}%)", inline=True)
        per_coin_embeds.append(e)

    # Send totals + up to 9 coin embeds per message (Discord limit)
    batch = [embeds[0]]  # start with totals
    for e in per_coin_embeds:
        if len(batch) == 10:
            await interaction.followup.send(embeds=batch)
            batch = []
        batch.append(e)
    if batch:
        await interaction.followup.send(embeds=batch)

    # Optional compact “Individual Buys” list (mobile-friendly)
    if include_buys:
        # Build lines like: 123 • AVAX • 4.25 @ $100.00 • 2025-08-21 • by @name
        buy_lines = []
        for rid, uid, sym, usd, qty, when in rows:
            dt_str = (when or "")[:10]
            uname = None
            try:
                if uid:
                    u = await bot.fetch_user(int(uid))
                    uname = f"@{u.display_name}"
            except Exception:
                uname = uid
            line = f"`{rid}` • **{(sym or '').upper()}** • {qty:g} for ${usd:.2f} • {dt_str}" + (f" • by {uname}" if uname else "")
            buy_lines.append(line)

        # Chunk into posts under Discord’s message length limits
        chunk = []
        total_chars = 0
        for line in buy_lines:
            if total_chars + len(line) + 1 > 1800:  # keep safe under 2000
                await interaction.followup.send("\n".join(chunk))
                chunk, total_chars = [], 0
            chunk.append(line)
            total_chars += len(line) + 1
        if chunk:
            await interaction.followup.send("\n".join(chunk))

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
        choice, seed, cand_count, pool_size = await pick_coin()
        if not choice:
            msg = "No candidates passed the filters today."
            await interaction.followup.send(msg)
            _last_pick = (msg, dt.datetime.now(dt.timezone.utc).timestamp())
            return
        msg = (f"🎲 **Random Pick:** {choice['name']} ({choice['symbol']})  [rank #{choice['rank']}]\n"
               f"Seed: `{seed}` | Universe after filters: {cand_count} | Pool size (no-repeat {NO_REPEAT_WEEKS}w): {pool_size}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO picks(symbol, coin_name, dt_utc) VALUES(?,?,?)",
                             (choice['symbol'], choice['name'], dt.datetime.utcnow().isoformat()+'Z'))
            await db.commit()
        await interaction.followup.send(msg)
        await post_in_announce_channel("📣 " + msg)
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
                    file = None
                    try:
                        series = await fetch_7d_prices(sym)
                        if series and len(series) > 4:
                            png = make_sparkline_png(series, sym)
                            file = discord.File(png, filename=f"{sym}_7d.png")
                            embed.set_image(url=f"attachment://{sym}_7d.png")
                    except Exception as e:
                        print(f"[alert chart] {sym} chart skipped: {e}")

                    await post_in_announce_channel(embed=embed, file=file)
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

@tasks.loop(minutes=10)
async def alert_watcher():
    """
    Background loop to run alerts every 10 minutes.
    """
    await run_alerts_once()

# /alertx — set default or per-symbol multiple (e.g., 5x)
@tree.command(guild=GUILD_OBJ, name="alertx", description="Set X-multiple alerts (global or per-symbol)")
@app_commands.describe(multiple="e.g., 5 for 5x", symbol="Optional symbol (e.g., RSR). Omit to set default.")
async def alertx_cmd(interaction: discord.Interaction, multiple: float, symbol: str = None):
    if multiple <= 0:
        await interaction.response.send_message("Multiple must be > 0.", ephemeral=True); return
    await set_alert_multiple(symbol.upper() if symbol else None, multiple)
    target = (symbol.upper() if symbol else "ALL COINS (default)")
    await interaction.response.send_message(f"✅ Alerts set to **{multiple}×** for **{target}**.", ephemeral=True)

# /removealert — remove a per-symbol override (falls back to default)
@tree.command(guild=GUILD_OBJ, name="removealert", description="Remove a per-symbol alert override")
@app_commands.describe(symbol="Symbol to remove override for (e.g., RSR).")
async def removealert_cmd(interaction: discord.Interaction, symbol: str):
    await remove_alert_rule(symbol.upper())
    await interaction.response.send_message(f"🧽 Removed alert override for **{symbol.upper()}**.", ephemeral=True)

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

# /checkalerts — run the alert pass now
@tree.command(guild=GUILD_OBJ, name="checkalerts", description="Run the alert check right now")
async def checkalerts_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await run_alerts_once()
    await interaction.followup.send("✅ Alerts checked.", ephemeral=True)

# /alertstatus — pretty per-symbol math in embeds
@tree.command(guild=GUILD_OBJ, name="alertstatus", description="Show alert math (cost, price, multiple, next target)")
@app_commands.describe(symbol="Optional symbol, e.g., RSR")
async def alertstatus_cmd(interaction: discord.Interaction, symbol: str | None = None):
    await interaction.response.defer(ephemeral=True)
    costs = await get_symbol_costs_and_qty()
    if symbol:
        symbol = symbol.upper()
        costs = {k:v for k,v in costs.items() if k == symbol}
    if not costs:
        await interaction.followup.send("No symbols (or filter excluded all).", ephemeral=True); return

    prices = await fetch_prices_map(set(costs.keys()))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT symbol, last_step FROM alert_state"); state = dict(await cur.fetchall())
        cur = await db.execute("SELECT multiple FROM alert_rules WHERE symbol=''"); row = await cur.fetchone()
        default_mult = float(row[0]) if row else 5.0
        cur = await db.execute("SELECT symbol, multiple FROM alert_rules WHERE symbol<>''")
        overrides = {s.upper(): float(m) for s,m in await cur.fetchall()}

    for sym, (cost, _qty) in sorted(costs.items()):
        price = prices.get(sym)
        mult = overrides.get(sym, default_mult)
        last = int(state.get(sym, 0))
        if price is None or cost <= 0:
            e = Embed(title=f"{sym} status", description="No price available in fetched pages.", colour=Colour.dark_grey())
            await interaction.followup.send(embed=e, ephemeral=True); continue
        step_now = int(price / (cost * mult))
        next_target = cost * mult * (max(last,0)+1)
        colour = Colour.green() if price >= cost else Colour.red()
        e = Embed(title=f"{sym} status", colour=colour)
        e.add_field(name="Price", value=f"${price:,.6f}", inline=True)
        e.add_field(name="Avg Cost", value=f"${cost:,.6f}", inline=True)
        e.add_field(name="Multiple", value=f"{mult:g}×", inline=True)
        e.add_field(name="Last Step", value=str(last), inline=True)
        e.add_field(name="Step Now", value=str(step_now), inline=True)
        e.add_field(name="Next Target", value=f"${next_target:,.6f}", inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)

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
        await interaction.followup.send(f"Couldn't fetch OHLC for **{sym}**.", ephemeral=True)
        return

    try:
        png = make_candle_png(rows, sym, ma=ma_tuple)
        file = discord.File(png, filename=f"{sym}_{days}d.png")
        embed = Embed(
            title=f"{sym} chart",
            description=f"Candles: **{days}**; MA: {', '.join(map(str,ma_tuple)) if ma_tuple else 'none'}",
            colour=Colour.blurple()
        )
        embed.set_image(url=f"attachment://{sym}_{days}d.png")
        await interaction.followup.send(embed=embed, file=file)
    except Exception as e:
        await interaction.followup.send(f"Chart render failed: `{e}`", ephemeral=True)

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
                content = (f"📣 **Biweekly Pick** → {choice['name']} ({choice['symbol']}) — seed `{seed}`\n"
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

