"""The replay's queue and fill logic on tiny hand-made event streams."""
import numpy as np
import pytest

from lob.replay import replay

BASE, WIDTH = 10_000, 200       # prices in cents; book window 10,000..10,199


def stream(events):
    """events: (time_us, kind, side, price_cents, size[, snapshot])"""
    a = np.array([e[:5] for e in events], dtype=float)
    snap = np.array([e[5] if len(e) > 5 else False for e in events], dtype=bool)
    return (a[:, 0].astype(np.int64), a[:, 1].astype(np.int8), a[:, 2].astype(np.int8),
            a[:, 3].astype(np.int64), a[:, 4], snap)


def run(events, queue_model=0, param=0.5, t_end=10_000_000):
    ts, kind, side, price, size, snap = stream(events)
    return replay(ts, kind, side, price, size, snap, BASE, WIDTH, 0, t_end,
                  0, param, 1e-4, 1.0, 1.0, 60.0, 1.0, queue_model, 1_000_000_000, 1_000_000)


BOOK_1C = [(0, 0, 1, 10_100, 5.0), (0, 0, -1, 10_101, 5.0)]   # 100.00 bid x 5 / 100.01 ask x 5


def test_joins_back_of_queue_and_fills_only_after_queue_ahead_trades():
    # we join the bid behind 5 BTC; a 3 BTC sell does not reach us, a further 3 BTC fills 1 of our 1
    f, _ = run(BOOK_1C + [(10, 1, -1, 10_100, 3.0)])
    assert len(f) == 0
    f, _ = run(BOOK_1C + [(10, 1, -1, 10_100, 3.0), (20, 1, -1, 10_100, 3.0)])
    assert len(f) == 1 and f[0, 1] == pytest.approx(1.0) and f[0, 2] == 10_100


def test_trade_through_our_price_fills_in_full():
    f, _ = run(BOOK_1C + [(10, 1, -1, 10_099, 0.1)])     # a sell printed below our bid
    assert len(f) == 1 and f[0, 1] == pytest.approx(1.0)


def test_cancels_move_us_forward_only_in_proportional_model():
    # While we sit at the back of the queue the two models agree (all cancels are "ahead of us").
    # They differ once others queue BEHIND us: 5 ahead of us, 5 more join behind (level 10),
    # then 5 are cancelled (level 5), then a 3 BTC sell.
    ev = BOOK_1C + [(10, 0, 1, 10_100, 10.0), (11, 0, 1, 10_100, 5.0), (20, 1, -1, 10_100, 3.0)]
    pess, _ = run(ev, queue_model=0)
    prop, _ = run(ev, queue_model=1)
    assert len(pess) == 0                              # pessimistic: cancels came from behind, 5 still ahead
    assert prop[0, 1] == pytest.approx(0.5)            # proportional: 5 - 5 * 5/10 = 2.5 ahead -> 0.5 fills


def test_queue_ahead_never_exceeds_level_size():
    # level shrinks 5 -> 1 with no trades: in both models at most 1 can be ahead of us
    ev = BOOK_1C + [(10, 0, 1, 10_100, 1.0), (20, 1, -1, 10_100, 1.5)]
    for m in (0, 1):
        f, _ = run(ev, queue_model=m)
        assert f[0, 1] == pytest.approx(0.5)


def test_book_rebuilds_best_price_after_level_removed_and_snapshot_resets():
    S = 1_000_000  # one second in microseconds (the path is sampled every second)
    ev = [(0, 0, 1, 10_100, 2.0), (0, 0, 1, 10_098, 2.0), (0, 0, -1, 10_102, 2.0),
          (2 * S, 0, 1, 10_100, 0.0),                    # best bid level removed: next best is 10_098
          (3 * S, 1, -1, 10_098, 0.0001)]
    _, path = run(ev)
    assert path[0, 1] == pytest.approx((10_100 + 10_102) / 2)
    assert path[-1, 1] == pytest.approx((10_098 + 10_102) / 2)
    snap = [(0, 0, 1, 10_100, 2.0), (0, 0, -1, 10_101, 2.0),
            (2 * S, 0, 1, 10_050, 1.0, True), (2 * S, 0, -1, 10_150, 1.0, True),   # a new snapshot replaces the book
            (3 * S, 0, 1, 10_051, 1.0)]
    _, p2 = run(snap)
    assert p2[-1, 1] == pytest.approx((10_051 + 10_150) / 2)


def test_trades_reflected_in_book_do_not_double_count_as_cancels():
    # 2 BTC trades at our level, then the book shows the level 5 -> 3: that drop is the trade, not cancels
    ev = BOOK_1C + [(10, 1, -1, 10_100, 2.0), (11, 0, 1, 10_100, 3.0), (20, 1, -1, 10_100, 3.5)]
    prop, _ = run(ev, queue_model=1)
    # ahead: 5 -> 3 after the trade; no cancel adjustment; then 3.5 trades -> 0.5 reaches us
    assert prop[0, 1] == pytest.approx(0.5)


def test_join_touch_quotes_the_best_prices_even_when_the_spread_is_wide():
    ts, kind, side, price, size, snap = stream([(0, 0, 1, 10_100, 5.0), (0, 0, -1, 10_150, 5.0),
                                                (10, 1, 1, 10_150, 6.0)])   # a buyer takes all 5 ahead of us + 1
    f, _ = replay(ts, kind, side, price, size, snap, BASE, WIDTH, 0, 10_000_000,
                  2, 0.0, 1e-4, 1.0, 1.0, 60.0, 1.0, 0, 1_000_000_000, 1_000_000)
    assert len(f) == 1 and f[0, 2] == 10_150 and f[0, 1] == pytest.approx(-1.0)   # our ask at the best ask filled


def test_position_limit_stops_quoting_the_side():
    ev = BOOK_1C + [(10, 1, -1, 10_099, 0.1)]   # trade-through fills our 1-lot bid
    ts, kind, side, price, size, snap = stream(ev + [(2_000_000, 1, -1, 10_099, 0.1)])
    f, _ = replay(ts, kind, side, price, size, snap, BASE, WIDTH, 0, 10_000_000,
                  0, 0.5, 1e-4, 1.0, 1.0, 60.0, 1.0, 0, 100_000, 1_000_000, 1.0)
    assert len(f) == 1                            # at the 1-lot limit we stop bidding, so no second fill
