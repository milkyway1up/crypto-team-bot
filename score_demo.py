import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# 5 example coins with realistic attributes
coins = [
    {
        "label": "BAT\n(actual pick)\nrank #205",
        "rank": 205, "ath_discount": 92, "price_change_24h": 1.2, "vol_ratio": 0.03,
    },
    {
        "label": "Dead Coin\nrank #800",
        "rank": 800, "ath_discount": 97, "price_change_24h": -15, "vol_ratio": 0.004,
    },
    {
        "label": "Sweet Spot\nrank #150",
        "rank": 150, "ath_discount": 62, "price_change_24h": 4.5, "vol_ratio": 0.08,
    },
    {
        "label": "Already Pumping\nrank #80",
        "rank": 80, "ath_discount": 35, "price_change_24h": 28, "vol_ratio": 0.12,
    },
    {
        "label": "Still Crashing\nrank #320",
        "rank": 320, "ath_discount": 55, "price_change_24h": -18, "vol_ratio": 0.02,
    },
]

def score_breakdown(c):
    rank = c["rank"]
    if rank <= 100:   rank_mult = 3.0
    elif rank <= 300: rank_mult = 2.0
    elif rank <= 500: rank_mult = 1.5
    else:             rank_mult = 1.0

    discount = c["ath_discount"]
    if 35 <= discount <= 80:  ath_mult = 2.0
    elif discount > 95:       ath_mult = 0.4
    else:                     ath_mult = 1.0

    ch = c["price_change_24h"]
    if 0 <= ch <= 10:   mom_mult = 1.5
    elif ch > 20:       mom_mult = 0.7
    elif ch < -10:      mom_mult = 0.6
    else:               mom_mult = 1.0

    vr = c["vol_ratio"]
    if vr >= 0.05:   vol_mult = 1.5
    elif vr < 0.01:  vol_mult = 0.4
    else:            vol_mult = 1.0

    base = 1.0
    return {
        "Rank":     base * rank_mult,
        "ATH Discount": base * rank_mult * ath_mult - base * rank_mult,
        "Momentum": base * rank_mult * ath_mult * mom_mult - base * rank_mult * ath_mult,
        "Volume":   base * rank_mult * ath_mult * mom_mult * vol_mult - base * rank_mult * ath_mult * mom_mult,
    }, base * rank_mult * ath_mult * mom_mult * vol_mult

labels = [c["label"] for c in coins]
breakdowns = []
totals = []
for c in coins:
    bd, total = score_breakdown(c)
    breakdowns.append(bd)
    totals.append(total)

keys = ["Rank", "ATH Discount", "Momentum", "Volume"]
colors = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759"]

x = np.arange(len(coins))
fig, ax = plt.subplots(figsize=(12, 6), facecolor="#0e0f14")
ax.set_facecolor("#0e0f14")

bottoms = np.zeros(len(coins))
for i, key in enumerate(keys):
    vals = np.array([bd[key] for bd in breakdowns])
    bars = ax.bar(x, vals, bottom=bottoms, color=colors[i], label=key, width=0.55, edgecolor="#0e0f14", linewidth=0.5)
    bottoms += vals

# Total score labels on top
for i, (total, bar_top) in enumerate(zip(totals, bottoms)):
    ax.text(i, bar_top + 0.05, f"{total:.2f}", ha="center", va="bottom",
            color="white", fontsize=10, fontweight="bold")

ax.set_xticks(x)
ax.set_xticklabels(labels, color="white", fontsize=9)
ax.set_ylabel("Score (higher = more likely to be picked)", color="white")
ax.set_title("Pick Scoring Breakdown — 5 Example Coins", color="white", fontsize=13, pad=14)
ax.tick_params(colors="white")
ax.spines[:].set_color("#444")
ax.yaxis.label.set_color("white")

legend = ax.legend(loc="upper right", facecolor="#1a1b22", edgecolor="#444", labelcolor="white")
fig.tight_layout()
fig.savefig("score_demo.png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print("Saved score_demo.png")
