"""Phase 3: naive market maker (fixed symmetric quotes) over 10 simulated days.

Run: .venv/bin/python scripts/run_phase3.py
Writes reports/phase3_naive.md, data/phase3_days.csv, figures/phase3_*.png.
"""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from lob.agents import NaiveMarketMaker
from lob.sim import FlowParams, Simulator

ROOT = Path(__file__).resolve().parents[1]
DAY = 6.5 * 3600
SEEDS = list(range(10))
HALF_SPREADS = [0.5, 1.5]
HORIZONS = [0, 1, 2, 5, 10, 20, 30, 60]
lines = []


def out(s=""):
    print(s)
    lines.append(s)


def one_day(args):
    seed, h = args
    sim = Simulator(FlowParams(), seed=seed, warmup=0)
    mm = NaiveMarketMaker(half_spread=h, size=1, dt=0.1)
    mm.attach(sim, start=60.0)
    sim.run(DAY)
    mm.on_event(sim, "end")
    a = mm.attribution(sim, horizon=10.0)
    path = np.array(mm.path, dtype=float)
    times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
    fills = np.array(mm.fills, dtype=float)
    mark = {}
    for hz in HORIZONS:
        later = np.interp(fills[:, 0] + hz, times, mids) if hz else fills[:, 3]
        mark[hz] = float(np.sum(fills[:, 1] * (later - fills[:, 2])) / np.abs(fills[:, 1]).sum())
    inv = path[:, 2]
    equity = path[:, 1] + path[:, 2] * path[:, 3]
    return {"seed": seed, "h": h, **a, "fills": len(fills), "lots": int(np.abs(fills[:, 1]).sum()),
            "inv_sd": float(inv.std()), "inv_max_abs": float(np.abs(inv).max()), "inv_end": int(mm.inventory),
            "mid_move": float(mids[-1] - mids[0]), "markouts": mark,
            "path": (path[::30, 0], inv[::30], equity[::30]) if seed < 3 else None}


if __name__ == "__main__":
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(one_day, [(s, h) for h in HALF_SPREADS for s in SEEDS]))
    days = pd.DataFrame([{k: v for k, v in r.items() if k not in ("markouts", "path")} for r in res])
    (ROOT / "data").mkdir(exist_ok=True)
    days.to_csv(ROOT / "data/phase3_days.csv", index=False)

    out("# Phase 3: naive market maker\n")
    out(f"Fixed symmetric quotes: bid at mid − h, ask at mid + h (rounded outward to ticks), 1 lot each side, "
        f"post-only, re-checked every 0.1 s and left untouched (keeping queue priority) when the price is unchanged. "
        f"No inventory control. Fills only when the engine matches the order in its queue. {len(SEEDS)} simulated "
        "days of 6.5 hours on the Phase 2 order flow, same seeds for both settings. P&L in **tick-lots** "
        "(1 tick × 1 lot; with a $0.01 tick and 100-share lots, 1 tick-lot = $1), marked to mid.\n")

    out("## 1. Results per day (mean ± standard deviation across days)\n")
    out("| h (ticks) | Total P&L | Spread capture | Adverse selection (10 s) | Other inventory P&L | Lots filled | "
        "Inventory sd | Max \\|inventory\\| | Inventory at close |")
    out("|---|---|---|---|---|---|---|---|---|")
    for h in HALF_SPREADS:
        d = days[days["h"] == h]
        f = lambda c: f"{d[c].mean():+,.0f} ± {d[c].std():,.0f}"
        out(f"| {h} | **{f('total')}** | {f('spread_capture')} | {f('adverse_selection')} | {f('other_inventory')} | "
            f"{d['lots'].mean():,.0f} | {d['inv_sd'].mean():.0f} | {d['inv_max_abs'].mean():.0f} | "
            f"{d['inv_end'].abs().mean():.0f} |")
    main = days[days["h"] == 0.5]
    t = main["total"].mean() / (main["total"].std() / np.sqrt(len(main)))
    out(f"\nFor h = 0.5 the mean daily P&L is {main['total'].mean():+,.0f} with t = {t:+.1f} over {len(main)} days. "
        f"Spread capture alone averages {main['spread_capture'].mean():,.0f} a day ({main['spread_capture'].mean() / main['lots'].mean():.2f} "
        f"ticks per lot, i.e. about half a one-tick spread), but the inventory terms swing by far more: the standard "
        f"deviation of daily P&L is {main['total'].std():,.0f}, driven by the inventory term (sd "
        f"{main['inventory'].std():,.0f}).\n")
    out(f"**The inventory problem:** with no control, inventory wanders like a random walk (typical size "
        f"{main['inv_sd'].mean():.0f} lots, up to {main['inv_max_abs'].mean():.0f}), and the day's P&L then depends on "
        "which way the price happened to move while it was held. The market maker becomes a directional trader by "
        "accident. Avellaneda-Stoikov (Phase 4) attacks exactly this.\n")

    out("## 2. Markouts: what a fill is worth after it happens\n")
    out("Average P&L per lot filled, measured as q × (mid at t + h − fill price). At h = 0 it is the half-spread "
        "captured; as h grows the mid moves against the fill (adverse selection) and the value decays.\n")
    out("| h (ticks) | " + " | ".join(f"+{hz} s" for hz in HORIZONS) + " |")
    out("|---|" + "---|" * len(HORIZONS))
    for h in HALF_SPREADS:
        m = [np.mean([r["markouts"][hz] for r in res if r["h"] == h]) for hz in HORIZONS]
        out(f"| {h} | " + " | ".join(f"{v:+.3f}" for v in m) + " |")
    out("\nThe drop happens within one second and then the curve is flat. In this order flow adverse selection is "
        "purely **mechanical**: our fill often takes the last lot at a price level, which itself moves the mid. "
        "Nobody here trades on information, so there is no further drift after the fill. Real markets also have "
        "informed flow, which makes markouts keep falling over seconds to minutes; Phase 5 adds informed traders "
        "to measure that.\n")

    # Figures
    fig, axes = plt.subplots(2, 2, figsize=(14, 8))
    for r, col in zip([r for r in res if r["h"] == 0.5 and r["path"] is not None], ["#4C72B0", "#55A868", "#C44E52"]):
        tt, inv, eq = r["path"]
        axes[0, 0].plot(tt / 3600, inv, color=col, lw=0.8, label=f"day {r['seed']}")
        axes[0, 1].plot(tt / 3600, eq, color=col, lw=0.8, label=f"day {r['seed']}")
    axes[0, 0].axhline(0, color="black", lw=0.6)
    axes[0, 0].set_xlabel("hours"); axes[0, 0].set_ylabel("inventory (lots)")
    axes[0, 0].set_title("Inventory, naive market maker (h = 0.5)"); axes[0, 0].legend(fontsize=8)
    axes[0, 1].set_xlabel("hours"); axes[0, 1].set_ylabel("P&L marked to mid (tick-lots)")
    axes[0, 1].set_title("Intraday P&L"); axes[0, 1].legend(fontsize=8)
    for h, col in zip(HALF_SPREADS, ["#4C72B0", "#DD8452"]):
        m = [np.mean([r["markouts"][hz] for r in res if r["h"] == h]) for hz in HORIZONS]
        axes[1, 0].plot(HORIZONS, m, "o-", color=col, label=f"h = {h}")
    axes[1, 0].axhline(0, color="black", lw=0.6)
    axes[1, 0].set_xlabel("seconds after fill"); axes[1, 0].set_ylabel("ticks per lot")
    axes[1, 0].set_title("Markout curve: spread earned, then given back"); axes[1, 0].legend()
    comp = main[["spread_capture", "adverse_selection", "other_inventory", "total"]]
    x = np.arange(len(main))
    w = 0.2
    for j, (c, col) in enumerate(zip(comp.columns, ["#55A868", "#C44E52", "#8172B2", "black"])):
        axes[1, 1].bar(x + (j - 1.5) * w, comp[c], w, color=col, label=c.replace("_", " "))
    axes[1, 1].axhline(0, color="black", lw=0.6)
    axes[1, 1].set_xlabel("simulated day"); axes[1, 1].set_ylabel("tick-lots")
    axes[1, 1].set_title("Daily P&L attribution (h = 0.5)"); axes[1, 1].legend(fontsize=7)
    fig.tight_layout(); fig.savefig(ROOT / "figures/phase3_naive.png", dpi=150); plt.close(fig)

    out("## 3. Figures\n")
    out("![naive market maker](../figures/phase3_naive.png)")
    (ROOT / "reports/phase3_naive.md").write_text("\n".join(lines) + "\n")
