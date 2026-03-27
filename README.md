# crypto-team-bot

A Discord bot for tracking a group crypto portfolio, generating biweekly coin picks, and posting price alerts.

## Commands

| Command | Description |
|---|---|
| `/buy` | Log a group buy — symbol, USD spent, quantity, optional date |
| `/history` | Portfolio summary with live P/L per coin. Pass `include_buys:true` for individual entries |
| `/removebuy` | Delete a logged buy by row ID (get the ID from `/history`) |
| `/pick` | Run the weighted pick algorithm manually and post the result |
| `/chart` | Candlestick chart for any coin. Options: `days` (1/7/14/30/90/180/365/max), `ma` (e.g. `7,20`) |
| `/alertx` | Set the X-multiple alert threshold globally or per symbol (e.g. `multiple:5`) |
| `/removealert` | Remove a per-symbol alert override, falls back to global default |
| `/alerts` | List all alert rules and the last step fired for each |
| `/alertstatus` | Show per-coin alert math — cost, price, multiple, next target |
| `/checkalerts` | Trigger an alert check immediately instead of waiting for the 10-minute loop |
| `/ping` | Health check |

## Biweekly Pick

Every other Friday at 10:00 UTC the bot automatically posts a pick to the announce channel.

The pick uses a **weighted random** draw — not pure random. Each candidate is scored on:

- **Rank** — top 100 = 3×, top 300 = 2×, top 500 = 1.5×
- **ATH Discount** — 35–80% below ATH = 2× boost; 95%+ below (likely dead project) = 0.4× penalty
- **24h Momentum** — gentle uptrend (0–10%) = 1.5×; already pumping (>20%) or dumping (<-10%) = penalized
- **Volume/MCap ratio** — active trading (>5%) = 1.5×; ghost coin (<1%) = 0.4× penalty

Filters applied before scoring: no stablecoins, no BTC/ETH/DOGE, must be Coinbase-listed, must be ≥30% below ATH, no repeats within 8 weeks.

### Scoring Visualised

![Pick Scoring Breakdown](score_demo.png)

The **top row** shows how each factor's multiplier changes across its range — the green shaded zones are the sweet spots. Each coloured dot is one of the five example coins plotted against that factor.

The **bottom left** radar shows all four multipliers at once per coin — a coin that fills out all four axes is getting boosted on every factor.

The **bottom right** is the final combined score. The Sweet Spot coin (rank #150, 62% below ATH, slight uptrend, active volume) scores 9× while the Dead Coin (rank #800, 97% below ATH, dumping, no volume) scores 0.10×. Higher score = higher probability of being selected, but it's still random so lower-scored coins can still win.

## Alerts

The bot checks every 10 minutes. An alert fires when a coin's price crosses a new multiple of your average cost (default 5×). Each crossing step only fires once.

## Setup

```
cp .env.example .env
# fill in .env

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python bot.py
```

### .env variables

```
DISCORD_TOKEN=
GUILD_ID=
ANNOUNCE_CHANNEL_ID=
ANNOUNCE_THREAD_ID=      # optional
TEST_ALERT_2PCT=false    # set true to enable +2% test nudge alerts
```

## Deployment (systemd)

The bot runs as a systemd service on the VM under the `cryptobot` user.

```bash
# Deploy updates
git pull
sudo systemctl restart crypto-bot.service
sudo systemctl status crypto-bot.service
```
