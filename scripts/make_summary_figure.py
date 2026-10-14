"""One-page summary figure from the saved results of Phases 4-6 (no re-simulation).

Run: .venv/bin/python scripts/make_summary_figure.py
Writes figures/summary.png.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
p4 = pd.read_csv(ROOT / "data/phase4_runs.csv")
p5 = pd.read_csv(ROOT / "data/phase5_runs.csv")
p6 = pd.read_csv(ROOT / "data/phase6_runs.csv")

fig, axes = plt.subplots(1, 3, figsize=(20, 5.5))

# 1. P&L attribution in the simulation
picks = [("naive", 0.5, "naive\nh=0.5"), ("naive", 2.0, "naive\nh=2"),
         ("A-S 1-hour", 1e-4, "A-S 1-hour\nγ=1e-4"), ("A-S end-of-day", 3e-5, "A-S end-of-day\nγ=3e-5")]
comp = {"spread_capture": "#55A868", "adverse_selection": "#C44E52", "other_inventory": "#8172B2"}
x = np.arange(len(picks))
for j, (c, col) in enumerate(comp.items()):
    vals = [p4[(p4["family"] == f) & np.isclose(p4["param"], v)][c].mean() for f, v, _ in picks]
    axes[0].bar(x + (j - 1) * 0.25, vals, 0.25, color=col, label=c.replace("_", " "))
tot = [p4[(p4["family"] == f) & np.isclose(p4["param"], v)]["total"] for f, v, _ in picks]
axes[0].errorbar(x, [t.mean() for t in tot], yerr=[t.std() for t in tot], fmt="ko", label="total (± sd across days)")
axes[0].axhline(0, color="black", lw=0.6)
axes[0].set_xticks(x, [p[2] for p in picks], fontsize=8)
axes[0].set_ylabel("tick-lots per day"); axes[0].set_title("Simulation: where P&L comes from (10 days)")
axes[0].legend(fontsize=7)

# 2. Ablation: effect of each friction
strategies = ["naive h = 0.5", "A-S 1-hour γ = 1e-04"]
envs = [e for e in p5["env"].unique() if e != "baseline"]
short = {e: e.split(":")[0].replace(" (latency 100 ms + fees + informed + limit + kill)", "") for e in envs}
y = np.arange(len(envs))
for k, (s, col) in enumerate(zip(strategies, ["#4C72B0", "#55A868"])):
    base = p5[(p5["strategy"] == s) & (p5["env"] == "baseline")].set_index("seed")["total"]
    diffs = [(p5[(p5["strategy"] == s) & (p5["env"] == e)].set_index("seed")["total"] - base).mean() for e in envs]
    axes[1].barh(y + (k - 0.5) * 0.38, diffs, 0.38, color=col, label=s)
axes[1].set_yticks(y, [short[e] for e in envs], fontsize=8); axes[1].invert_yaxis()
axes[1].axvline(0, color="black", lw=0.6)
axes[1].set_xlabel("change in daily P&L vs baseline (tick-lots)")
axes[1].set_title("Simulation: which friction matters most"); axes[1].legend(fontsize=8)

# 3. Real day
r = p6[p6["queue"] == "pessimistic"].set_index("strategy")
order = [s for s in r.index if "no limit" not in s]
axes[2].barh(np.arange(len(order)), r.loc[order, "pnl"], color=["#4C72B0" if s.startswith("naive") else "#55A868" for s in order])
axes[2].set_yticks(np.arange(len(order)), order, fontsize=8); axes[2].invert_yaxis()
axes[2].axvline(0, color="black", lw=0.6)
runaway = r.loc["naive: join best bid/ask, no limit", "pnl"]
axes[2].set_xlabel("P&L over 20 hours ($, before fees)")
axes[2].set_title(f"Real BTC-USD day (1 Jan 2024): no edge\n(not shown: naive with no limit, ${runaway:,.0f})")
fig.tight_layout()
fig.savefig(ROOT / "figures/summary.png", dpi=150)
print("wrote figures/summary.png")
