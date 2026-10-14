"""Limit order book with price-time priority.

Prices and quantities are integers (prices in ticks), so comparisons are exact.

Data structures:
  levels[side][price]  -> dict {order_id: Order}. Python dicts keep insertion order, so the
                          first key is the oldest order (FIFO), and deleting any order is O(1).
  level_qty[side][price] -> total resting quantity at that price (kept in step, O(1) depth).
  heaps[side]          -> heap of prices with a level (bids stored negated); emptied levels are
                          dropped lazily when they reach the top. Best price: O(1) amortised.
  orders[order_id]     -> resting Order, for O(1) cancels.

Matching: an incoming order trades against the opposite side, best price first and, within a
price, oldest order first. Trades print at the resting (maker) order's price.
"""
import heapq
from dataclasses import dataclass

BUY, SELL = 1, -1


@dataclass(slots=True)
class Order:
    id: int
    side: int
    price: int
    qty: int          # remaining quantity
    owner: int
    seq: int          # arrival sequence number (time priority)


@dataclass(frozen=True, slots=True)
class Trade:
    seq: int          # sequence number of the incoming (taker) order
    price: int
    qty: int
    taker_side: int
    maker_id: int
    taker_id: int
    maker_owner: int
    taker_owner: int


@dataclass(frozen=True, slots=True)
class Result:
    order_id: int | None   # id if (part of) the order rests in the book, else None
    trades: list
    filled: int
    resting: int           # quantity left resting in the book
    unfilled: int          # quantity discarded (market / immediate-or-cancel remainder, or rejected)
    rejected: bool = False


class OrderBook:
    def __init__(self):
        self.levels = {BUY: {}, SELL: {}}
        self.level_qty = {BUY: {}, SELL: {}}
        self.heaps = {BUY: [], SELL: []}
        self.orders = {}
        self._next_id = 1
        self._seq = 0

    # --- queries ------------------------------------------------------------------------
    def best(self, side):
        heap, levels = self.heaps[side], self.levels[side]
        while heap:
            price = -heap[0] if side == BUY else heap[0]
            if price in levels:
                return price
            heapq.heappop(heap)          # level emptied earlier: drop lazily
        return None

    def best_bid(self):
        return self.best(BUY)

    def best_ask(self):
        return self.best(SELL)

    def spread(self):
        b, a = self.best_bid(), self.best_ask()
        return None if b is None or a is None else a - b

    def mid(self):
        b, a = self.best_bid(), self.best_ask()
        return None if b is None or a is None else (a + b) / 2

    def volume_at(self, side, price):
        return self.level_qty[side].get(price, 0)

    def depth(self, side, n=5):
        """Top n levels of one side as [(price, quantity)], best first."""
        prices = sorted(self.levels[side], reverse=(side == BUY))[:n]
        return [(p, self.level_qty[side][p]) for p in prices]

    def imbalance(self):
        """Qb / (Qb + Qa) at the best prices (0.5 = balanced)."""
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        qb, qa = self.level_qty[BUY][b], self.level_qty[SELL][a]
        return qb / (qb + qa)

    def microprice(self):
        """(Qa * Pb + Qb * Pa) / (Qa + Qb): leans towards the side with less queue."""
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        qb, qa = self.level_qty[BUY][b], self.level_qty[SELL][a]
        return (qa * b + qb * a) / (qa + qb)

    def queue_ahead(self, order_id):
        """Quantity resting ahead of this order at its price (its queue position)."""
        o = self.orders[order_id]
        ahead = 0
        for oid, other in self.levels[o.side][o.price].items():
            if oid == order_id:
                return ahead
            ahead += other.qty
        raise KeyError(order_id)

    def snapshot(self):
        """Full book as ({price: [(order_id, qty), ...]} for bids, same for asks), FIFO order."""
        return tuple({p: [(oid, o.qty) for oid, o in lvl.items()] for p, lvl in self.levels[s].items()}
                     for s in (BUY, SELL))

    # --- order entry ----------------------------------------------------------------------
    def limit(self, side, price, qty, owner=0, post_only=False, ioc=False, front=False):
        """Limit order. post_only: rejected if it would trade on arrival. ioc: any part that
        does not trade immediately is cancelled instead of resting. front: rest at the FRONT of its
        price level instead of the back. Not a real order type; used only to measure how much
        queue position matters (an 'ignore the queue' counterfactual)."""
        self._check(side, qty)
        if not isinstance(price, int):
            raise TypeError("prices are integer ticks")
        self._seq += 1
        oid = self._next_id
        self._next_id += 1
        if post_only and self._crosses(side, price):
            return Result(None, [], 0, 0, qty, rejected=True)
        trades, left = self._match(side, qty, price, oid, owner)
        filled = qty - left
        if left and not ioc:
            self._rest(Order(oid, side, price, left, owner, self._seq), front=front)
            return Result(oid, trades, filled, left, 0)
        return Result(None, trades, filled, 0, left)

    def market(self, side, qty, owner=0):
        """Market order: trades against the book until filled or the book side is empty;
        any remainder is discarded."""
        self._check(side, qty)
        self._seq += 1
        oid = self._next_id
        self._next_id += 1
        trades, left = self._match(side, qty, None, oid, owner)
        return Result(None, trades, qty - left, 0, left)

    def cancel(self, order_id):
        """Remove a resting order. Returns its remaining quantity, or 0 if it is not resting."""
        o = self.orders.pop(order_id, None)
        if o is None:
            return 0
        level = self.levels[o.side][o.price]
        del level[order_id]
        self._reduce_level(o.side, o.price, o.qty)
        return o.qty

    # --- internals ------------------------------------------------------------------------
    @staticmethod
    def _check(side, qty):
        if side not in (BUY, SELL):
            raise ValueError("side must be BUY (1) or SELL (-1)")
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError("quantity must be a positive integer")

    def _crosses(self, side, price):
        opp = self.best(-side)
        return opp is not None and (price >= opp if side == BUY else price <= opp)

    def _match(self, side, qty, limit, taker_id, taker_owner):
        trades = []
        opp = -side
        while qty:
            p = self.best(opp)
            if p is None or (limit is not None and (p > limit if side == BUY else p < limit)):
                break
            level = self.levels[opp][p]
            while qty and level:
                maker_id = next(iter(level))          # oldest order at this price
                maker = level[maker_id]
                fill = min(qty, maker.qty)
                maker.qty -= fill
                qty -= fill
                trades.append(Trade(self._seq, p, fill, side, maker_id, taker_id, maker.owner, taker_owner))
                self._reduce_level(opp, p, fill, drop_empty=False)
                if maker.qty == 0:
                    del level[maker_id]
                    del self.orders[maker_id]
            if not level:
                del self.levels[opp][p]
                del self.level_qty[opp][p]
        return trades, qty

    def _rest(self, o, front=False):
        levels = self.levels[o.side]
        if o.price not in levels:
            levels[o.price] = {}
            self.level_qty[o.side][o.price] = 0
            heapq.heappush(self.heaps[o.side], -o.price if o.side == BUY else o.price)
        if front:
            levels[o.price] = {o.id: o, **levels[o.price]}
        else:
            levels[o.price][o.id] = o
        self.level_qty[o.side][o.price] += o.qty
        self.orders[o.id] = o

    def _reduce_level(self, side, price, qty, drop_empty=True):
        self.level_qty[side][price] -= qty
        if drop_empty and not self.levels[side][price]:
            del self.levels[side][price]
            del self.level_qty[side][price]
