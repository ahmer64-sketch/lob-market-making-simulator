"""Market-making agents.

MarketMaker handles everything except the quoting rule: a timer every `dt` seconds, post-only
quotes that are left alone (keeping their queue position) when the desired price is unchanged and
cancelled/replaced otherwise, fills taken only from the engine's actual trades, and accounting.
Subclasses implement `quotes(sim) -> (bid_price | None, ask_price | None)` in ticks.

Optional frictions and controls (all off by default, which reproduces Phases 3-4 exactly):
  latency          seconds between a decision and its cancel/new order reaching the book; while
                   in flight the old quote stays live and can be filled ("picked off")
  maker_rebate     received per lot filled as the resting side (tick-lots per lot)
  taker_fee        paid per lot when we take liquidity (only when flattening)
  max_inventory    stop quoting the side that would push |inventory| beyond this
  kill_drawdown    if equity falls this far below its intraday peak: cancel quotes, stop quoting for
                   the day and work out of the position with market orders of at most
                   `flatten_clip` lots per timer tick (dumping it all at once can empty one side
                   of the book)
  front_of_queue   counterfactual only: our orders jump to the front of their price level

Accounting (prices in ticks, quantities in lots; P&L in tick-lots):
  equity           = cash + inventory * mid   (cash includes fees and rebates)
  spread capture   = sum over fills of q * (mid just before the fill - fill price),  q > 0 buy, < 0 sell
  fees             = rebates received - taker fees paid
  inventory P&L    = equity change - spread capture - fees
  adverse selection at horizon h = sum over fills of q * (mid(t + h) - mid just before the fill),
                     the part of inventory P&L that comes right after fills
"""
import math

import numpy as np

from .book import BUY, SELL


class MarketMaker:
    owner = 1

    def __init__(self, size=1, dt=0.1, record_dt=1.0, latency=0.0, maker_rebate=0.0, taker_fee=0.0,
                 max_inventory=None, kill_drawdown=None, front_of_queue=False, flatten_clip=5):
        self.size = size
        self.dt = dt
        self.latency = latency
        self.maker_rebate, self.taker_fee = maker_rebate, taker_fee
        self.max_inventory, self.kill_drawdown = max_inventory, kill_drawdown
        self.front_of_queue = front_of_queue
        self.flatten_clip = flatten_clip
        self.cash = 0.0
        self.fees = 0.0
        self.inventory = 0
        self.orders = {BUY: None, SELL: None}      # side -> (order_id, price)
        self.pending = {BUY: False, SELL: False}   # an order action is in flight (latency)
        self.fills = []                            # (time, signed qty, price, mid before)
        self.fill_moved_mid = []                   # per fill: did the mid change on that event?
        self.fill_is_taker = []
        self.requotes = 0                          # cancel-and-replace of an existing quote
        self.path = []                             # (time, cash, inventory, mid)
        self.quotes_placed = 0
        self.dist_sum = {BUY: 0.0, SELL: 0.0}      # quoted distance from the mid, summed over timer ticks
        self.dist_n = {BUY: 0, SELL: 0}
        self.killed_at = None
        self._peak = 0.0
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
                fee = self.maker_rebate * qty
            elif taker_owner == self.owner:
                q = taker_side * qty                   # we took liquidity (flattening)
                fee = -self.taker_fee * qty
            else:
                continue
            self.inventory += q
            self.cash += fee - q * price
            self.fees += fee
            self.fills.append((t, q, price, mid_before))
            self.fill_moved_mid.append(sim.book.mid() != mid_before)
            self.fill_is_taker.append(taker_owner == self.owner)
            for side in (BUY, SELL):
                o = self.orders[side]
                if o is not None and o[0] not in sim.book.orders:
                    self.orders[side] = None           # fully filled

    # --- quoting --------------------------------------------------------------------------
    def _on_timer(self, sim):
        self.on_event(sim, "timer")
        self._check_kill(sim)
        if self.killed_at is not None and self.inventory and not self.pending[BUY] and not self.pending[SELL]:
            if self.latency > 0:                       # keep working out of the position
                self.pending[BUY] = self.pending[SELL] = True
                sim.schedule(sim.t + self.latency, "agent", self._flatten)
            else:
                self._flatten(sim)
        bid, ask = (None, None) if self.killed_at is not None else self.quotes(sim)
        if self.max_inventory is not None:
            if self.inventory + self.size > self.max_inventory:
                bid = None
            if self.inventory - self.size < -self.max_inventory:
                ask = None
        mid = sim.book.mid()
        for side, price in ((BUY, bid), (SELL, ask)):
            if price is not None and mid is not None:
                self.dist_sum[side] += abs(mid - price)
                self.dist_n[side] += 1
            if self.pending[side]:
                continue                               # previous action still in flight
            current = self.orders[side]
            if current is not None and current[0] not in sim.book.orders:
                current = self.orders[side] = None
            if current is not None and current[1] == price:
                continue                               # unchanged: keep queue position
            if current is None and price is None:
                continue
            if self.latency > 0:
                self.pending[side] = True
                sim.schedule(sim.t + self.latency, "agent",
                             lambda s, side=side, old=current, price=price: self._act(s, side, old, price))
            else:
                self._act(sim, side, current, price)
        while self._next_record <= sim.t:
            self.path.append((self._next_record, self.cash, self.inventory, sim.book.mid()))
            self._next_record += self._record_dt
        sim.schedule(sim.t + self.dt, "agent", self._on_timer)

    def _act(self, sim, side, old, price):
        """Cancel `old` (if any) and place a new quote at `price` (if any): runs at decision time
        plus latency."""
        if old is not None:
            sim.book.cancel(old[0])                    # returns 0 if it was filled in the meantime
            self.orders[side] = None
            self.requotes += 1
        if price is not None:
            res = sim.book.limit(side, int(price), self.size, owner=self.owner, post_only=True,
                                 front=self.front_of_queue)
            self.quotes_placed += 1
            if res.order_id is not None:
                self.orders[side] = (res.order_id, price)
        self.pending[side] = False

    def _check_kill(self, sim):
        if self.kill_drawdown is None or self.killed_at is not None:
            return
        mid = sim.book.mid()
        if mid is None:
            return
        equity = self.cash + self.inventory * mid
        self._peak = max(self._peak, equity)
        if self._peak - equity > self.kill_drawdown:
            self.killed_at = sim.t
            self.inventory_at_kill = self.inventory

    def _flatten(self, sim):
        """Cancel our quotes and trade up to `flatten_clip` lots towards zero inventory."""
        for side in (BUY, SELL):
            if self.orders[side] is not None:
                sim.book.cancel(self.orders[side][0])
                self.orders[side] = None
        self.on_event(sim, "flatten")                  # book any fills that landed in flight
        if self.inventory:
            side = SELL if self.inventory > 0 else BUY
            mid_before = sim.book.mid()
            res = sim.book.market(side, min(abs(self.inventory), self.flatten_clip), owner=self.owner)
            sim._record_trades(res.trades, mid_before)
            self.on_event(sim, "flatten")
        self.pending[BUY] = self.pending[SELL] = False

    # --- results --------------------------------------------------------------------------
    def attribution(self, sim, horizon=10.0):
        """Total P&L split into spread capture, fees, adverse selection (at `horizon`) and other
        inventory P&L."""
        mid_end = sim.book.mid()
        total = self.cash + self.inventory * mid_end
        times, mids = np.array(sim.rec.times), (np.array(sim.rec.bid) + np.array(sim.rec.ask)) / 2
        spread = adverse = 0.0
        for t, q, price, mid_before in self.fills:
            spread += q * (mid_before - price)
            mid_later = np.interp(t + horizon, times, mids) if t + horizon <= times[-1] else mid_end
            adverse += q * (mid_later - mid_before)
        inventory = total - spread - self.fees
        return {"total": total, "spread_capture": spread, "fees": self.fees, "adverse_selection": adverse,
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
