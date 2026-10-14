"""Speed benchmarks (events per second) for the three engines in this project.

Run: .venv/bin/python scripts/benchmark.py   (needs the Phase 6 data for the replay rows)
Writes reports/benchmarks.md.
"""
import platform
import random
import time
from pathlib import Path

import numpy as np

from lob.book import BUY, SELL, OrderBook
from lob.replay import load_day, replay
from lob.sim import Simulator

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "data/external"


def best_of(fn, n=3):
    return min((fn() for _ in range(n)), key=lambda r: r[0])


def engine_ops(n=300_000, seed=1):
    rng = random.Random(seed)
    ops, next_id = [], 1
    for _ in range(n):
        u = rng.random()
        if u < 0.75:
            side = rng.choice([BUY, SELL])
            ops.append((0, side, 10_000 - side * rng.randint(-2, 200), rng.randint(1, 50)))
            next_id += 1
        elif u < 0.80:
            ops.append((1, rng.choice([BUY, SELL]), rng.randint(1, 400), 0))
            next_id += 1
        else:
            ops.append((2, rng.randint(max(1, next_id - 200), max(1, next_id - 1)), 0, 0))
    return ops


def run_engine(ops):
    book = OrderBook()
    t0 = time.perf_counter()
    for kind, a, b, c in ops:
        if kind == 0:
            book.limit(a, b, c)
        elif kind == 1:
            book.market(a, b)
        else:
            book.cancel(a)
    return time.perf_counter() - t0, len(ops)


def run_sim(seconds=3600.0):
    s = Simulator(seed=0)
    t0 = time.perf_counter()
    s.run(seconds)
    n = s.counts["limit"] + s.counts["market"] + s.counts["cancel"]
    return time.perf_counter() - t0, n


if __name__ == "__main__":
    rows = []
    ops = engine_ops()
    dt, n = best_of(lambda: run_engine(ops))
    rows.append(("Matching engine (Phase 1), pure Python", "limit / market / cancel orders", n, dt))
    dt, n = best_of(lambda: run_sim(), 2)
    rows.append(("Order-flow simulator (Phase 2), pure Python", "events incl. flow generation", n, dt))

    have_real = (EXT / "events_BTC-USD_2024-01-01.npz").exists() or (EXT / "trades_BTC-USD_2024-01-01.csv.gz").exists()
    speedup = None
    if have_real:
        ev = load_day(EXT / "incremental_book_L2_BTC-USD_2024-01-01.csv.gz", EXT / "trades_BTC-USD_2024-01-01.csv.gz",
                      cache=EXT / "events_BTC-USD_2024-01-01.npz")
        tr = ev["kind"] == 1
        base = int(ev["price"][tr].min()) - 200_000
        width = int(ev["price"][tr].max()) + 200_000 - base
        a = (ev["ts"], ev["kind"], ev["side"], ev["price"], ev["size"], ev["snapshot"])
        t0, t1 = int(ev["ts"][0]), int(ev["ts"][-1])

        def rp(fn, n_ev):
            args = tuple(x[:n_ev] for x in a)
            t_end = int(args[0][-1])
            start = time.perf_counter()
            fn(*args, base, width, t0, t_end, 2, 0.0, 1e-8, 1.0, 1.0, 3600.0, 0.01, 0, 100_000, 1_000_000, 50.0)
            return time.perf_counter() - start, n_ev

        replay(*a, base, width, t0, t1, 2, 0.0, 1e-8, 1.0, 1.0, 3600.0, 0.01, 0, 100_000, 1_000_000, 50.0)  # compile
        dt, n = best_of(lambda: rp(replay, len(a[0])))
        rows.append(("Real-data replay (Phase 6), Numba", "book updates + trades, full day", n, dt))
        n_small = 1_000_000
        dt_py, _ = rp(replay.py_func, n_small)
        dt_nb, _ = best_of(lambda: rp(replay, n_small))
        rows.append(("Same replay loop, plain Python (first 1M events)", "book updates + trades", n_small, dt_py))
        speedup = dt_py / dt_nb

    lines = ["# Benchmarks\n", f"Python {platform.python_version()} on {platform.machine()} "
             f"({platform.system()}), single core, best of several runs.\n",
             "| Engine | What is counted | Events | Seconds | **Events / second** |", "|---|---|---|---|---|"]
    for name, what, n, dt in rows:
        lines.append(f"| {name} | {what} | {n:,} | {dt:.2f} | **{n / dt:,.0f}** |")
    if speedup:
        lines.append(f"\nCompiling the replay loop with Numba makes it **{speedup:,.0f}× faster** than the same code "
                     "run as plain Python.")
    lines += ["\n## How the matching engine could go faster\n",
              "The Phase 1 engine favours clarity: Python dicts per price level (FIFO with O(1) cancel), a heap per "
              "side for the best price, an order-id map. The replay row shows the payoff of the next steps:",
              "- **Arrays instead of objects:** price levels as a dense array over a price window (as the replay "
              "does), orders in preallocated arrays indexed by id; no per-order Python objects.",
              "- **Compile the hot loop:** Numba (shown above) or Cython; here the same logic runs about two orders of "
              "magnitude faster.",
              "- **Memory layout:** intrusive linked lists per level stored in arrays, so cancels stay O(1) "
              "without dict overhead and data stays cache-friendly.",
              "- **Language:** C++ or Rust for production latency, where predictable memory and no garbage "
              "collection matter more than raw throughput."]
    (ROOT / "reports/benchmarks.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
