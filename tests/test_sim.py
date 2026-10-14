import numpy as np
import pytest

from lob.book import BUY, SELL
from lob.sim import FlowParams, Simulator


def test_same_seed_same_run():
    a, b = Simulator(seed=3), Simulator(seed=3)
    a.run(600)
    b.run(600)
    assert a.rec.trades == b.rec.trades and a.counts == b.counts
    c = Simulator(seed=4)
    c.run(600)
    assert c.rec.trades != a.rec.trades


def test_market_order_rate():
    p = FlowParams()
    s = Simulator(p, seed=5)
    T = 3600
    s.run(T)
    expected = 2 * p.mu * T
    assert abs(s.counts["market"] - expected) < 4 * np.sqrt(expected)


def test_limit_distance_frequencies_follow_power_law():
    p = FlowParams()
    s = Simulator(p, seed=6)
    s.run(3600)
    observed = s.limit_distance_counts / s.limit_distance_counts.sum()
    expected = p.lam / p.lam.sum()
    n = s.limit_distance_counts.sum()
    assert np.all(np.abs(observed - expected) < 4 * np.sqrt(expected * (1 - expected) / n))


def test_cancellation_clocks_by_distance():
    # with no market orders nothing can trade, so every order's life ends at its own cancel clock
    p = FlowParams(mu=0.0)
    s = Simulator(p, seed=7, warmup=0)
    s.run(3600)
    life = np.array(s.lifetimes)
    for i in (1, 3, 6, 10):
        x = life[life[:, 1] == i, 0]
        assert x.mean() == pytest.approx(1 / p.theta[i - 1], rel=4 / np.sqrt(len(x)))


def test_book_never_crosses_and_never_empties():
    s = Simulator(seed=8)
    s.run(1800)
    bid, ask = np.array(s.rec.bid), np.array(s.rec.ask)
    assert np.all(bid < ask)
    assert not np.isnan(bid).any() and not np.isnan(ask).any()


def test_events_processed_in_time_order():
    s = Simulator(seed=9)
    seen = []

    class Watcher:
        def on_event(self, sim, kind):
            seen.append(sim.t)

    s.agents.append(Watcher())
    s.run(300)
    assert seen == sorted(seen) and len(seen) > 1000
