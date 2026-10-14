import numpy as np
import pytest

from lob.agents import MarketMaker, NaiveMarketMaker
from lob.book import BUY, SELL, OrderBook
from lob.sim import FlowParams, Simulator


def _run(seconds=1800, seed=1, **kw):
    sim = Simulator(seed=seed, warmup=0)
    mm = NaiveMarketMaker(**kw)
    mm.attach(sim, start=1.0)
    sim.run(seconds)
    mm.on_event(sim, "end")
    return sim, mm


def test_inventory_and_cash_follow_fills():
    sim, mm = _run()
    assert len(mm.fills) > 50
    assert mm.inventory == sum(q for _, q, _, _ in mm.fills)
    assert mm.cash == pytest.approx(-sum(q * p for _, q, p, _ in mm.fills))


def test_fills_match_engine_trades_against_the_agent():
    sim, mm = _run()
    ours = [(t, p, q) for t, p, q, ts, mo, to, _ in sim.rec.trades if mo == mm.owner]
    assert [(t, p, abs(q)) for t, q, p, _ in mm.fills] == ours
    assert not any(to == mm.owner for *_, to, _ in sim.rec.trades)   # post-only: never the aggressor


def test_attribution_adds_up_exactly():
    sim, mm = _run()
    a = mm.attribution(sim)
    equity = mm.cash + mm.inventory * sim.book.mid()
    assert a["spread_capture"] + a["adverse_selection"] + a["other_inventory"] == pytest.approx(equity)
    assert a["spread_capture"] > 0           # passive fills at the bid/ask earn the half-spread


def test_quotes_kept_when_price_unchanged_preserving_queue_position():
    sim = Simulator(FlowParams(mu=0.0, k=0.0001), seed=2, warmup=0)   # a frozen market
    mm = NaiveMarketMaker()
    mm.attach(sim, start=0.5)
    sim.run(1.0)
    first = dict(mm.orders)
    sim.run(5.0)
    assert mm.orders == first and mm.quotes_placed == 2


def test_no_fill_on_touch_queue_priority_respected():
    """A market order smaller than the queue ahead of us trades at our price but does not fill us."""
    class Fixed(MarketMaker):
        def quotes(self, sim):
            return 100, None
    sim = Simulator(FlowParams(mu=0.0, k=0.0001), seed=3, warmup=0)
    sim.book = OrderBook()
    sim.book.limit(BUY, 100, 5, owner=0)        # background queue ahead of us
    sim.book.limit(SELL, 101, 5, owner=0)
    mm = Fixed()
    mm.attach(sim, start=0.0)
    sim.run(0.05)
    sim.rec.trades.extend((0.06, t.price, t.qty, t.taker_side, t.maker_owner, t.taker_owner, 100.5)
                          for t in sim.book.market(SELL, 3, owner=0).trades)
    mm.on_event(sim, "x")
    assert mm.fills == [] and mm.inventory == 0     # price touched 100, but 3 < 5 lots ahead of us
    sim.rec.trades.extend((0.07, t.price, t.qty, t.taker_side, t.maker_owner, t.taker_owner, 100.5)
                          for t in sim.book.market(SELL, 3, owner=0).trades)
    mm.on_event(sim, "x")
    assert mm.inventory == 1                         # now the queue ahead is gone and we fill


def test_wider_quotes_fill_less():
    _, tight = _run(half_spread=0.5, seed=4)
    _, wide = _run(half_spread=2.5, seed=4)
    assert len(wide.fills) < len(tight.fills)
