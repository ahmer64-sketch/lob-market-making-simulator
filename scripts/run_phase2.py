"""Phase 2: synthetic order flow and stylised facts.

Run: .venv/bin/python scripts/run_phase2.py
Simulates 5 trading days (6.5 hours each, seeds 0-4) of zero-intelligence order flow and
compares the book with stylised facts of real markets. Writes reports/phase2_flow.md and
figures/phase2_*.png.
"""
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import kurtosis

from lob.book import BUY, SELL
from lob.sim import FlowParams, Simulator

ROOT = Path(__file__).resolve().parents[1]
DAY = 6.5 * 3600
SEEDS = range(5)
P = FlowParams()
lines = []


def out(s=""):
    print(s)
    lines.append(s)


def acf(x, lags):
    x = np.asarray(x, float) - np.mean(x)
    v = np.dot(x, x)
    return np.array([np.dot(x[:-l], x[l:]) / v for l in lags])


# --- simulate ----------------------------------------------------------------------------------
runs, t0 = [], time.perf_counter()
for seed in SEEDS:
    s = Simulator(P, seed=seed, record_dt=1.0, warmup=60.0)
    s.run(DAY)
    runs.append(s)
elapsed = time.perf_counter() - t0
n_events = sum(sum(s.counts[k] for k in ("limit", "market", "cancel")) for s in runs)

spread = np.concatenate([np.array(s.rec.ask) - np.array(s.rec.bid) for s in runs])
qbest = np.concatenate([np.r_[s.rec.bid_qty, s.rec.ask_qty] for s in runs])
depth = np.mean(np.vstack([np.vstack(s.rec.depth) for s in runs]), axis=0)
mids = [(np.array(s.rec.bid) + np.array(s.rec.ask)) / 2 for s in runs]

out("# Phase 2: synthetic order flow\n")
out("Zero-intelligence order flow in the style of Cont, Stoikov & Talreja (2010): Poisson limit orders placed i "
    "ticks from the opposite best price at rate k/i^α, Poisson market orders, and an exponential cancellation "
    "clock per resting order. Parameters set the *form* of the model and give a liquid, mostly one-tick-spread "
    "book; they are not calibrated to a particular stock.\n")
out("| Parameter | Value |")
out("|---|---|")
out(f"| Limit orders per side, distance i = 1..{P.K} ticks from the opposite best | {P.k} / i^{P.alpha} per second "
    f"(total {P.lam.sum():.1f}/s per side) |")
out(f"| Market orders per side | {P.mu}/s |")
out(f"| Cancellation rate per resting order | {P.theta0} / i^{P.theta_decay} per second (by distance at placement) |")
out(f"| Order size | geometric, mean {P.size_mean} lots |")
out(f"\nSimulated: {len(SEEDS)} days × 6.5 hours, {n_events:,} events in {elapsed:.1f} s "
    f"(**{n_events / elapsed:,.0f} events/s** including flow generation). Book sampled every second.\n")

# --- stylised facts -------------------------------------------------------------------------------
taus = [1, 2, 5, 10, 30, 60, 300, 900]
sig = {}
for tau in taus:
    d = np.concatenate([m[tau::tau] - m[:-tau:tau] for m in mids])
    sig[tau] = d.var() / tau
r1 = np.concatenate([np.diff(m) for m in mids])
r10 = np.concatenate([m[10::10] - m[:-10:10] for m in mids])
r60 = np.concatenate([m[60::60] - m[:-60:60] for m in mids])
lags = list(range(1, 21))
acf_r1 = np.mean([acf(np.diff(m), lags) for m in mids], axis=0)
acf_abs10 = np.mean([acf(np.abs(m[10::10] - m[:-10:10]), lags) for m in mids], axis=0)
signs, gaps = [], []
for s in runs:
    tr = np.array(s.rec.trades)
    # One market order can fill several resting orders at the same instant. Count it once: sign and
    # timing are properties of the incoming order, not of each fill.
    t_unique, first = np.unique(tr[:, 0], return_index=True)
    signs.append(tr[first, 3])
    gaps.append(np.diff(t_unique))
acf_sign = np.mean([acf(x, lags) for x in signs], axis=0)
gaps = np.concatenate(gaps)
cv_gap = gaps.std() / gaps.mean()
daily_sd = np.sqrt(sig[900] * DAY)  # diffusion rate x day length (5 single-day moves would be too noisy)
hump = int(np.argmax(depth)) + 1

out("## 1. Does it look like a real book? Stylised facts\n")
out("| Stylised fact (real markets) | This simulator | Reproduced? |")
out("|---|---|---|")
share = {k: np.mean(spread == k) for k in (1, 2, 3)}
out(f"| Liquid large-tick stocks: spread is one tick most of the time | 1 tick {share[1]:.0%}, 2 ticks {share[2]:.0%}, "
    f"3+ ticks {1 - share[1] - share[2]:.0%} of the time | yes |")
out(f"| Book never empty on liquid names | one side empty {np.mean(np.isnan(spread)):.2%} of the time | yes |")
out(f"| Average depth is hump-shaped: largest a few ticks from the best, not at the best | largest at "
    f"{hump} ticks from the mid ({depth[hump - 1]:.1f} lots vs {depth[0]:.1f} at 1 tick) | "
    + ("yes (mild)" if hump > 1 else "no") + " |")
out(f"| Mid-price diffusive over longer horizons: variance grows linearly with time | variance per second "
    f"{sig[60]:.3f} at 1 min, {sig[900]:.3f} at 15 min (ticks²/s) | "
    + ("yes" if abs(sig[900] / sig[60] - 1) < 0.25 else "partly") + " |")
out(f"| Short-horizon mean reversion: consecutive mid changes negatively correlated | lag-1 autocorrelation of "
    f"1-second mid changes {acf_r1[0]:+.3f} | " + ("yes" if acf_r1[0] < -0.02 else "no") + " |")
out(f"| Fat tails at short horizons (kurtosis > 0, excess) | excess kurtosis {kurtosis(r10):.1f} at 10 s, "
    f"{kurtosis(r60):.1f} at 60 s | " + ("partly (short horizons only)" if kurtosis(r10) > 1 and kurtosis(r60) < 1 else
                                         "yes" if kurtosis(r60) > 1 else "no") + " |")
out(f"| Volatility clustering: autocorrelation of absolute returns positive and slowly decaying | lag-1 "
    f"{acf_abs10[0]:+.3f}, lag-10 {acf_abs10[9]:+.3f} (10-second returns) | "
    + ("no" if abs(acf_abs10[0]) < 0.05 else "weak") + " |")
out(f"| Order-flow persistence: signs of successive market orders strongly positively autocorrelated (long memory) | lag-1 "
    f"{acf_sign[0]:+.3f}, lag-10 {acf_sign[9]:+.3f} | " + ("no" if acf_sign[9] < 0.05 else "partly") + " |")
out(f"| Trades cluster in time (inter-trade times more variable than Poisson, CV > 1) | coefficient of variation "
    f"{cv_gap:.2f} | " + ("no (CV ≈ 1: Poisson)" if cv_gap < 1.1 else "yes") + " |")
out("")
out("The model reproduces the **mechanical** facts that come from how an order book works (tight spread, never "
    "empty, a depth hump, short-term mean reversion, diffusion at longer horizons) and misses the **behavioural** "
    "facts that come from how traders act (volatility clustering, persistent order flow, clustered trading). That is "
    "the known limit of zero-intelligence models: every order is independent, so nothing can create memory. For "
    "testing a market maker this matters: real order flow is more persistent, so real adverse selection and "
    "inventory risk are worse than here. Phase 5 adds informed traders to close part of that gap, and Phase 6 "
    "replays real order flow.\n")

out("## 2. Scale\n")
out(f"Daily mid-price volatility: {daily_sd:.0f} ticks over 6.5 hours (from the diffusion rate at 15 minutes, "
    f"{sig[900]:.3f} ticks²/s). With a tick of $0.01 on a $100 "
    f"stock (10,000 ticks) that is about {daily_sd / 100:.1f}% a day, i.e. roughly "
    f"{daily_sd / 100 * np.sqrt(252):.0f}% annualised: within the range of real large-cap stocks. Median best-queue "
    f"size {np.median(qbest):.0f} lots; {np.mean([len(s.rec.trades) for s in runs]):,.0f} fills per day.\n")

# --- figures --------------------------------------------------------------------------------------
fig, axes = plt.subplots(2, 3, figsize=(16, 8.5))
ax = axes[0, 0]
vals, cnts = np.unique(spread, return_counts=True)
ax.bar(vals, cnts / cnts.sum(), color="#4C72B0")
ax.set_xlabel("spread (ticks)"); ax.set_ylabel("share of time"); ax.set_title("Spread distribution")
ax = axes[0, 1]
ax.bar(np.arange(1, P.K + 1), depth, color="#55A868")
ax.set_xlabel("distance from mid (ticks)"); ax.set_ylabel("average lots per side"); ax.set_title("Average depth profile")
ax = axes[0, 2]
ax.hist(qbest, bins=np.arange(0, qbest.max() + 2) - 0.5, color="#8172B2", density=True)
ax.set_xlabel("lots at the best price"); ax.set_ylabel("share of time"); ax.set_title("Queue size at the best price")
ax = axes[1, 0]
ax.semilogx(taus, [sig[t] for t in taus], "o-", color="#C44E52")
ax.axhline(sig[900], color="grey", ls="--", lw=0.8)
ax.set_xlabel("horizon τ (seconds)"); ax.set_ylabel("Var(Δmid over τ) / τ  (ticks²/s)")
ax.set_title("Signature plot: flat = diffusive")
ax = axes[1, 1]
ax.plot(lags, acf_r1, "o-", label="mid changes (1 s)")
ax.plot(lags, acf_abs10, "s-", label="|mid changes| (10 s)")
ax.plot(lags, acf_sign, "^-", label="market-order signs")
ax.axhline(0, color="black", lw=0.6)
ax.set_xlabel("lag"); ax.set_ylabel("autocorrelation"); ax.set_title("Memory (real markets: |r| and signs > 0)")
ax.legend(fontsize=8)
ax = axes[1, 2]
bins = np.linspace(-15, 15, 31)
ax.hist(r60, bins=bins, density=True, color="#DD8452", alpha=0.7, label="60 s mid changes")
x = np.linspace(-15, 15, 200)
ax.plot(x, np.exp(-x**2 / (2 * r60.var())) / np.sqrt(2 * np.pi * r60.var()), "k--", lw=1, label="normal")
ax.set_xlabel("ticks"); ax.set_title("Mid-price changes over 60 s"); ax.legend(fontsize=8)
fig.tight_layout(); fig.savefig(ROOT / "figures/phase2_stylised_facts.png", dpi=150); plt.close(fig)

# Book heatmap: 10 minutes sampled every 0.5 s
s = Simulator(P, seed=0, warmup=0)
s.run(3600)
grid = np.arange(3600, 3600 + 600, 0.5)
lo, hi = None, None
snaps = []
for t in grid:
    s.run(t)
    snaps.append((t, dict(s.book.level_qty[BUY]), dict(s.book.level_qty[SELL]), s.book.best_bid(), s.book.best_ask()))
mids_h = np.array([(b + a) / 2 for _, _, _, b, a in snaps])
lo, hi = int(mids_h.min()) - 12, int(mids_h.max()) + 12
img = np.zeros((hi - lo + 1, len(grid)))
for j, (_, bids, asks, _, _) in enumerate(snaps):
    for p, q in bids.items():
        if lo <= p <= hi:
            img[p - lo, j] = -q
    for p, q in asks.items():
        if lo <= p <= hi:
            img[p - lo, j] = q
fig, ax = plt.subplots(figsize=(13, 5))
vmax = np.percentile(np.abs(img[img != 0]), 95)
ax.imshow(img, aspect="auto", origin="lower", cmap="RdBu_r", vmin=-vmax, vmax=vmax,
          extent=[0, 600, lo - 0.5, hi + 0.5])
ax.plot(grid - grid[0], mids_h, color="black", lw=0.8, label="mid")
tr = np.array([t for t in s.rec.trades if grid[0] <= t[0] <= grid[-1]])
ax.scatter(tr[:, 0] - grid[0], tr[:, 1], s=6, c=np.where(tr[:, 3] > 0, "#2ca02c", "#d62728"), label="trades")
ax.set_xlabel("seconds"); ax.set_ylabel("price (ticks)")
ax.set_title("Simulated order book, 10 minutes (blue = bids, red = asks, colour = queue size; dots = trades)")
ax.legend(loc="upper right", fontsize=8)
fig.tight_layout(); fig.savefig(ROOT / "figures/phase2_book_heatmap.png", dpi=150); plt.close(fig)

out("## 3. Figures\n")
out("![book](../figures/phase2_book_heatmap.png)\n")
out("![stylised facts](../figures/phase2_stylised_facts.png)")
(ROOT / "reports/phase2_flow.md").write_text("\n".join(lines) + "\n")
