import math

import numpy as np
import pytest

from lob.agents import AvellanedaStoikov, NaiveMarketMaker
from lob.calibrate import fit_intensity, mid_variance_rate
from lob.sim import Simulator


def guide_quotes(mid, q, t, T, gamma, sigma, k, tick=1):
    """The guide's code starter, verbatim apart from tick = 1 (prices in ticks)."""
    tau = max(T - t, 0.0)
    reservation = mid - q * gamma * sigma**2 * tau
    spread = gamma * sigma**2 * tau + (2.0 / gamma) * np.log(1.0 + gamma / k)
    bid = np.floor((reservation - spread / 2) / tick) * tick
    ask = np.ceil((reservation + spread / 2) / tick) * tick
    return bid, ask


def test_formulas_by_hand():
    a = AvellanedaStoikov(gamma=1e-3, sigma2=0.5, k=1.5, end_time=1000.0)
    r, d = a.reservation_and_spread(mid=100.5, q=4, t=600.0)
    assert r == pytest.approx(100.5 - 4 * 1e-3 * 0.5 * 400)                      # 99.7
    assert d == pytest.approx(1e-3 * 0.5 * 400 + 2 / 1e-3 * math.log(1 + 1e-3 / 1.5))


@pytest.mark.parametrize("q", [-20, -3, 0, 5, 40])
def test_matches_guide_code_starter(q):
    a = AvellanedaStoikov(gamma=2e-4, sigma2=0.44, k=1.2, end_time=23_400.0)
    r, d = a.reservation_and_spread(mid=10_000.5, q=q, t=3_000.0)
    bid, ask = guide_quotes(10_000.5, q, 3_000.0, 23_400.0, 2e-4, math.sqrt(0.44), 1.2)
    assert (math.floor(r - d / 2), math.ceil(r + d / 2)) == (bid, ask)


def test_long_inventory_shifts_both_quotes_down():
    a = AvellanedaStoikov(gamma=1e-3, sigma2=0.5, k=1.5, tau=600.0)
    r0, d0 = a.reservation_and_spread(100.0, 0, 0.0)
    r_long, d_long = a.reservation_and_spread(100.0, 10, 0.0)
    r_short, _ = a.reservation_and_spread(100.0, -10, 0.0)
    assert r_long < r0 < r_short and d_long == d0     # skew moves the centre, not the width


def test_finite_horizon_skew_fades_at_the_close():
    a = AvellanedaStoikov(gamma=1e-3, sigma2=0.5, k=1.5, end_time=1000.0)
    early, _ = a.reservation_and_spread(100.0, 10, 0.0)
    late, _ = a.reservation_and_spread(100.0, 10, 999.0)
    assert abs(late - 100.0) < abs(early - 100.0) / 100


def test_quotes_never_cross_the_book():
    sim = Simulator(seed=1, warmup=0)
    a = AvellanedaStoikov(gamma=5e-3, sigma2=0.44, k=1.2, tau=3600.0)   # strong skew
    a.inventory = 500                                                   # pretend we are very long
    bid, ask = a.quotes(sim)
    assert bid < sim.book.best_ask() and ask > sim.book.best_bid()


def test_intensity_fit_recovers_known_parameters():
    d = np.array([0.5, 1.5, 2.5, 3.5, 4.5])
    rates = 2.0 * np.exp(-1.3 * d)
    A, k, r2 = fit_intensity(d, rates)
    assert (A, k, r2) == pytest.approx((2.0, 1.3, 1.0))


def test_variance_rate_recovers_diffusion():
    rng = np.random.default_rng(0)
    # 0.5 ticks^2 per second; 10 x 60,000 s gives 10,000 one-minute changes, so the variance
    # estimate has a standard error of about sqrt(2 / 10,000) = 1.4%: allow ~4 standard errors
    mids = [np.cumsum(rng.normal(0, np.sqrt(0.5), 60_000)) for _ in range(10)]
    assert mid_variance_rate(mids, dt=1.0, horizon=60.0) == pytest.approx(0.5, rel=0.06)


def test_as_holds_less_inventory_than_naive_on_same_flow():
    def run(agent):
        sim = Simulator(seed=11, warmup=0)
        agent.attach(sim, start=1.0)
        sim.run(3600)
        return np.array([p[2] for p in agent.path])
    naive = run(NaiveMarketMaker(half_spread=0.5))
    a_s = run(AvellanedaStoikov(gamma=2e-3, sigma2=0.44, k=1.2, tau=3600.0))
    assert a_s.std() < naive.std() / 2


class _Book:
    def __init__(self, bid, ask):
        self.b, self.a = bid, ask

    def mid(self):
        return (self.b + self.a) / 2

    def best_bid(self):
        return self.b

    def best_ask(self):
        return self.a


class _Sim:
    def __init__(self, bid, ask, t=0.0):
        self.book, self.t = _Book(bid, ask), t


def test_rounding_choice_in_a_one_tick_market():
    # optimal half-spread about 0.61 ticks with q = 0: nearest joins the best prices, outward steps back
    kw = dict(gamma=1e-6, sigma2=0.44, k=1.65, tau=3600.0)
    near = AvellanedaStoikov(rounding="nearest", **kw).quotes(_Sim(100, 101))
    out = AvellanedaStoikov(rounding="outward", **kw).quotes(_Sim(100, 101))
    assert near == (100, 101) and out == (99, 102)
