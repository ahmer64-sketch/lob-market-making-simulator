import numpy as np
import pytest

from lob.agents import AvellanedaStoikov, MarketMaker, NaiveMarketMaker
from lob.book import BUY, SELL, OrderBook
from lob.informed import INFORMED, InformedTraders
from lob.sim import FlowParams, Simulator

QUIET = FlowParams(mu=0.0, k=0.0001)   # a market with (almost) no background activity


def _day(agent, seconds=1800, seed=1, informed=None):
    sim = Simulator(seed=seed, warmup=0)
    agent.attach(sim, start=1.0)
    if informed:
        informed.attach(sim, start=1.0)
    sim.run(seconds)
    agent.on_event(sim, "end")
    return sim, agent


def test_latency_delays_orders_reaching_the_book():
    class Fixed(MarketMaker):
        def quotes(self, sim):
            return 9_990, None
    sim = Simulator(QUIET, seed=2, warmup=0)
    mm = Fixed(latency=0.25)
    mm.attach(sim, start=1.0)
    sim.run(1.2)
    assert not any(o.owner == mm.owner for o in sim.book.orders.values())   # still in flight
    sim.run(1.3)
    assert any(o.owner == mm.owner and o.price == 9_990 for o in sim.book.orders.values())


def test_stale_quote_picked_off_before_cancel_arrives():
    """We decide to move our bid away, but a seller hits the old bid before the cancel lands."""
    class Mover(MarketMaker):
        price = 100

        def quotes(self, sim):
            return self.price, None
    sim = Simulator(QUIET, seed=3, warmup=0)
    sim.book = OrderBook()
    sim.book.limit(SELL, 105, 5, owner=0)
    mm = Mover(latency=0.5)
    mm.attach(sim, start=0.0)
    sim.run(0.6)                                   # bid at 100 is now live
    mm.price = 98                                  # decide to step back
    sim.run(0.75)                                  # decision made at 0.7, cancel lands at 1.2
    res = sim.book.market(SELL, 1, owner=0)        # a seller arrives at 0.8, before the cancel lands
    sim._record_trades(res.trades, 102.5)
    mm.on_event(sim, "x")
    assert mm.inventory == 1                       # picked off on the stale quote
    sim.run(1.3)
    assert any(o.owner == mm.owner and o.price == 98 for o in sim.book.orders.values())


def test_rebates_and_fees_booked_and_attribution_adds_up():
    sim, mm = _day(NaiveMarketMaker(maker_rebate=0.2))
    lots = sum(abs(q) for _, q, _, _ in mm.fills)
    assert mm.fees == pytest.approx(0.2 * lots)
    a = mm.attribution(sim)
    assert a["spread_capture"] + a["fees"] + a["adverse_selection"] + a["other_inventory"] == pytest.approx(a["total"])


def test_position_limit_respected():
    sim, mm = _day(NaiveMarketMaker(max_inventory=10), seconds=3600)
    inv = np.array([p[2] for p in mm.path])
    assert np.abs(inv).max() <= 10 + mm.size


def test_kill_switch_flattens_and_stops_quoting():
    sim, mm = _day(NaiveMarketMaker(kill_drawdown=50.0, taker_fee=0.3), seconds=3600)
    assert mm.killed_at is not None
    assert mm.inventory == 0
    if mm.inventory_at_kill != 0:   # flattening must have taken liquidity and paid the taker fee
        taker_lots = sum(abs(f[1]) for f, tk in zip(mm.fills, mm.fill_is_taker) if tk)
        assert taker_lots == abs(mm.inventory_at_kill) and mm.fees == pytest.approx(-0.3 * taker_lots)
    after = [f for f in mm.fills if f[0] > mm.killed_at + 1]
    assert after == [] and mm.orders == {BUY: None, SELL: None}


def test_front_of_queue_fills_more():
    _, back = _day(NaiveMarketMaker(), seed=4)
    _, front = _day(NaiveMarketMaker(front_of_queue=True), seed=4)
    assert len(front.fills) > 1.3 * len(back.fills)


def test_informed_traders_push_price_toward_the_news():
    sim = Simulator(seed=5, warmup=0)
    inf = InformedTraders(news_rate=1 / 60, seed=5)
    inf.attach(sim)
    sim.run(3600)
    times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
    moved_toward = []
    for t, m0, target in inf.news:
        if t + 30 < times[-1]:
            m30 = np.interp(t + 30, times, mids)
            moved_toward.append((m30 - m0) * np.sign(target - m0))
    assert len(moved_toward) > 30 and np.mean(moved_toward) > 1.5
    signs = [ts for (_, _, _, ts, _, to, _) in sim.rec.trades if to == INFORMED]
    assert inf.orders_sent > 0 and len(signs) >= inf.orders_sent


def test_informed_flow_makes_markouts_keep_falling():
    _, plain = _day(NaiveMarketMaker(), seconds=3600, seed=6)
    sim, informed = _day(NaiveMarketMaker(), seconds=3600, seed=6, informed=InformedTraders(seed=6))
    def markout(sim, mm, h):
        times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
        f = np.array(mm.fills, float)
        return np.sum(f[:, 1] * (np.interp(f[:, 0] + h, times, mids) - f[:, 2])) / np.abs(f[:, 1]).sum()
    # with informed traders, fills keep losing value between 1 s and 30 s
    assert markout(sim, informed, 30) < markout(sim, informed, 1) - 0.05


def test_kill_switch_works_out_of_a_large_position_in_clips():
    sim = Simulator(seed=0, warmup=0)
    mm = NaiveMarketMaker(kill_drawdown=5000.0, taker_fee=0.3)
    mm.attach(sim, start=60.0)
    sim.run(7200)
    mm.on_event(sim, "end")
    assert mm.killed_at is not None and abs(mm.inventory_at_kill) > 100   # bigger than one side of the book
    assert mm.inventory == 0
    taker = [abs(f[1]) for f, tk in zip(mm.fills, mm.fill_is_taker) if tk]
    assert max(taker) <= mm.flatten_clip and sum(taker) >= abs(mm.inventory_at_kill)
