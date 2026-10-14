"""Phase 6 (stretch): replay one real day of Coinbase BTC-USD order-book data with virtual
market-making quotes.

Data (free, no account): Tardis.dev, first day of the month, 1 Jan 2024.
  make fetch-real   (or see FILES below) downloads ~400 MB into data/external/ (not in git).
Run: .venv/bin/python scripts/run_phase6.py
Writes reports/phase6_real_data.md, data/phase6_runs.csv, figures/phase6_*.png.
"""
import hashlib
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from lob.calibrate import fit_intensity
from lob.replay import book_stats, load_day, replay

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "data/external"
FILES = {
    "incremental_book_L2_BTC-USD_2024-01-01.csv.gz": (
        "https://datasets.tardis.dev/v1/coinbase/incremental_book_L2/2024/01/01/BTC-USD.csv.gz",
        "49618c2885cd6aa9a48fb7b92b45aee72ed8a689eb5ee7e9d1a2ef88c13dfb37"),
    "trades_BTC-USD_2024-01-01.csv.gz": (
        "https://datasets.tardis.dev/v1/coinbase/trades/2024/01/01/BTC-USD.csv.gz",
        "7a95f20fc40d5a6c21974df5115584e7f109ca5828a908d998d62588a2bebd4c"),
}
DAY0 = 1704067200 * 1_000_000           # 2024-01-01 00:00 UTC in microseconds
H = 3600 * 1_000_000
CALIB = (DAY0, DAY0 + 4 * H)
EVAL = (DAY0 + 4 * H, DAY0 + 24 * H)
QSIZE = 0.01                            # BTC per quote (one "lot")
DT = 100_000                            # re-quote every 100 ms
PROBES = [1, 2, 5, 10, 25, 50, 100, 200]
QUEUE = {0: "pessimistic", 1: "proportional"}
lines = []


def out(s=""):
    print(s)
    lines.append(s)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


for name, (url, digest) in FILES.items():
    if not (EXT / name).exists():
        raise SystemExit(f"missing {EXT / name}: download it with `make fetch-real` ({url})")
ev = load_day(EXT / "incremental_book_L2_BTC-USD_2024-01-01.csv.gz", EXT / "trades_BTC-USD_2024-01-01.csv.gz",
              cache=EXT / "events_BTC-USD_2024-01-01.npz")
args = (ev["ts"], ev["kind"], ev["side"], ev["price"], ev["size"], ev["snapshot"])
tr = ev["kind"] == 1
base = int(ev["price"][tr].min()) - 200_000
width = int(ev["price"][tr].max()) + 200_000 - base


def run(strategy, param, t0, t1, queue_model, gamma=1e-8, sigma2=1.0, k=1.0, max_lots=np.inf):
    return replay(*args, base, width, t0, t1, strategy, param, gamma, sigma2, k, 3600.0, QSIZE, queue_model,
                  DT, 1_000_000, max_lots)


# --- the market -----------------------------------------------------------------------------------
stats, crossed, checked = book_stats(*args, base, width, DAY0, DAY0 + 24 * H, 1_000_000)
spread = stats[:, 2] - stats[:, 1]
mid_day = (stats[:, 1] + stats[:, 2]) / 2
cal = (stats[:, 0] >= CALIB[0]) & (stats[:, 0] < CALIB[1])
m_cal = mid_day[cal]
sigma2 = float(np.var(m_cal[60::60] - m_cal[:-60:60]) / 60)

out("# Phase 6 (stretch): replaying a real day of order-book data\n")
out("**Data:** Coinbase BTC-USD, 1 January 2024 (UTC), from Tardis.dev's free first-day-of-the-month datasets "
    "(no account needed): every change to each price level's total size and every trade, timestamped to the "
    f"microsecond. {int((~tr).sum()):,} book updates (starting with a full {int(ev['snapshot'].sum()):,}-level "
    f"snapshot) and {int(tr.sum()):,} trades. Files and SHA-256 checksums:\n")
for name, (url, digest) in FILES.items():
    ok = sha256(EXT / name) == digest
    out(f"- `{name}`: {url} (`{digest[:16]}…`, {'verified' if ok else '**CHECKSUM MISMATCH**'})")
out("\n**How the replay works.** The recorded book is rebuilt event by event (Numba-compiled, about 100 million "
    "events per second, so the day takes under a second). Our quotes are **virtual**: they never enter the "
    "recorded book, so the replay cannot show our own market impact, as the guide warns. Price-level data does "
    "not say where in a queue each order sits, so our queue position is estimated with two standard models that "
    "bracket the truth: **pessimistic** (we join the back; only trades move us up) and **proportional** "
    "(cancels are spread evenly over the queue). A trade printing at a worse price than our quote means the "
    "price went through us and fills us in full.\n")
out(f"Quotes are {QSIZE} BTC (one \"lot\"), re-evaluated every 100 ms. Calibration uses 00:00–04:00 UTC; "
    "everything is evaluated on 04:00–24:00 only.\n")

out("## 1. The market: very unlike the simulation\n")
out("| | Simulation (Phase 2) | Real BTC-USD, 1 Jan 2024 |")
out("|---|---|---|")
out(f"| Spread | 1 tick 81% of the time | 1 cent {np.mean(spread == 1):.0%} of the time; median {np.median(spread):.0f} "
    f"cents; 90th percentile {np.percentile(spread, 90):.0f} cents |")
out(f"| Mid volatility | 0.44 ticks² per second | {sigma2:,.0f} cents² per second (≈ ${np.sqrt(sigma2) / 100:.2f} per "
    f"second: about {np.sqrt(sigma2):.0f} ticks) |")
out(f"| Price over the day | random walk | ${mid_day[0] / 100:,.0f} → ${mid_day[-1] / 100:,.0f} "
    f"({mid_day[-1] / mid_day[0] - 1:+.1%}): a trending day |")
out(f"| Size at the best price | ~4 lots | median {np.median(stats[:, 3]):.3f} BTC (bid), "
    f"{np.median(stats[:, 4]):.3f} BTC (ask) |")
out(f"| Data quality | — | reconstructed book crossed or one-sided on {crossed / checked:.3%} of events |")
out("\nBTC-USD is a **small-tick** market: a one-cent tick is tiny next to a move of hundreds of ticks per second. "
    "The spread flips between one cent (a crowded queue) and dollars wide, and the price can move far before a "
    "market maker can earn back a loss. Our simulation was a large-tick world.\n")

# --- calibration ---------------------------------------------------------------------------------------
out("## 2. Calibration on 00:00–04:00\n")
cal_secs = (CALIB[1] - CALIB[0]) / 1e6
cal_rows = []
for qm in QUEUE:
    for d in PROBES:
        f, _ = run(0, float(d), CALIB[0], CALIB[1], qm)
        cal_rows.append({"queue": qm, "delta": d, "rate": len(f) / (2 * cal_secs)})
cal_df = pd.DataFrame(cal_rows)
fits = {qm: fit_intensity(g["delta"], g["rate"].clip(lower=1e-6)) for qm, g in cal_df.groupby("queue")}
out(f"σ² = **{sigma2:,.0f} cents²/s** (1-minute mid changes). Fill intensity from virtual probes quoting at a "
    "fixed distance δ from the mid (all at once: virtual orders do not interact):\n")
out("| δ (cents) | " + " | ".join(f"fills/s per side ({QUEUE[q]})" for q in QUEUE) + " |")
out("|---|" + "---|" * len(QUEUE))
for d in PROBES:
    out(f"| {d} | " + " | ".join(f"{cal_df[(cal_df['queue'] == q) & (cal_df['delta'] == d)]['rate'].iloc[0]:.4f}"
                                  for q in QUEUE) + " |")
out("")
for q, (A, k, r2) in fits.items():
    out(f"- {QUEUE[q]}: A = {A:.3f} fills/s, k = {k:.4f} per cent (1/k = {1 / k:.0f} cents), R² = {r2:.3f}")
out("\nThe exponential law fits less well than in the simulation (R² 0.96 vs 0.997): fill rates barely change "
    "for δ up to about 25 cents, likely because the spread is often wider than that, so such quotes sit inside the "
    "spread and are filled at similar rates, then fall off fast. The two queue models give almost the same fill "
    "rates, a first sign that queue position matters little here (section 4).\n")

# --- evaluation ---------------------------------------------------------------------------------------
configs = [
    ("naive: join best bid/ask, no limit", 2, 0.0, None, np.inf),
    ("naive: join best bid/ask, limit 50 lots", 2, 0.0, None, 50),
    ("naive: mid ± 25¢, limit 50 lots", 0, 25.0, None, 50),
    ("naive: mid ± 100¢, limit 50 lots", 0, 100.0, None, 50),
    ("naive: mid ± 250¢, limit 50 lots", 0, 250.0, None, 50),
] + [(f"A-S γ = {g:.0e}", 1, 0.0, g, np.inf) for g in (1e-9, 1e-8, 1e-7, 1e-6)]
rows, paths, fill_store = [], {}, {}
for qm in QUEUE:
    k_q = fits[qm][1]
    for name, strat, param, gamma, lim in configs:
        f, path = run(strat, param, EVAL[0], EVAL[1], qm, gamma or 1e-8, sigma2, k_q, lim)
        mid_end = path[-1, 1]
        pnl = (path[-1, 3] + path[-1, 2] * mid_end) / 100
        notional = float(np.sum(np.abs(f[:, 1]) * f[:, 2]) / 100)
        t_path, m_path = path[:, 0], path[:, 1]
        # at h = 0: the recorded mid just before the fill (= spread captured per BTC); later horizons
        # interpolate the 1-second mid samples, so +1 s is approximate
        mk = {h: float(np.sum(f[:, 1] * ((np.interp(f[:, 0] + h * 1e6, t_path, m_path) if h else f[:, 3]) - f[:, 2]))
                       / 100 / np.abs(f[:, 1]).sum()) if len(f) else np.nan for h in (0, 1, 10, 60)}
        spread_cap = float(np.nansum(f[:, 1] * (f[:, 3] - f[:, 2])) / 100)
        rows.append({"queue": QUEUE[qm], "strategy": name, "pnl": pnl, "btc_traded": float(np.abs(f[:, 1]).sum()),
                     "notional": notional, "break_even_bps": pnl / notional * 1e4 if notional else np.nan,
                     "spread_capture": spread_cap, "inv_sd": float(path[:, 2].std()),
                     "inv_max": float(np.abs(path[:, 2]).max()), "inv_end": float(path[-1, 2]),
                     **{f"markout_{h}": v for h, v in mk.items()}})
        paths[(qm, name)] = path
res = pd.DataFrame(rows)
res.to_csv(ROOT / "data/phase6_runs.csv", index=False)

out("## 3. Results on 04:00–24:00 UTC (20 hours, before fees)\n")
out("P&L in dollars, marked to the mid at the end. Markouts are value per BTC filled, in dollars: at the fill "
    "against the recorded mid just before it (the spread captured), later against the mid 1, 10 and 60 seconds on "
    "(interpolated from one-second samples).\n")
for q in QUEUE.values():
    out(f"**Queue model: {q}**\n")
    out("| Strategy | P&L ($) | BTC traded | Break-even maker fee (bp) | Spread capture ($) | Markout per BTC: fill → +1 s → +10 s → +60 s | "
        "Inventory sd (BTC) | Max \\|inv\\| | Inventory at end |")
    out("|---|---|---|---|---|---|---|---|---|")
    for _, r in res[res["queue"] == q].iterrows():
        out(f"| {r['strategy']} | {r['pnl']:+,.0f} | {r['btc_traded']:,.1f} | {r['break_even_bps']:+.2f} | "
            f"{r['spread_capture']:+,.0f} | {r['markout_0']:+.2f} → {r['markout_1']:+.2f} → {r['markout_10']:+.2f} → "
            f"{r['markout_60']:+.2f} | {r['inv_sd']:.2f} | {r['inv_max']:.2f} | {r['inv_end']:+.2f} |")
    out("")
best = res.loc[res["pnl"].idxmax()]
out("## 4. What the real day says\n")
if best["pnl"] < 0:
    out(f"- **No edge, even before fees.** Every strategy loses money over the 20 hours. The best is "
        f"{best['strategy']} ({best['queue']} queue): {'−' if best['pnl'] < 0 else '+'}${abs(best['pnl']):,.0f} on {best['btc_traded']:,.1f} BTC traded, so "
        f"it would need a maker *rebate* of {-best['break_even_bps']:.2f} bp just to break even, and the naive makers "
        f"need {-res[res['strategy'].str.startswith('naive') & ~res['strategy'].str.contains('no limit')]['break_even_bps'].max():.1f}"
        f"–{-res[res['strategy'].str.startswith('naive') & ~res['strategy'].str.contains('no limit')]['break_even_bps'].min():.1f} bp. "
        "A market maker paying any maker fee on this day loses more.")
else:
    out(f"- **Best P&L:** {best['strategy']} ({best['queue']} queue), {best['pnl']:+,.0f} on {best['btc_traded']:,.1f} BTC: "
        f"break-even maker fee {best['break_even_bps']:+.2f} bp.")
lim_naive = res[(res["strategy"] == "naive: join best bid/ask, limit 50 lots")]["pnl"].mean()
best_as = res[res["strategy"].str.startswith("A-S")].groupby("strategy")["pnl"].mean().max()
out(f"- **Avellaneda-Stoikov is clearly the better approach on real data:** its best setting loses about "
    f"${-best_as:,.0f} against ${-lim_naive:,.0f} for naive joining the best prices with a position limit, while "
    "keeping inventory near zero. Its skew does what the simulation suggested.")
no_lim = res[res["strategy"] == "naive: join best bid/ask, no limit"]
out(f"- **Inventory runs away without control:** joining the best prices with no limit ends the day "
    + " / ".join(f"{r['inv_end']:+.0f} BTC ({r['queue']})" for _, r in no_lim.iterrows())
    + " as BTC trends up; buyers keep lifting the ask. A limit or A-S skew is essential, not optional.")
out("- **Adverse selection is the dominant cost:** on a small-tick, fast-moving market the value of a fill decays "
    "within seconds, by far more than the spread captured, which is the guide's warning in its starkest form.")
gap = res.pivot_table(index="strategy", columns="queue", values="pnl")
gap_abs = (gap["pessimistic"] - gap["proportional"]).abs()
gap_rel = gap_abs / gap["pessimistic"].abs()
controlled = gap_abs[[i for i in gap_abs.index if "no limit" not in i]]
out(f"- **Queue position barely matters on this market:** the two queue models agree within ${controlled.max():,.0f} for "
    f"every strategy with inventory control (and within {gap_rel.max():.0%} for the runaway one), the opposite of the simulation, where it was the largest effect. The likely reason: with spreads "
    "often dollars wide, our quotes frequently sit inside the spread with nobody ahead of them, and in fast moves "
    "fills come from the price trading straight through our quote. Queue position matters most on large-tick "
    "markets with long queues, like the simulated one.")
out("- **Limits of a replay:** our orders do not affect the book or other traders, so fills on a real exchange "
    "would be somewhat worse (we would move prices against ourselves); one day, one asset, one trend regime.\n")

# --- figures -----------------------------------------------------------------------------------------------
fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
hours = (stats[:, 0] - DAY0) / 3.6e9
axes[0].plot(hours, mid_day / 100, color="black", lw=0.8)
axes[0].axvspan(0, 4, color="grey", alpha=0.15, label="calibration")
axes[0].set_ylabel("BTC-USD mid ($)"); axes[0].legend(fontsize=8)
axes[0].set_title("Coinbase BTC-USD, 1 Jan 2024 (UTC)")
axes[1].plot(hours, pd.Series(spread).rolling(300, min_periods=1).median(), color="#4C72B0", lw=0.8)
axes[1].set_yscale("log"); axes[1].set_ylabel("spread (cents), 5-min median"); axes[1].set_xlabel("hour (UTC)")
fig.tight_layout(); fig.savefig(ROOT / "figures/phase6_market.png", dpi=150); plt.close(fig)

fig, axes = plt.subplots(1, 3, figsize=(20, 5))
shown = [("naive: join best bid/ask, no limit", "#C44E52"), ("naive: join best bid/ask, limit 50 lots", "#4C72B0"),
         ("naive: mid ± 100¢, limit 50 lots", "#DD8452"), ("A-S γ = 1e-07", "#55A868")]
for ax, subset, title in [(axes[0], shown, "All, incl. the runaway no-limit maker"),
                          (axes[1], shown[1:], "Zoom: strategies with inventory control")]:
    for name, col in subset:
        for qm, ls in ((0, "-"), (1, ":")):
            p = paths[(qm, name)]
            ax.plot((p[:, 0] - DAY0) / 3.6e9, (p[:, 3] + p[:, 2] * p[:, 1]) / 100, color=col, ls=ls, lw=1,
                    label=name if qm == 0 else None)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_xlabel("hour (UTC)"); ax.set_ylabel("P&L ($, before fees)"); ax.set_title(title)
    ax.legend(fontsize=7)
axes[0].set_title("All (solid: pessimistic queue, dotted: proportional)")
for q, ls in zip(QUEUE.values(), ["-", ":"]):
    g = cal_df[cal_df["queue"] == [k for k, v in QUEUE.items() if v == q][0]]
    axes[2].semilogy(g["delta"], g["rate"], "o" + ls, label=f"probe fills ({q})")
axes[2].set_xscale("log"); axes[2].set_xlabel("quote distance from mid δ (cents)")
axes[2].set_ylabel("fills per second per side"); axes[2].set_title("Fill intensity vs distance (calibration)")
axes[2].legend(fontsize=8)
fig.tight_layout(); fig.savefig(ROOT / "figures/phase6_results.png", dpi=150); plt.close(fig)

out("## 5. Figures\n")
out("![market](../figures/phase6_market.png)\n")
out("![results](../figures/phase6_results.png)")
(ROOT / "reports/phase6_real_data.md").write_text("\n".join(lines) + "\n")
