"""Phase 5: realism. Ablation of frictions and risk controls for the naive and A-S market makers.

Run: .venv/bin/python scripts/run_phase5.py
Writes reports/phase5_realism.md, data/phase5_runs.csv, figures/phase5_*.png.
"""
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from lob.agents import AvellanedaStoikov, NaiveMarketMaker
from lob.informed import InformedTraders
from lob.sim import Simulator

ROOT = Path(__file__).resolve().parents[1]
DAY = 6.5 * 3600
START = 60.0
SEEDS = list(range(10))
SIGMA2, K = 0.441, 1.648            # Phase 4 calibration (not re-fitted per environment: model risk)
FEES = dict(maker_rebate=0.2, taker_fee=0.3)
KILL = 5_000.0
ENVS = {
    "baseline": {},
    "latency 100 ms": dict(latency=0.1),
    "latency 500 ms": dict(latency=0.5),
    "fees: maker rebate 0.2, taker fee 0.3": FEES,
    "informed traders": dict(informed=True),
    "ignore queue (front of queue)": dict(front_of_queue=True),
    "position limit 20 lots": dict(max_inventory=20),
    # only the taker fee it pays to flatten; no maker rebate, which would swamp the comparison
    f"kill switch at −{KILL:,.0f}": dict(kill_drawdown=KILL, taker_fee=FEES["taker_fee"]),
    "all realistic (latency 100 ms + fees + informed + limit + kill)": dict(
        latency=0.1, informed=True, max_inventory=20, kill_drawdown=KILL, **FEES),
}
FRICTIONS = ["latency 100 ms", "latency 500 ms", "fees: maker rebate 0.2, taker fee 0.3", "informed traders"]
CONTROLS = ["position limit 20 lots", f"kill switch at −{KILL:,.0f}"]
lines = []


def out(s=""):
    print(s)
    lines.append(s)


def make(strategy, kw):
    if strategy == "naive h = 0.5":
        return NaiveMarketMaker(half_spread=0.5, **kw)
    return AvellanedaStoikov(gamma=1e-4, sigma2=SIGMA2, k=K, tau=3600.0, **kw)


def run(args):
    strategy, env, seed = args
    t0 = time.perf_counter()
    kw = dict(ENVS[env])
    informed = kw.pop("informed", False)
    sim = Simulator(seed=seed, warmup=0)
    mm = make(strategy, kw)
    mm.attach(sim, start=START)
    inf = None
    if informed:
        inf = InformedTraders(seed=seed)
        inf.attach(sim, start=START)
    sim.run(DAY)
    mm.on_event(sim, "end")
    a = mm.attribution(sim, horizon=10.0)
    path = np.array(mm.path, dtype=float)
    times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
    f = np.array(mm.fills, dtype=float)
    mk = {h: float(np.sum(f[:, 1] * ((np.interp(f[:, 0] + h, times, mids) if h else f[:, 3]) - f[:, 2]))
                   / np.abs(f[:, 1]).sum()) if len(f) else np.nan for h in (0, 1, 30, 300)}
    equity = path[:, 1] + path[:, 2] * path[:, 3]
    return {"strategy": strategy, "env": env, "seed": seed, **a, "lots": int(np.abs(f[:, 1]).sum()) if len(f) else 0,
            "inv_sd": float(path[:, 2].std()), "max_drawdown": float((equity - np.maximum.accumulate(equity)).min()),
            "killed": mm.killed_at is not None, "wall_seconds": time.perf_counter() - t0, "mid_var_per_s": float(np.var(mids[300::300] - mids[:-300:300]) / 300),
            **{f"markout_{h}": v for h, v in mk.items()}}


if __name__ == "__main__":
    strategies = ["naive h = 0.5", "A-S 1-hour γ = 1e-04"]
    with ProcessPoolExecutor() as ex:
        rows = list(ex.map(run, [(s, e, d) for s in strategies for e in ENVS for d in SEEDS]))
    runs = pd.DataFrame(rows)
    runs.to_csv(ROOT / "data/phase5_runs.csv", index=False)
    slow = runs.groupby("env")["wall_seconds"].median().sort_values(ascending=False)
    print("median wall seconds per simulated day by environment:\n" + slow.round(1).to_string(), flush=True)

    out("# Phase 5: realism: which friction hurts most?\n")
    out("Two strategies from Phase 4 (naive h = 0.5; A-S with a 1-hour horizon, γ = 1e-04), each run on the same "
        f"{len(SEEDS)} simulated days in {len(ENVS)} environments: the Phase 4 baseline, each friction alone, the "
        "risk controls, a counterfactual that ignores queue position, and everything realistic together. With every "
        "option off, the code reproduces the recorded Phase 4 results exactly (checked on full days for both "
        "strategies). P&L in tick-lots per day; paired differences are against the baseline on the same days.\n")
    out("| Setting | Model |")
    out("|---|---|")
    out("| Latency | Every cancel and new order reaches the book 100 ms (or 500 ms) after the decision; the old quote "
        "stays live meanwhile and can be filled |")
    out("| Fees | Maker rebate 0.2 ticks per lot filled passively (≈ $0.002/share), taker fee 0.3 per lot taken |")
    out("| Informed traders | News moves a hidden fair value 2–6 ticks about every 2 minutes; informed traders send "
        "2-lot market orders toward it (≈ 2 per second) until the mid catches up or 60 s pass |")
    out("| Ignore queue | Counterfactual: our orders go to the front of their price level, as a simulator that "
        "ignores queues implicitly assumes |")
    out("| Position limit | Stop quoting the side that would take \\|inventory\\| past 20 lots |")
    out(f"| Kill switch | If P&L falls {KILL:,.0f} below its intraday peak: cancel, flatten with a market order "
        "(paying the taker fee), stop for the day |")
    out("\nA-S keeps its Phase 4 calibration (σ², k) in every environment. Informed trading raises volatility, so "
        "in those runs its inputs are out of date: the kind of model risk a real desk faces.\n")

    base = {s: runs[(runs["strategy"] == s) & (runs["env"] == "baseline")].set_index("seed") for s in strategies}
    summary = []
    for s in strategies:
        out(f"## {s}\n")
        out("| Environment | Mean P&L ± sd | Change vs baseline (paired ± se) | Spread capture | Fees | Adverse sel. (10 s) "
            "| Lots | Inventory sd | Worst intraday drawdown | Markout per lot +1 s → +30 s → +5 min |")
        out("|---|---|---|---|---|---|---|---|---|---|")
        for env in ENVS:
            g = runs[(runs["strategy"] == s) & (runs["env"] == env)].set_index("seed")
            d = g["total"] - base[s]["total"]
            se = d.std() / np.sqrt(len(d))
            summary.append({"strategy": s, "env": env, "pnl": g["total"].mean(), "diff": d.mean(), "se": se,
                            "pnl_sd": g["total"].std(), "worst_dd": g["max_drawdown"].min(), "inv_sd": g["inv_sd"].mean(),
                            "killed": int(g["killed"].sum())})
            kill_note = f" ({int(g['killed'].sum())} of {len(g)} days killed)" if g["killed"].any() else ""
            change = "—" if env == "baseline" else f"{d.mean():+,.0f} ± {se:,.0f}"
            out(f"| {env}{kill_note} | {g['total'].mean():+,.0f} ± {g['total'].std():,.0f} | {change} | "
                f"{g['spread_capture'].mean():+,.0f} | {g['fees'].mean():+,.0f} | {g['adverse_selection'].mean():+,.0f} | "
                f"{g['lots'].mean():,.0f} | {g['inv_sd'].mean():.1f} | {g['max_drawdown'].min():+,.0f} | "
                f"{g['markout_1'].mean():+.2f} → {g['markout_30'].mean():+.2f} → {g['markout_300'].mean():+.2f} |")
        out("")
    summ = pd.DataFrame(summary)

    out("## Which friction hurts most?\n")
    out("Frictions ranked by their effect on mean daily P&L (paired with the baseline):\n")
    out("| Rank | " + " | ".join(strategies) + " |")
    out("|---|" + "---|" * len(strategies))
    ranked = {s: summ[(summ["strategy"] == s) & summ["env"].isin(FRICTIONS)].sort_values("diff") for s in strategies}
    for i in range(len(FRICTIONS)):
        out(f"| {i + 1} | " + " | ".join(f"{r.iloc[i]['env']}: {r.iloc[i]['diff']:+,.0f} ± {r.iloc[i]['se']:,.0f}"
                                          for r in ranked.values()) + " |")
    worst = {s: ranked[s].iloc[0]["env"] for s in strategies}
    out("")
    vol_base = runs[runs["env"] == "baseline"]["mid_var_per_s"].mean()
    vol_inf = runs[runs["env"] == "informed traders"]["mid_var_per_s"].mean()
    out(f"- **Most damaging:** {worst[strategies[0]]} for the naive maker, {worst[strategies[1]]} for A-S. Informed "
        f"traders raise the mid's variance (with the market maker present) from {vol_base:.2f} to {vol_inf:.2f} ticks² "
        "per second, and their effect shows in the markouts: fills keep losing value from 1 s to 5 min, unlike the "
        "baseline, where adverse selection is over within a second. For the naive maker these differences are noisy "
        "(its daily P&L swings by thousands), so only the 500 ms latency effect is clearly significant; A-S's P&L is so "
        "stable that even its smaller effects are.")
    lat = summ[(summ["strategy"] == strategies[1]) & summ["env"].str.startswith("latency")]
    if (lat["diff"] > 0).all():
        out("- **Latency helps A-S here, which needs care.** Its quotes land later, so it chases its own inventory "
            "less: fewer re-quotes and fills, each worth more a second later (the Phase 4 finding that eager "
            "re-quoting is A-S's main cost). This is *not* a general result: the simulator has no fast competing "
            "market makers or arbitrageurs to pick off stale quotes, which is the main way latency costs money in "
            "real markets. Here stale quotes can only be hit by random flow and the informed traders.")
    q = summ[summ["env"] == "ignore queue (front of queue)"]
    out("- **Queue position:** pretending our orders sit at the front of the queue changes daily P&L by "
        + ", ".join(f"{r['diff']:+,.0f} ({r['strategy']})" for _, r in q.iterrows())
        + ". That is how far a 'fill when the price touches' simulator would mislead.")
    out("- **Fees** are a rebate for a passive market maker: they add to P&L in proportion to volume, so they "
        "favour the high-volume strategies. They become a cost only when the strategy has to take liquidity "
        "(the kill switch).\n")

    out("## Risk controls\n")
    out("| Strategy | Control | Mean P&L change | Inventory sd (baseline → control) | Worst intraday drawdown (baseline → control) | Days killed |")
    out("|---|---|---|---|---|---|")
    for s in strategies:
        b = summ[(summ["strategy"] == s) & (summ["env"] == "baseline")].iloc[0]
        for env in CONTROLS:
            r = summ[(summ["strategy"] == s) & (summ["env"] == env)].iloc[0]
            out(f"| {s} | {env} | {r['diff']:+,.0f} ± {r['se']:,.0f} | {b['inv_sd']:.1f} → {r['inv_sd']:.1f} | "
                f"{b['worst_dd']:+,.0f} → {r['worst_dd']:+,.0f} | {r['killed']} |")
    out("")

    out("## All frictions together\n")
    for s in strategies:
        b = summ[(summ["strategy"] == s) & (summ["env"] == "baseline")].iloc[0]
        r = summ[(summ["strategy"] == s) & (summ["env"].str.startswith("all realistic"))].iloc[0]
        g = runs[(runs["strategy"] == s) & runs["env"].str.startswith("all realistic")]
        ex = (g["total"] - g["fees"]).mean()
        out(f"- **{s}:** {b['pnl']:+,.0f} a day in the idealised baseline → **{r['pnl']:+,.0f} ± {r['pnl_sd']:,.0f}** "
            f"with every friction and control on, of which {g['fees'].mean():+,.0f} is net fees (mostly maker rebates); "
            f"**{ex:+,.0f} without them**.")
    out("\nMaker rebates are worth as much as or more than the whole trading profit here: a market maker in this setting is "
        "largely paid by the exchange for providing liquidity, which is close to how many real ones operate.")
    out("")

    # Figures
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5))
    envs = [e for e in ENVS if e != "baseline"]
    y = np.arange(len(envs))
    for ax, s in zip(axes, strategies):
        sub = summ[(summ["strategy"] == s)].set_index("env").loc[envs]
        cols = ["#C44E52" if v < 0 else "#55A868" for v in sub["diff"]]
        ax.barh(y, sub["diff"], xerr=1.96 * sub["se"], color=cols, alpha=0.8)
        ax.set_yticks(y, [e.replace(" (latency 100 ms + fees + informed + limit + kill)", "") for e in envs], fontsize=8)
        ax.axvline(0, color="black", lw=0.8)
        ax.set_xlabel("change in mean daily P&L vs baseline (tick-lots, 95% CI)")
        ax.set_title(s)
        ax.invert_yaxis()
    fig.suptitle("Ablation: effect of each friction and control (same 10 days)")
    fig.tight_layout(); fig.savefig(ROOT / "figures/phase5_ablation.png", dpi=150); plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    for ax, s in zip(axes, strategies):
        for env, col in [("baseline", "#4C72B0"), ("informed traders", "#C44E52"), ("latency 500 ms", "#DD8452")]:
            g = runs[(runs["strategy"] == s) & (runs["env"] == env)]
            ax.plot([0, 1, 30, 300], [g[f"markout_{h}"].mean() for h in (0, 1, 30, 300)], "o-", color=col, label=env)
        ax.set_xscale("symlog", linthresh=1); ax.axhline(0, color="black", lw=0.6)
        ax.set_xlabel("seconds after fill"); ax.set_ylabel("value per lot (ticks)"); ax.set_title(f"Markouts: {s}")
        ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(ROOT / "figures/phase5_markouts.png", dpi=150); plt.close(fig)

    out("## Figures\n")
    out("![ablation](../figures/phase5_ablation.png)\n")
    out("![markouts](../figures/phase5_markouts.png)")
    (ROOT / "reports/phase5_realism.md").write_text("\n".join(lines) + "\n")
