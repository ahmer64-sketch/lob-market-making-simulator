import pytest

from lob.book import BUY, SELL, OrderBook


def test_resting_orders_and_top_of_book():
    b = OrderBook()
    b.limit(BUY, 99, 10)
    b.limit(BUY, 100, 5)
    b.limit(SELL, 102, 7)
    b.limit(SELL, 101, 3)
    assert (b.best_bid(), b.best_ask(), b.spread(), b.mid()) == (100, 101, 1, 100.5)
    assert b.depth(BUY) == [(100, 5), (99, 10)] and b.depth(SELL) == [(101, 3), (102, 7)]


def test_marketable_limit_trades_at_maker_price_and_rests_remainder():
    b = OrderBook()
    b.limit(SELL, 101, 3)
    r = b.limit(BUY, 105, 5)
    assert [(t.price, t.qty) for t in r.trades] == [(101, 3)]
    assert r.filled == 3 and r.resting == 2 and b.best_bid() == 105


def test_price_priority_sweeps_levels_in_order():
    b = OrderBook()
    for p, q in [(103, 2), (101, 2), (102, 2)]:
        b.limit(SELL, p, q)
    r = b.market(BUY, 5)
    assert [(t.price, t.qty) for t in r.trades] == [(101, 2), (102, 2), (103, 1)]
    assert b.depth(SELL) == [(103, 1)]


def test_fifo_within_a_price_level():
    b = OrderBook()
    first = b.limit(SELL, 101, 4, owner=1).order_id
    second = b.limit(SELL, 101, 4, owner=2).order_id
    r = b.market(BUY, 6)
    assert [(t.maker_id, t.qty) for t in r.trades] == [(first, 4), (second, 2)]
    assert b.queue_ahead(second) == 0 and b.volume_at(SELL, 101) == 2


def test_partial_fill_keeps_time_priority():
    b = OrderBook()
    a = b.limit(BUY, 100, 10).order_id
    c = b.limit(BUY, 100, 5).order_id
    b.market(SELL, 4)                      # a partly filled, still first in line
    assert b.queue_ahead(c) == 6
    r = b.market(SELL, 7)
    assert [(t.maker_id, t.qty) for t in r.trades] == [(a, 6), (c, 1)]


def test_market_order_larger_than_book_discards_remainder():
    b = OrderBook()
    b.limit(SELL, 101, 3)
    r = b.market(BUY, 10)
    assert r.filled == 3 and r.unfilled == 7 and b.best_ask() is None


def test_cancel_is_exact_and_idempotent():
    b = OrderBook()
    a = b.limit(BUY, 100, 5).order_id
    c = b.limit(BUY, 100, 3).order_id
    assert b.cancel(a) == 5 and b.cancel(a) == 0
    assert b.queue_ahead(c) == 0 and b.volume_at(BUY, 100) == 3
    assert b.cancel(c) == 3 and b.best_bid() is None


def test_cancel_after_full_fill_returns_zero():
    b = OrderBook()
    a = b.limit(SELL, 101, 2).order_id
    b.market(BUY, 2)
    assert b.cancel(a) == 0


def test_post_only_rejected_if_it_would_cross():
    b = OrderBook()
    b.limit(SELL, 101, 5)
    r = b.limit(BUY, 101, 2, post_only=True)
    assert r.rejected and r.trades == [] and b.best_bid() is None
    ok = b.limit(BUY, 100, 2, post_only=True)
    assert not ok.rejected and b.best_bid() == 100


def test_ioc_never_rests():
    b = OrderBook()
    b.limit(SELL, 101, 2)
    r = b.limit(BUY, 102, 5, ioc=True)
    assert r.filled == 2 and r.unfilled == 3 and r.order_id is None and b.best_bid() is None


def test_imbalance_and_microprice():
    b = OrderBook()
    b.limit(BUY, 100, 30)
    b.limit(SELL, 102, 10)
    assert b.imbalance() == pytest.approx(0.75)
    # more queue on the bid -> fair value leans toward the ask
    assert b.microprice() == pytest.approx((10 * 100 + 30 * 102) / 40)


def test_bad_inputs_rejected():
    b = OrderBook()
    with pytest.raises(TypeError):
        b.limit(BUY, 100.5, 1)
    with pytest.raises(ValueError):
        b.limit(BUY, 100, 0)
    with pytest.raises(ValueError):
        b.market(2, 1)


def test_emptied_levels_do_not_come_back_as_best_price():
    b = OrderBook()
    a = b.limit(BUY, 105, 1).order_id
    b.limit(BUY, 100, 1)
    b.cancel(a)
    assert b.best_bid() == 100
    b.limit(BUY, 105, 2)             # level re-created later
    assert b.best_bid() == 105 and b.volume_at(BUY, 105) == 2


def test_front_of_queue_counterfactual():
    b = OrderBook()
    a = b.limit(BUY, 100, 5).order_id
    f = b.limit(BUY, 100, 2, front=True).order_id
    c = b.limit(BUY, 100, 3).order_id
    assert b.queue_ahead(f) == 0 and b.queue_ahead(a) == 2 and b.queue_ahead(c) == 7
    r = b.market(SELL, 4)
    assert [(t.maker_id, t.qty) for t in r.trades] == [(f, 2), (a, 2)]
