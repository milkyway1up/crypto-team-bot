# Crypto Team Trading View

A Discord bot for tracking group crypto portfolios, generating AI-weighted biweekly coin picks, and posting price alerts. Optional Coinbase integration for live portfolio sync.

## Commands

| Command | Visibility | Description |
|---|---|---|
| `/wallet` | Private | Portfolio dashboard — rendered as an image card with live prices, 24h change, and P/L |
| `/pick` | Public | AI-weighted coin pick. Heavily favors AI/utility tokens, penalizes meme coins |
| `/buy` | Private | Log a purchase — symbol, USD spent, quantity, optional date |
| `/removebuy` | Private | Delete a logged buy by row ID |
| `/connect-coinbase` | Private | Link your Coinbase read-only API key via a secure modal form |
| `/disconnect-coinbase` | Private | Remove your stored Coinbase API keys |
| `/chart` | Public | Candlestick chart for any coin (1d to max timeframe, optional moving averages) |
| `/alertx` | Private | Set the X-multiple alert threshold globally or per symbol |
| `/alerts` | Private | List all alert rules and the last step fired for each |

## Wallet

`/wallet` renders a dark-themed dashboard card as an image:

- Sorted by value, shows price, value, 24h change, and P/L per coin
- If you've linked Coinbase: pulls live balances automatically, merges cost basis from local `/buy` log
- If not linked: uses your local buy log only
- Buttons: **Refresh** (re-fetches prices) and **Show Buys** (shows your buy log)
- Ephemeral — only you can see it

## Pick Algorithm

Every other Friday at 10:00 UTC the bot automatically posts a pick to the announce channel. `/pick` runs it manually.

The pick uses a **weighted random** draw. Each candidate is scored on:

- **Category** (strongest factor):
  - AI tokens (artificial-intelligence, ai-agents, ai-applications, ai-framework, bittensor, etc.): **5x boost**
  - Meme tokens (meme-token, dog/cat/frog-themed, solana-meme, etc.): **0.05x penalty**
- **Rank** — top 100 = 3x, top 300 = 2x, top 500 = 1.5x
- **ATH Discount** — 35-80% below ATH = 2x boost; 95%+ below = 0.4x penalty
- **24h Momentum** — gentle uptrend (0-10%) = 1.5x; pumping (>20%) or dumping (<-10%) = penalized
- **Volume/MCap ratio** — active trading (>5%) = 1.5x; ghost coin (<1%) = 0.4x penalty

Filters: no stablecoins, no BTC/ETH/DOGE, Coinbase-listed only, 30%+ below ATH, no repeats within 8 weeks.

Result: AI tokens hold ~48% of pick weight with ~23 coins, while meme coins hold ~0.3% despite ~15 coins in the pool.

### Scoring Visualized

![Pick Scoring Breakdown](score_demo.png)

The **top row** shows how each factor's multiplier changes across its range. The Category chart shows the dominant effect — AI tokens get a 5x boost while meme coins are reduced to 0.05x. The remaining four charts show Rank, ATH Discount, Momentum, and Volume sweet spots.

The **bottom left** radar shows all five multipliers at once per coin. The **bottom right** bar chart is the final combined score — TAO and KITE (AI tokens) score 45x while a meme coin scores 0.7x.

## Coinbase Integration

Users can optionally link their Coinbase account for live portfolio tracking:

1. Create a **read-only** API key at [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/access/api)
2. Whitelist your server IP in the key settings
3. Run `/connect-coinbase` in Discord — a secure modal form opens (keys never visible in chat)
4. Keys are encrypted at rest in SQLite using Fernet (derived from the bot token)

## Alerts

The bot checks every 10 minutes. An alert fires when a coin's price crosses a new multiple of your average cost (default 5x). Each crossing step only fires once.

## Discord Setup

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**
2. Go to the **Bot** tab and click **Add Bot**
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. Copy the token into your `.env` as `DISCORD_TOKEN`
5. Go to **OAuth2 > URL Generator**, select scopes: `bot` and `applications.commands`
6. Under Bot Permissions select: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`
7. Copy the generated URL, open it in a browser, and invite the bot to your server
8. Copy your server ID and announcement channel ID into `.env`

## Local Setup

```bash
cp .env.example .env
# fill in .env

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python bot.py
```

### .env variables

```
DISCORD_TOKEN=
GUILD_ID=
ANNOUNCE_CHANNEL_ID=
ANNOUNCE_THREAD_ID=      # optional — post into a thread instead of the channel
TEST_ALERT_2PCT=false    # set true to enable +2% test nudge alerts
CG_API_KEY=              # optional — free CoinGecko Demo key for 100 calls/min (vs 5-15 without)
```

## Deployment

The bot runs on a Linux VPS with systemd.

**Service file** at `/etc/systemd/system/crypto-bot.service`:
```ini
[Unit]
Description=Crypto Team Trading View Discord Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/crypto-bot
Environment=PYTHONUNBUFFERED=1
Environment=MPLBACKEND=Agg
EnvironmentFile=/opt/crypto-bot/.env
ExecStart=/opt/crypto-bot/.venv/bin/python /opt/crypto-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Deploy updates:**
```bash
scp bot.py root@164.90.140.209:/opt/crypto-bot/bot.py
ssh root@164.90.140.209 "systemctl restart crypto-bot.service"
```
