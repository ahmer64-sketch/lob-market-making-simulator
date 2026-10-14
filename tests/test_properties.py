"""Property-based and randomised stress tests: the fast engine must match the naive reference
exactly, and the book's invariants must hold after every event."""
import random

from hypothesis import given, settings, strategies as st

from lob.book import BUY, SELL, OrderBook
from reference import ReferenceBook

op = st.one_of(
    st.tuples(st.just("limit"), st.sampled_from([BUY, SELL]), st.integers(95, 105), st.integers(1, 20),
              st.booleans(), st.booleans()),
    st.tuples(st.just("market"), st.sampled_from([BUY, SELL]), st.integers(1, 40)),
    st.tuples(st.just("cancel"), st.integers(1, 200)),
)


class Ledger:
    """Per-order accounting: every unit submitted must end up filled, resting, cancelled or discarded."""

    def __init__(self):
        self.submitted = self.filled = self.cancelled = self.discarded = 0


def check_invariants(book, ledger):
    bid, ask = book.best_bid(), book.best_ask()
    assert bid is None or ask is None or bid < ask, "book crossed"
    resting = sum(o.qty for o in book.orders.values())
    for side in (BUY, SELL):
        for price, lvl in book.levels[side].items():
            assert lvl, "empty level left in book"
            assert book.level_qty[side][price] == sum(o.qty for o in lvl.values())
            assert all(o.qty > 0 and o.price == price and o.side == side for o in lvl.values())
            seqs = [o.seq for o in lvl.values()]
            assert seqs == sorted(seqs), "FIFO order broken"
    assert ledger.submitted == ledger.filled + resting + ledger.cancelled + ledger.discarded, "quantity not conserved"


def apply(book, ref, ledger, o):
    if o[0] == "limit":
        _, side, price, qty, post_only, ioc = o
        r = book.limit(side, price, qty, post_only=post_only, ioc=ioc)
        rt, rleft = ref.limit(side, price, qty, post_only=post_only, ioc=ioc)
        ledger.submitted += qty
        ledger.discarded += r.unfilled
        assert r.filled + r.resting + r.unfilled == qty
    elif o[0] == "market":
        _, side, qty = o
        r = book.market(side, qty)
        rt, rleft = ref.market(side, qty)
        ledger.submitted += qty
        ledger.discarded += r.unfilled
    else:
        got, exp = book.cancel(o[1]), ref.cancel(o[1])
        assert got == exp
        ledger.cancelled += got
        return
    assert [(t.price, t.qty, t.taker_side, t.maker_id, t.taker_id) for t in r.trades] == rt
    # each traded unit fills one unit of the incoming order AND one unit of a resting order
    ledger.filled += 2 * r.filled


@settings(max_examples=400, deadline=None)
@given(st.lists(op, max_size=120))
def test_engine_matches_reference_and_keeps_invariants(ops):
    book, ref, ledger = OrderBook(), ReferenceBook(), Ledger()
    for o in ops:
        apply(book, ref, ledger, o)
        assert book.snapshot() == ref.snapshot()
        check_invariants(book, ledger)


def test_randomised_stress_100k_events():
    rng = random.Random(42)
    book, ref, ledger = OrderBook(), ReferenceBook(), Ledger()
    live = []
    for i in range(100_000):
        u = rng.random()
        if u < 0.55:
            side = rng.choice([BUY, SELL])
            mid = 10_000
            price = mid - side * rng.randint(-3, 15)     # mostly passive, sometimes marketable
            o = ("limit", side, price, rng.randint(1, 50), rng.random() < 0.1, rng.random() < 0.05)
        elif u < 0.70:
            o = ("market", rng.choice([BUY, SELL]), rng.randint(1, 80))
        else:
            o = ("cancel", rng.choice(live) if live else 1)
        apply(book, ref, ledger, o)
        live = list(book.orders) if i % 50 == 0 else live
        if i % 997 == 0:
            assert book.snapshot() == ref.snapshot()
        check_invariants(book, ledger)
    assert book.snapshot() == ref.snapshot()


def test_randomised_stress_deep_book():
    """Mostly passive orders over a wide price range and frequent cancels: thousands of resting
    orders across many levels, exercising level creation/removal and stale heap entries."""
    rng = random.Random(7)
    book, ref, ledger = OrderBook(), ReferenceBook(), Ledger()
    max_resting = 0
    for i in range(30_000):
        u = rng.random()
        if u < 0.75:
            side = rng.choice([BUY, SELL])
            price = 10_000 - side * rng.randint(-2, 200)
            o = ("limit", side, price, rng.randint(1, 50), rng.random() < 0.2, False)
        elif u < 0.80:
            o = ("market", rng.choice([BUY, SELL]), rng.randint(1, 400))
        else:
            o = ("cancel", rng.randint(1, book._next_id))
        apply(book, ref, ledger, o)
        max_resting = max(max_resting, len(book.orders))
        if i % 499 == 0:
            assert book.snapshot() == ref.snapshot()
            check_invariants(book, ledger)
    assert book.snapshot() == ref.snapshot()
    check_invariants(book, ledger)
    assert max_resting > 1_000 and len(book.levels[BUY]) + len(book.levels[SELL]) > 200
