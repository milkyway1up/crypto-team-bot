import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

BG = "#0d0e12"
CARD = "#16171e"
WHITE = "#eaeaea"
DIM = "#666"
GREEN = "#34d399"
RED = "#fb7185"
ACCENT = "#7c3aed"

coins = [
    {"label": "TAO (AI)\nrank #43",       "rank": 43,  "ath_discount": 74, "price_change_24h": -8.8, "vol_ratio": 0.06, "is_ai": True,  "is_meme": False, "color": GREEN},
    {"label": "KITE (AI)\nrank #113",      "rank": 113, "ath_discount": 45, "price_change_24h": 0.5,  "vol_ratio": 0.07, "is_ai": True,  "is_meme": False, "color": "#60a5fa"},
    {"label": "LINK (AI)\nrank #20",       "rank": 20,  "ath_discount": 86, "price_change_24h": -7.0, "vol_ratio": 0.04, "is_ai": True,  "is_meme": False, "color": ACCENT},
    {"label": "JST\nrank #85",             "rank": 85,  "ath_discount": 59, "price_change_24h": 4.7,  "vol_ratio": 0.06, "is_ai": False, "is_meme": False, "color": "#f59e0b"},
    {"label": "DOGE Meme\nrank #50",       "rank": 50,  "ath_discount": 60, "price_change_24h": 2.0,  "vol_ratio": 0.08, "is_ai": False, "is_meme": True,  "color": RED},
]

def get_mults(c):
    cat_m = 5.0 if c["is_ai"] else 0.05 if c["is_meme"] else 1.0
    rank = c["rank"]
    rank_m = 3.0 if rank <= 100 else 2.0 if rank <= 300 else 1.5 if rank <= 500 else 1.0
    d = c["ath_discount"]
    ath_m = 2.0 if 35 <= d <= 80 else 0.4 if d > 95 else 1.0
    ch = c["price_change_24h"]
    mom_m = 1.5 if 0 <= ch <= 10 else 0.7 if ch > 20 else 0.6 if ch < -10 else 1.0
    vr = c["vol_ratio"]
    vol_m = 1.5 if vr >= 0.05 else 0.4 if vr < 0.01 else 1.0
    total = cat_m * rank_m * ath_m * mom_m * vol_m
    return cat_m, rank_m, ath_m, mom_m, vol_m, total

fig = plt.figure(figsize=(18, 11), facecolor=BG)
fig.suptitle("Pick Scoring Algorithm — How Each Factor Affects Selection Probability",
             color=WHITE, fontsize=14, y=0.98, fontweight="bold")

gs = fig.add_gridspec(2, 5, hspace=0.45, wspace=0.4,
                      left=0.05, right=0.97, top=0.91, bottom=0.07)

factor_axes = [fig.add_subplot(gs[0, i]) for i in range(5)]
ax_radar = fig.add_subplot(gs[1, :2], polar=True)
ax_bar   = fig.add_subplot(gs[1, 2:])

def curve_ax(ax, title, xlabel, xs, ys, coin_xs, coin_labels, coin_colors):
    ax.set_facecolor(CARD)
    ax.plot(xs, ys, color=ACCENT, linewidth=2.5)
    ax.axhline(1.0, color=DIM, linewidth=0.8, linestyle="--")
    for cx, lbl, col in zip(coin_xs, coin_labels, coin_colors):
        idx = np.argmin(np.abs(np.array(xs) - cx))
        cy = ys[idx]
        ax.plot(cx, cy, "o", color=col, markersize=7, zorder=5)
    ax.set_title(title, color=WHITE, fontsize=9, pad=4)
    ax.set_xlabel(xlabel, color=DIM, fontsize=7)
    ax.set_ylabel("Multiplier", color=DIM, fontsize=7)
    ax.tick_params(colors=DIM, labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#333")

# Category factor (bar-style since it's discrete)
ax_cat = factor_axes[0]
ax_cat.set_facecolor(CARD)
cat_labels = ["Meme\n0.05x", "Other\n1.0x", "AI\n5.0x"]
cat_vals = [0.05, 1.0, 5.0]
cat_colors = [RED, DIM, GREEN]
bars = ax_cat.bar(range(3), cat_vals, color=cat_colors, width=0.6, edgecolor=BG)
ax_cat.set_xticks(range(3))
ax_cat.set_xticklabels(cat_labels, color=WHITE, fontsize=7)
ax_cat.set_title("Category", color=WHITE, fontsize=9, pad=4)
ax_cat.set_ylabel("Multiplier", color=DIM, fontsize=7)
ax_cat.tick_params(colors=DIM, labelsize=7)
ax_cat.axhline(1.0, color=DIM, linewidth=0.8, linestyle="--")
for spine in ax_cat.spines.values():
    spine.set_color("#333")
for c in coins:
    cat_x = 2 if c["is_ai"] else 0 if c["is_meme"] else 1
    cat_y = 5.0 if c["is_ai"] else 0.05 if c["is_meme"] else 1.0
    ax_cat.plot(cat_x, cat_y + 0.15, "o", color=c["color"], markersize=7, zorder=5)

# Rank curve
ranks = np.arange(1, 1001)
rank_curve = np.where(ranks <= 100, 3.0, np.where(ranks <= 300, 2.0, np.where(ranks <= 500, 1.5, 1.0)))
curve_ax(factor_axes[1], "Rank", "Market Cap Rank",
         ranks, rank_curve,
         [c["rank"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])
factor_axes[1].set_xlim(1, 1000)

# ATH discount curve
discounts = np.arange(0, 101)
ath_curve = np.where((discounts >= 35) & (discounts <= 80), 2.0, np.where(discounts > 95, 0.4, 1.0))
curve_ax(factor_axes[2], "ATH Discount", "% Below All-Time High",
         discounts, ath_curve,
         [c["ath_discount"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])
factor_axes[2].axvspan(35, 80, alpha=0.12, color=GREEN)
factor_axes[2].set_xlim(0, 100)

# Momentum curve
changes = np.linspace(-30, 40, 300)
def mom(x):
    if 0 <= x <= 10: return 1.5
    elif x > 20: return 0.7
    elif x < -10: return 0.6
    return 1.0
mom_curve = np.array([mom(x) for x in changes])
curve_ax(factor_axes[3], "24h Momentum", "24h Price Change %",
         changes, mom_curve,
         [c["price_change_24h"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])
factor_axes[3].axvspan(0, 10, alpha=0.12, color=GREEN)

# Volume curve
vols = np.linspace(0, 0.15, 300)
def vol(x):
    if x >= 0.05: return 1.5
    elif x < 0.01: return 0.4
    return 1.0
vol_curve = np.array([vol(x) for x in vols])
curve_ax(factor_axes[4], "Volume Activity", "Volume / Market Cap",
         vols, vol_curve,
         [c["vol_ratio"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])

# Radar chart
categories = ["Category", "Rank", "ATH\nDiscount", "Momentum", "Volume"]
N = len(categories)
angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
angles += angles[:1]

ax_radar.set_facecolor(CARD)
ax_radar.set_theta_offset(np.pi / 2)
ax_radar.set_theta_direction(-1)
ax_radar.set_ylim(0, 5.5)
ax_radar.set_xticks(angles[:-1])
ax_radar.set_xticklabels(categories, color=WHITE, fontsize=8)
ax_radar.tick_params(colors=DIM, labelsize=7)
ax_radar.set_yticks([1, 2, 3, 5])
ax_radar.set_yticklabels(["1x", "2x", "3x", "5x"], color=DIM, fontsize=6)
ax_radar.grid(color="#333", linewidth=0.8)
ax_radar.spines["polar"].set_color("#444")
ax_radar.set_title("Factor Multipliers per Coin", color=WHITE, fontsize=9, pad=14)

for c in coins:
    cm, rm, am, mm, vm, _ = get_mults(c)
    vals = [cm, rm, am, mm, vm]
    vals += vals[:1]
    ax_radar.plot(angles, vals, color=c["color"], linewidth=2)
    ax_radar.fill(angles, vals, color=c["color"], alpha=0.08)

# Bar chart
ax_bar.set_facecolor(CARD)
x = np.arange(len(coins))
totals = [get_mults(c)[5] for c in coins]
bar_colors = [c["color"] for c in coins]
bars = ax_bar.bar(x, totals, color=bar_colors, width=0.55, edgecolor=BG, linewidth=1.2)
for bar, total in zip(bars, totals):
    ax_bar.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                f"{total:.1f}x", ha="center", va="bottom", color=WHITE, fontsize=10, fontweight="bold")
ax_bar.set_xticks(x)
ax_bar.set_xticklabels([c["label"] for c in coins], color=WHITE, fontsize=8)
ax_bar.set_ylabel("Final Score", color=DIM, fontsize=9)
ax_bar.set_title("Final Score (higher = more likely to be picked)", color=WHITE, fontsize=9)
ax_bar.tick_params(colors=DIM)
for spine in ax_bar.spines.values():
    spine.set_color("#333")
ax_bar.set_ylim(0, max(totals) * 1.15)

# Legend
handles = [mpatches.Patch(color=c["color"], label=c["label"].replace("\n", " ")) for c in coins]
fig.legend(handles=handles, loc="lower center", ncol=5, facecolor=CARD,
           edgecolor="#444", labelcolor=WHITE, fontsize=8, bbox_to_anchor=(0.5, 0.0))

fig.savefig("score_demo.png", dpi=150, bbox_inches="tight", facecolor=BG)
print("Saved score_demo.png")
