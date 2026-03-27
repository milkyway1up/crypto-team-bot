import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

BG = "#0e0f14"
CARD = "#1a1b22"
WHITE = "#e8e8e8"
DIM = "#888"

coins = [
    {"label": "BAT\nrank #205",       "rank": 205, "ath_discount": 92, "price_change_24h": 1.2,  "vol_ratio": 0.03,  "color": "#4e79a7"},
    {"label": "Dead Coin\nrank #800", "rank": 800, "ath_discount": 97, "price_change_24h": -15,  "vol_ratio": 0.004, "color": "#e15759"},
    {"label": "Sweet Spot\nrank #150","rank": 150, "ath_discount": 62, "price_change_24h": 4.5,  "vol_ratio": 0.08,  "color": "#59a14f"},
    {"label": "Pumping\nrank #80",    "rank": 80,  "ath_discount": 35, "price_change_24h": 28,   "vol_ratio": 0.12,  "color": "#f28e2b"},
    {"label": "Crashing\nrank #320",  "rank": 320, "ath_discount": 55, "price_change_24h": -18,  "vol_ratio": 0.02,  "color": "#b07aa1"},
]

def get_mults(c):
    rank = c["rank"]
    rank_m = 3.0 if rank <= 100 else 2.0 if rank <= 300 else 1.5 if rank <= 500 else 1.0
    d = c["ath_discount"]
    ath_m = 2.0 if 35 <= d <= 80 else 0.4 if d > 95 else 1.0
    ch = c["price_change_24h"]
    mom_m = 1.5 if 0 <= ch <= 10 else 0.7 if ch > 20 else 0.6 if ch < -10 else 1.0
    vr = c["vol_ratio"]
    vol_m = 1.5 if vr >= 0.05 else 0.4 if vr < 0.01 else 1.0
    total = rank_m * ath_m * mom_m * vol_m
    return rank_m, ath_m, mom_m, vol_m, total

fig = plt.figure(figsize=(16, 10), facecolor=BG)
fig.suptitle("Pick Scoring — How Each Factor Affects Your Coin's Chance of Being Picked",
             color=WHITE, fontsize=13, y=0.98)

# ── Layout: top row = 4 factor curve plots, bottom = radar + bar ──
gs = fig.add_gridspec(2, 5, hspace=0.45, wspace=0.4,
                      left=0.06, right=0.97, top=0.91, bottom=0.07)

factor_axes = [fig.add_subplot(gs[0, i]) for i in range(4)]
ax_radar = fig.add_subplot(gs[1, :2], polar=True)
ax_bar   = fig.add_subplot(gs[1, 2:])

# ── Factor curve plots ──
def curve_ax(ax, title, xlabel, xs, ys, coin_xs, coin_labels, coin_colors):
    ax.set_facecolor(CARD)
    ax.plot(xs, ys, color="#4e79a7", linewidth=2.5)
    ax.axhline(1.0, color=DIM, linewidth=0.8, linestyle="--")
    for cx, lbl, col in zip(coin_xs, coin_labels, coin_colors):
        idx = np.argmin(np.abs(np.array(xs) - cx))
        cy = ys[idx]
        ax.plot(cx, cy, "o", color=col, markersize=7, zorder=5)
    ax.set_title(title, color=WHITE, fontsize=9, pad=4)
    ax.set_xlabel(xlabel, color=DIM, fontsize=7)
    ax.set_ylabel("Multiplier", color=DIM, fontsize=7)
    ax.tick_params(colors=DIM, labelsize=7)
    ax.spines[:].set_color("#333")
    ax.yaxis.label.set_color(DIM)
    for spine in ax.spines.values():
        spine.set_color("#333")

# Rank curve
ranks = np.arange(1, 1001)
rank_curve = np.where(ranks <= 100, 3.0, np.where(ranks <= 300, 2.0, np.where(ranks <= 500, 1.5, 1.0)))
curve_ax(factor_axes[0], "Rank", "Market Cap Rank",
         ranks, rank_curve,
         [c["rank"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])
factor_axes[0].set_xlim(1, 1000)

# ATH discount curve
discounts = np.arange(0, 101)
ath_curve = np.where((discounts >= 35) & (discounts <= 80), 2.0, np.where(discounts > 95, 0.4, 1.0))
curve_ax(factor_axes[1], "ATH Discount", "% Below All-Time High",
         discounts, ath_curve,
         [c["ath_discount"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])
factor_axes[1].axvspan(35, 80, alpha=0.12, color="#59a14f")
factor_axes[1].set_xlim(0, 100)

# Momentum curve
changes = np.linspace(-30, 40, 300)
def mom(x):
    if 0 <= x <= 10: return 1.5
    elif x > 20: return 0.7
    elif x < -10: return 0.6
    return 1.0
mom_curve = np.array([mom(x) for x in changes])
curve_ax(factor_axes[2], "24h Momentum", "24h Price Change %",
         changes, mom_curve,
         [c["price_change_24h"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])
factor_axes[2].axvspan(0, 10, alpha=0.12, color="#59a14f")

# Volume curve
vols = np.linspace(0, 0.15, 300)
def vol(x):
    if x >= 0.05: return 1.5
    elif x < 0.01: return 0.4
    return 1.0
vol_curve = np.array([vol(x) for x in vols])
curve_ax(factor_axes[3], "Volume Activity", "Volume / Market Cap",
         vols, vol_curve,
         [c["vol_ratio"] for c in coins], [c["label"].split("\n")[0] for c in coins], [c["color"] for c in coins])

# ── Radar chart ──
categories = ["Rank", "ATH\nDiscount", "Momentum", "Volume"]
N = len(categories)
angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
angles += angles[:1]

ax_radar.set_facecolor(CARD)
ax_radar.set_theta_offset(np.pi / 2)
ax_radar.set_theta_direction(-1)
ax_radar.set_ylim(0, 3.5)
ax_radar.set_xticks(angles[:-1])
ax_radar.set_xticklabels(categories, color=WHITE, fontsize=8)
ax_radar.tick_params(colors=DIM, labelsize=7)
ax_radar.set_yticks([1, 2, 3])
ax_radar.set_yticklabels(["1×", "2×", "3×"], color=DIM, fontsize=6)
ax_radar.grid(color="#333", linewidth=0.8)
ax_radar.spines["polar"].set_color("#444")
ax_radar.set_title("Factor Multipliers per Coin", color=WHITE, fontsize=9, pad=14)

for c in coins:
    rm, am, mm, vm, _ = get_mults(c)
    vals = [rm, am, mm, vm]
    vals += vals[:1]
    ax_radar.plot(angles, vals, color=c["color"], linewidth=2)
    ax_radar.fill(angles, vals, color=c["color"], alpha=0.08)

# ── Bar chart ──
ax_bar.set_facecolor(CARD)
x = np.arange(len(coins))
totals = [get_mults(c)[4] for c in coins]
bar_colors = [c["color"] for c in coins]
bars = ax_bar.bar(x, totals, color=bar_colors, width=0.55, edgecolor=BG, linewidth=1.2)
for bar, total in zip(bars, totals):
    ax_bar.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
                f"{total:.2f}×", ha="center", va="bottom", color=WHITE, fontsize=10, fontweight="bold")
ax_bar.set_xticks(x)
ax_bar.set_xticklabels([c["label"] for c in coins], color=WHITE, fontsize=8)
ax_bar.set_ylabel("Final Score", color=DIM, fontsize=9)
ax_bar.set_title("Final Score (higher = more likely to be picked)", color=WHITE, fontsize=9)
ax_bar.tick_params(colors=DIM)
for spine in ax_bar.spines.values():
    spine.set_color("#333")
ax_bar.set_ylim(0, max(totals) * 1.18)

# Legend
handles = [mpatches.Patch(color=c["color"], label=c["label"].replace("\n", " ")) for c in coins]
fig.legend(handles=handles, loc="lower center", ncol=5, facecolor=CARD,
           edgecolor="#444", labelcolor=WHITE, fontsize=8, bbox_to_anchor=(0.5, 0.0))

fig.savefig("score_demo.png", dpi=150, bbox_inches="tight", facecolor=BG)
print("Saved score_demo.png")
