"""Market-making agents.

MarketMaker handles everything except the quoting rule: a timer every `dt` seconds, post-only
quotes that are left alone (keeping their queue position) when the desired price is unchanged and
cancelled/replaced otherwise, fills taken only from the engine's actual trades, and accounting.
Subclasses implement `quotes(sim) -> (bid_price | None, ask_price | None)` in ticks.

Accounting (prices in ticks, quantities in lots; P&L in tick-lots):
  equity    = cash + inventory * mid
  spread capture   = sum over fills of q * (mid just before the fill - fill price),  q > 0 buy, < 0 sell
  inventory P&L    = equity change - spread capture   (P&L from holding inventory as the mid moves)
  adverse selection at horizon h = sum over fills of q * (mid(t + h) - mid just before the fill),
                     the part of inventory P&L that comes right after fills
"""
import math

import numpy as np

from .book import BUY, SELL


class MarketMaker:
    owner = 1

    def __init__(self, size=1, dt=0.1, record_dt=1.0):
        self.size = size
        self.dt = dt
        self.cash = 0.0
        self.inventory = 0
        self.orders = {BUY: None, SELL: None}      # side -> (order_id, price)
        self.fills = []                            # (time, signed qty, price, mid before)
        self.fill_moved_mid = []                   # per fill: did the mid change on that event?
        self.requotes = 0                          # cancel-and-replace of an existing quote
        self.path = []                             # (time, cash, inventory, mid)
        self.quotes_placed = 0
        self.dist_sum = {BUY: 0.0, SELL: 0.0}      # quoted distance from the mid, summed over timer ticks
        self.dist_n = {BUY: 0, SELL: 0}
        self._trade_cursor = 0
        self._record_dt = record_dt
        self._next_record = 0.0

    # --- hooks ----------------------------------------------------------------------------
    def quotes(self, sim):
        raise NotImplementedError

    def attach(self, sim, start=0.0):
        self._trade_cursor = len(sim.rec.trades)
        self._next_record = start
        sim.agents.append(self)
        sim.schedule(start, "agent", self._on_timer)

    def on_event(self, sim, kind):
        trades = sim.rec.trades
        while self._trade_cursor < len(trades):
            t, price, qty, taker_side, maker_owner, taker_owner, mid_before = trades[self._trade_cursor]
            self._trade_cursor += 1
            if maker_owner == self.owner:
                q = -taker_side * qty                  # we were the resting side
                self.inventory += q
                self.cash -= q * price
                self.fills.append((t, q, price, mid_before))
                self.fill_moved_mid.append(sim.book.mid() != mid_before)
                side = BUY if q > 0 else SELL
                oid = self.orders[side][0] if self.orders[side] else None
                if oid is not None and oid not in sim.book.orders:
                    self.orders[side] = None           # fully filled

    # --- quoting --------------------------------------------------------------------------
    def _on_timer(self, sim):
        self.on_event(sim, "timer")
        bid, ask = self.quotes(sim)
        mid = sim.book.mid()
        for side, price in ((BUY, bid), (SELL, ask)):
            if price is not None and mid is not None:
                self.dist_sum[side] += abs(mid - price)
                self.dist_n[side] += 1
            current = self.orders[side]
            if current is not None and current[0] not in sim.book.orders:
                current = self.orders[side] = None
            if current is not None and current[1] == price:
                continue                               # unchanged: keep queue position
            if current is not None:
                sim.book.cancel(current[0])
                self.orders[side] = None
                self.requotes += 1
            if price is not None:
                res = sim.book.limit(side, int(price), self.size, owner=self.owner, post_only=True)
                self.quotes_placed += 1
                if res.order_id is not None:
                    self.orders[side] = (res.order_id, price)
        while self._next_record <= sim.t:
            self.path.append((self._next_record, self.cash, self.inventory, sim.book.mid()))
            self._next_record += self._record_dt
        sim.schedule(sim.t + self.dt, "agent", self._on_timer)

    # --- results --------------------------------------------------------------------------
    def attribution(self, sim, horizon=10.0):
        """Total P&L split into spread capture, adverse selection (at `horizon`) and other inventory P&L."""
        mid_end = sim.book.mid()
        total = self.cash + self.inventory * mid_end
        times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
        spread = adverse = 0.0
        for t, q, price, mid_before in self.fills:
            spread += q * (mid_before - price)
            mid_later = np.interp(t + horizon, times, mids) if t + horizon <= times[-1] else mid_end
            adverse += q * (mid_later - mid_before)
        inventory = total - spread
        return {"total": total, "spread_capture": spread, "adverse_selection": adverse,
                "other_inventory": inventory - adverse, "inventory": inventory}


class NaiveMarketMaker(MarketMaker):
    """Fixed symmetric quotes: bid at mid - h, ask at mid + h, rounded outward to whole ticks.
    h = 0.5 joins the best bid and ask whenever the spread is one tick."""

    def __init__(self, half_spread=0.5, **kw):
        super().__init__(**kw)
        self.h = half_spread

    def quotes(self, sim):
        mid = sim.book.mid()
        if mid is None:
            return None, None
        return math.floor(mid - self.h), math.ceil(mid + self.h)


class AvellanedaStoikov(MarketMaker):
    """Avellaneda & Stoikov (2008) quotes:

        reservation price  r = s - q * gamma * sigma2 * tau
        total spread       d = gamma * sigma2 * tau + (2 / gamma) * ln(1 + gamma / k)
        bid, ask = r -/+ d/2 rounded to ticks, then kept passive (bid < best ask, ask > best bid)

    rounding="outward" (floor the bid, ceil the ask) is the guide's code starter; "nearest" rounds each
    quote to the nearest tick. In a market whose spread is usually one tick the choice matters: a
    continuous optimal half-spread of ~0.6 ticks becomes 1.5 ticks (one level behind the best price)
    with outward rounding but 0.5 (joining the best price) with nearest rounding.

    s: mid (ticks); q: inventory (lots); sigma2: mid variance per second (ticks^2/s); k: decay of fill
    intensity with distance (1/ticks); gamma: risk aversion (1/tick-lots). tau is the time left to the
    end of the day (T - t) if `end_time` is given, else the constant `tau`.
    """

    def __init__(self, gamma, sigma2, k, end_time=None, tau=None, rounding="nearest", **kw):
        super().__init__(**kw)
        if rounding not in ("nearest", "outward"):
            raise ValueError(rounding)
        self.rounding = rounding
        if (end_time is None) == (tau is None):
            raise ValueError("give exactly one of end_time (finite horizon) or tau (constant horizon)")
        self.gamma, self.sigma2, self.k = gamma, sigma2, k
        self.end_time, self.tau = end_time, tau

    def horizon(self, t):
        return max(self.end_time - t, 0.0) if self.end_time is not None else self.tau

    def reservation_and_spread(self, mid, q, t):
        tau = self.horizon(t)
        r = mid - q * self.gamma * self.sigma2 * tau
        d = self.gamma * self.sigma2 * tau + (2.0 / self.gamma) * math.log1p(self.gamma / self.k)
        return r, d

    def quotes(self, sim):
        mid = sim.book.mid()
        if mid is None:
            return None, None
        r, d = self.reservation_and_spread(mid, self.inventory, sim.t)
        if self.rounding == "outward":
            bid, ask = math.floor(r - d / 2), math.ceil(r + d / 2)
        else:
            bid, ask = math.floor(r - d / 2 + 0.5), math.ceil(r + d / 2 - 0.5)   # nearest tick, ties outward
        best_bid, best_ask = sim.book.best_bid(), sim.book.best_ask()
        bid = min(bid, best_ask - 1) if best_ask is not None else bid
        ask = max(ask, best_bid + 1) if best_bid is not None else ask
        return bid, ask
