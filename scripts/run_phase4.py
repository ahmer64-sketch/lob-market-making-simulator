"""Phase 4: Avellaneda-Stoikov quotes, calibrated on separate simulated days, compared with the
naive market maker on the same 10 evaluation days.

Run: .venv/bin/python scripts/run_phase4.py
Writes reports/phase4_avellaneda_stoikov.md, data/phase4_runs.csv, figures/phase4_*.png.
"""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from lob.agents import AvellanedaStoikov, NaiveMarketMaker
from lob.book import BUY, SELL
from lob.calibrate import fit_intensity, mid_variance_rate
from lob.sim import Simulator

ROOT = Path(__file__).resolve().parents[1]
DAY = 6.5 * 3600
START = 60.0
EVAL_SEEDS = list(range(10))          # same days as Phase 3
CALIB_SEEDS = (100, 101, 102, 103, 104)
PROBE_H = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
lines = []


def out(s=""):
    print(s)
    lines.append(s)


# --- calibration ----------------------------------------------------------------------------------
def probe(args):
    h, seed = args
    sim = Simulator(seed=seed, warmup=0)
    mm = NaiveMarketMaker(half_spread=h)
    mm.attach(sim, start=START)
    sim.run(2 * 3600)
    mm.on_event(sim, "end")
    dist = (mm.dist_sum[BUY] + mm.dist_sum[SELL]) / (mm.dist_n[BUY] + mm.dist_n[SELL])
    return h, len(mm.fills) / (2 * (2 * 3600 - START)), dist


def free_day(seed):
    s = Simulator(seed=seed)
    s.run(DAY)
    return (np.array(s.rec.bid) + np.array(s.rec.ask)) / 2


# --- evaluation ------------------------------------------------------------------------------------
def make_agent(cfg, sigma2, k):
    if cfg["family"] == "naive":
        return NaiveMarketMaker(half_spread=cfg["param"])
    kw = dict(gamma=cfg["param"], sigma2=sigma2, k=k, rounding=cfg.get("rounding", "nearest"))
    if cfg["family"] == "A-S end-of-day":
        return AvellanedaStoikov(end_time=DAY, **kw)
    return AvellanedaStoikov(tau=3600.0, **kw)


def run_day(args):
    cfg, seed, sigma2, k = args
    sim = Simulator(seed=seed, warmup=0)
    mm = make_agent(cfg, sigma2, k)
    mm.attach(sim, start=START)
    sim.run(DAY)
    mm.on_event(sim, "end")
    a = mm.attribution(sim, horizon=10.0)
    path = np.array(mm.path, dtype=float)
    inv = path[:, 2]
    hours = (path[:, 0] // 3600).astype(int)
    by_hour = pd.Series(np.abs(inv)).groupby(hours).mean().reindex(range(7)).to_numpy()
    times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
    f = np.array(mm.fills, dtype=float)
    mk = {hz: float(np.sum(f[:, 1] * ((np.interp(f[:, 0] + hz, times, mids) if hz else f[:, 3]) - f[:, 2]))
                    / np.abs(f[:, 1]).sum()) for hz in (0, 1, 10, 60, 300)}
    return {**cfg, "seed": seed, **a, "lots": int(sum(abs(f[1]) for f in mm.fills)),
            "moved_mid_share": float(np.mean(mm.fill_moved_mid)), "requotes_per_fill": mm.requotes / len(mm.fills),
            **{f"markout_{hz}": v for hz, v in mk.items()},
            "inv_sd": float(inv.std()), "inv_max_abs": float(np.abs(inv).max()), "inv_end_abs": abs(int(mm.inventory)),
            "abs_inv_by_hour": by_hour, "inv_path": inv[::20] if seed == 0 else None}


if __name__ == "__main__":
    with ProcessPoolExecutor() as ex:
        probes = list(ex.map(probe, [(h, s) for h in PROBE_H for s in CALIB_SEEDS[:2]]))
        calib_mids = list(ex.map(free_day, CALIB_SEEDS[2:]))
    pr = pd.DataFrame(probes, columns=["h", "rate", "dist"]).groupby("h").mean()
    A, k, r2 = fit_intensity(pr["dist"], pr["rate"])
    sigma2 = mid_variance_rate(calib_mids, dt=1.0, horizon=300.0)
    half_opt = np.log1p(1e-9 / k) / 1e-9  # (1/gamma) ln(1 + gamma/k) as gamma -> 0, i.e. 1/k

    configs = ([{"family": "naive", "param": h} for h in (0.5, 1.0, 1.5, 2.0)]
               + [{"family": "A-S 1-hour", "param": g} for g in (1e-5, 3e-5, 1e-4, 3e-4, 1e-3)]
               + [{"family": "A-S end-of-day", "param": g} for g in (3e-6, 1e-5, 3e-5, 1e-4, 3e-4)]
               + [{"family": "A-S 1-hour, outward rounding", "param": 1e-4, "rounding": "outward"}])
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(run_day, [(c, s, sigma2, k) for c in configs for s in EVAL_SEEDS]))
    runs = pd.DataFrame([{kk: v for kk, v in r.items() if kk not in ("abs_inv_by_hour", "inv_path")} for r in res])
    runs.to_csv(ROOT / "data/phase4_runs.csv", index=False)

    out("# Phase 4: Avellaneda-Stoikov quotes\n")
    out("Avellaneda & Stoikov (2008): reservation price r = s − q·γ·σ²·τ and total spread γσ²τ + (2/γ)·ln(1 + γ/k), "
        "quotes kept passive. Two horizons: **end-of-day** (τ = T − t, as in the paper and the guide) and a "
        "**constant 1-hour** τ (a common practical variant, see section 4). Calibration uses separate simulated days "
        f"(seeds {CALIB_SEEDS[0]}–{CALIB_SEEDS[-1]}); evaluation uses the same {len(EVAL_SEEDS)} days as Phase 3 "
        "(seeds 0–9) for every strategy, so comparisons are paired day by day.\n")

    out("## 1. Calibration\n")
    out("**Fill intensity.** A 1-lot probe quotes both sides at a fixed distance from the mid (Phase 3's naive "
        "maker with half-spread h), 2 hours × 2 seeds per distance; fills per second per side:\n")
    out("| Probe h | Realised mean distance (ticks) | Fills per second per side |")
    out("|---|---|---|")
    for h, r in pr.iterrows():
        out(f"| {h:g} | {r['dist']:.2f} | {r['rate']:.4f} |")
    out(f"\nFit of ln λ = ln A − k·δ: **A = {A:.2f} fills/s, k = {k:.2f} per tick, R² = {r2:.3f}**: the exponential "
        "decay A-S assumes fits this market closely. (Realised distances exceed h because quotes are rounded outward "
        "to whole ticks around a half-tick mid.)\n")
    out(f"**Volatility.** σ² = **{sigma2:.3f} ticks² per second**, from 5-minute mid changes on 3 calibration days "
        "(long enough to be diffusive; Phase 2's signature plot).\n")
    out(f"**What it implies.** For small γ the optimal half-spread is (1/γ)·ln(1 + γ/k) → 1/k = {1 / k:.2f} ticks. In a "
        "market whose spread is usually one tick, the guide's code starter (bid rounded down, ask up) turns that into "
        "1.5 ticks, one level behind the best price, where fills are about 7× rarer; rounding to the nearest tick "
        "gives 0.5, i.e. joining the best price. The main runs use nearest rounding; the guide's rounding is shown "
        "for comparison (section 3). This was decided from the calibration days, before any evaluation run.\n")

    # --- results table ------------------------------------------------------------------------------
    out("## 2. Results: same 10 days for every strategy\n")
    out("P&L in tick-lots per day (1 tick × 1 lot; $1 with a $0.01 tick and 100-share lots).\n")
    out("| Strategy | Setting | Mean P&L ± sd | P&L / sd | Spread capture | Adverse selection (10 s) | Lots filled | "
        "Inventory sd | Max \\|inv\\| | \\|inv\\| at close |")
    out("|---|---|---|---|---|---|---|---|---|---|")
    summary = runs.groupby(["family", "param"], sort=False).agg(
        pnl=("total", "mean"), pnl_sd=("total", "std"), spread=("spread_capture", "mean"),
        adverse=("adverse_selection", "mean"), lots=("lots", "mean"), inv_sd=("inv_sd", "mean"),
        inv_max=("inv_max_abs", "mean"), inv_end=("inv_end_abs", "mean")).reset_index()
    for _, r in summary.iterrows():
        setting = f"h = {r['param']:g}" if r["family"] == "naive" else f"γ = {r['param']:.0e}"
        out(f"| {r['family']} | {setting} | {r['pnl']:+,.0f} ± {r['pnl_sd']:,.0f} | {r['pnl'] / r['pnl_sd']:.1f} | {r['spread']:+,.0f} | "
            f"{r['adverse']:+,.0f} | {r['lots']:,.0f} | {r['inv_sd']:.1f} | {r['inv_max']:.0f} | {r['inv_end']:.1f} |")
    out("")

    # --- done-when check ----------------------------------------------------------------------------
    out("## 3. Does A-S beat naive on inventory risk at similar P&L? (the guide's test)\n")
    out("For each naive setting: the A-S setting with the lowest inventory risk among those earning at least as "
        "much on the same days (paired difference ± standard error over 10 days).\n")
    out("| Naive setting | Naive P&L | Best A-S at ≥ that P&L | A-S P&L | Paired P&L diff | Inventory sd: naive → A-S | Daily P&L sd: naive → A-S |")
    out("|---|---|---|---|---|---|---|")
    asx = summary[summary["family"].isin(["A-S 1-hour", "A-S end-of-day"])]
    for _, n in summary[summary["family"] == "naive"].iterrows():
        ok = asx[asx["pnl"] >= n["pnl"]]
        if ok.empty:
            best_as = asx.loc[asx["pnl"].idxmax()]
            note = "none earns as much; highest-earning A-S shown"
        else:
            best_as = ok.loc[ok["inv_sd"].idxmin()]
            note = ""
        nd = runs[(runs["family"] == "naive") & (runs["param"] == n["param"])].set_index("seed")["total"]
        ad = runs[(runs["family"] == best_as["family"]) & (runs["param"] == best_as["param"])].set_index("seed")["total"]
        diff = ad - nd
        out(f"| h = {n['param']:g} | {n['pnl']:+,.0f} | {best_as['family']}, γ = {best_as['param']:.0e}"
            + (f" ({note})" if note else "") + f" | {best_as['pnl']:+,.0f} | {diff.mean():+,.0f} ± "
            f"{diff.std() / np.sqrt(len(diff)):,.0f} | {n['inv_sd']:.1f} → {best_as['inv_sd']:.1f} | "
            f"{n['pnl_sd']:,.0f} → {best_as['pnl_sd']:,.0f} |")
    out("\n**Against naive settings that earn 1,500–1,900 a day (h = 1 to 2), the test is met with room to spare:** "
        "A-S earns more with a small fraction of the inventory risk. **Against the tightest naive maker (h = 0.5) it is "
        "not:** no A-S setting matches its ~7,000 a day; A-S gives up roughly 4,000 a day for inventory control, "
        "although on a risk-adjusted basis (P&L / sd, section 2) it is far ahead. Section 4 explains where the gap "
        "comes from.\n")
    out_row = summary[summary["family"] == "A-S 1-hour, outward rounding"].iloc[0]
    near_row = summary[(summary["family"] == "A-S 1-hour") & (summary["param"] == 1e-4)].iloc[0]
    out(f"**Rounding changes behaviour, not P&L, here:** at γ = 1e-04, the guide's outward rounding fills "
        f"{out_row['lots']:,.0f} lots a day against {near_row['lots']:,.0f} with nearest rounding (it quotes one level "
        f"back most of the time) yet earns about the same ({out_row['pnl']:+,.0f} vs {near_row['pnl']:+,.0f}): fewer "
        "fills, each worth more.\n")

    # --- why A-S earns less than the tightest naive maker ----------------------------------------------
    out("## 4. Why A-S earns less than the tightest naive maker\n")
    out("Both quote mostly at the best prices and capture almost the same half-spread at the moment of the fill. The "
        "difference is what happens next:\n")
    out("| Strategy | Value per lot at fill | +1 s | +10 s | +60 s | +5 min | Fills where our order was last at its price (fill moved the mid) | Re-quotes per fill |")
    out("|---|---|---|---|---|---|---|---|")
    for fam, prm in [("naive", 0.5), ("A-S 1-hour", 1e-5), ("A-S 1-hour", 1e-4), ("A-S 1-hour", 1e-3)]:
        g = runs[(runs["family"] == fam) & (runs["param"] == prm)]
        lab = f"naive h = {prm:g}" if fam == "naive" else f"{fam}, γ = {prm:.0e}"
        out(f"| {lab} | " + " | ".join(f"{g[f'markout_{hz}'].mean():+.3f}" for hz in (0, 1, 10, 60, 300))
            + f" | {g['moved_mid_share'].mean():.0%} | {g['requotes_per_fill'].mean():.2f} |")
    out("\nThe two kinds of strategy capture the same half-spread at the moment of the fill (+0.53); the difference is "
        "how much of it survives. Two effects show up, and how much each matters depends on γ:\n")
    out("- **Losing queue position (clear at high γ).** A-S moves its quotes whenever its inventory changes. Each move "
        "sends the order to the back of the queue, and orders at the back tend to fill just as the level is used up "
        "and the mid moves. At γ = 1e-03, A-S re-quotes 4 times per fill and two-thirds of its fills move the mid "
        "(naive: 0.35 and 39%), and almost all of each fill's value is gone within a second. At γ = 1e-04 the effect "
        "is small (42% vs 39%), and at γ = 1e-05 it is absent: queue position alone does not explain those settings.")
    out("- **Paying to unload inventory (the intended mechanism).** Skewing means moving quotes towards where the "
        "market will take them: when long, A-S lowers its ask, often posting alone at a better price than the rest of "
        "the book, which itself shifts the mid. Part of the \"spread captured\" at the fill is measured against a mid "
        "that A-S's own quote created, and it is given back as the book refills. At γ = 1e-05 this shows as value "
        "decaying over one to five minutes (+0.26 → +0.14) while queue measures match the naive maker. This price "
        "concession is what inventory control costs; the continuous A-S model assumes quotes don't move the price and "
        "so does not count it.\n")
    out("These are the explanations the measurements support, not a full proof: Phase 5 measures queue position "
        "directly and separates the two.\n")

    # --- 4. end-of-day horizon ----------------------------------------------------------------------------
    eod = [r for r in res if r["family"] == "A-S end-of-day" and r["param"] == 3e-5]
    const = [r for r in res if r["family"] == "A-S 1-hour" and r["param"] == 1e-4]
    eod_h = np.nanmean([r["abs_inv_by_hour"] for r in eod], axis=0)
    const_h = np.nanmean([r["abs_inv_by_hour"] for r in const], axis=0)
    out("## 5. The end-of-day horizon problem\n")
    out("With τ = T − t the inventory penalty γσ²τ shrinks to zero at the close, so the end-of-day version stops "
        "managing inventory exactly when a desk most wants to be flat. Average |inventory| by hour of the day:\n")
    out("| Hour | " + " | ".join(str(h + 1) for h in range(7)) + " |")
    out("|---|" + "---|" * 7)
    out("| A-S end-of-day, γ = 3e-05 | " + " | ".join(f"{v:.1f}" for v in eod_h) + " |")
    out("| A-S 1-hour, γ = 1e-04 | " + " | ".join(f"{v:.1f}" for v in const_h) + " |")
    out("\nThe paper assumes inventory is simply valued at the mid at T; a real desk would add a closing penalty or "
        "flatten before the close. The constant-horizon variant avoids the problem.\n")

    # --- figures ---------------------------------------------------------------------------------------
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    styles = {"naive": ("#4C72B0", "o"), "A-S 1-hour": ("#55A868", "s"), "A-S end-of-day": ("#C44E52", "^"),
              "A-S 1-hour, outward rounding": ("#8172B2", "D")}
    for fam, g in summary.groupby("family", sort=False):
        col, mk = styles[fam]
        axes[0].plot(g["inv_sd"], g["pnl"], marker=mk, color=col, label=fam)
        axes[1].plot(g["pnl_sd"], g["pnl"], marker=mk, color=col, label=fam)
        for _, r in g.iterrows():
            lab = f"h={r['param']:g}" if fam == "naive" else f"{r['param']:.0e}"
            axes[0].annotate(lab, (r["inv_sd"], r["pnl"]), fontsize=6, xytext=(3, 3), textcoords="offset points")
    axes[0].set_xscale("log"); axes[0].set_xlabel("inventory sd (lots, log scale)"); axes[0].set_ylabel("mean daily P&L")
    axes[0].set_title("P&L vs inventory risk (up-left is better)"); axes[0].legend(fontsize=7)
    axes[1].set_xlabel("sd of daily P&L"); axes[1].set_ylabel("mean daily P&L"); axes[1].set_title("P&L vs P&L risk")
    nv = [r for r in res if r["family"] == "naive" and r["param"] == 0.5 and r["inv_path"] is not None][0]
    for r, col, lab in [(nv, "#4C72B0", "naive h = 0.5")] + [(x, "#55A868", "A-S 1-hour γ = 1e-04") for x in const if x["inv_path"] is not None]:
        axes[2].plot(np.arange(len(r["inv_path"])) * 20 / 3600 + START / 3600, r["inv_path"], color=col, lw=0.8, label=lab)
    axes[2].axhline(0, color="black", lw=0.6)
    axes[2].set_xlabel("hours"); axes[2].set_ylabel("inventory (lots)"); axes[2].set_title("Inventory, same simulated day")
    axes[2].legend(fontsize=8)
    fig.tight_layout(); fig.savefig(ROOT / "figures/phase4_frontier.png", dpi=150); plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    axes[0].semilogy(pr["dist"], pr["rate"], "o", label="probe fill rates")
    xx = np.linspace(0.3, 4.6, 50)
    axes[0].semilogy(xx, A * np.exp(-k * xx), "k--", label=f"fit A e^(−kδ): A={A:.2f}, k={k:.2f}, R²={r2:.3f}")
    axes[0].set_xlabel("quote distance from mid δ (ticks)"); axes[0].set_ylabel("fills per second per side")
    axes[0].set_title("Calibrating fill intensity"); axes[0].legend(fontsize=8)
    axes[1].plot(range(1, 8), eod_h, "^-", color="#C44E52", label="A-S end-of-day γ=3e-05")
    axes[1].plot(range(1, 8), const_h, "s-", color="#55A868", label="A-S 1-hour γ=1e-04")
    axes[1].set_xlabel("hour of the trading day"); axes[1].set_ylabel("average |inventory| (lots)")
    axes[1].set_title("End-of-day horizon: inventory control fades at the close"); axes[1].legend(fontsize=8)
    fig.tight_layout(); fig.savefig(ROOT / "figures/phase4_calibration.png", dpi=150); plt.close(fig)

    out("## 6. Figures\n")
    out("![frontier](../figures/phase4_frontier.png)\n")
    out("![calibration](../figures/phase4_calibration.png)")
    (ROOT / "reports/phase4_avellaneda_stoikov.md").write_text("\n".join(lines) + "\n")
