"""Event-driven simulation: an event loop around the order book plus zero-intelligence
background order flow in the style of Cont, Stoikov & Talreja (2010).

Background flow (per side, Poisson):
  - limit orders: a buy is placed i ticks below the best ask (a sell i ticks above the best bid),
    i = 1..K, at rate lam[i-1] = k / i**alpha, so most land near the top of the book; i below the
    current spread improves the best price;
  - market orders at rate mu;
  - each resting background order gets its own exponential cancellation clock with rate
    theta[i-1], set by its distance at placement;
  - sizes ~ 1 + Geometric, mean `size_mean` lots.

The event loop is a priority queue keyed by (time, sequence). Agents (Phase 3+) hook in through
`on_event`; this module only knows about the book and the background flow.
"""
import heapq
from dataclasses import dataclass, field

import numpy as np

from .book import BUY, SELL, OrderBook

BACKGROUND = 0  # owner id of background orders


@dataclass(frozen=True)
class FlowParams:
    K: int = 10                   # price levels (distance from the opposite best) for limit orders
    k: float = 1.92               # limit arrival rate at distance 1, per side, per second
    alpha: float = 0.52           # power-law decay of limit arrivals with distance
    mu: float = 1.0               # market orders per side per second
    theta0: float = 0.6           # cancellation rate (per order per second) at distance 1
    theta_decay: float = 0.4      # theta(i) = theta0 / i**theta_decay
    size_mean: float = 2.0        # mean order size in lots
    p0: int = 10_000              # initial mid-price reference, ticks

    @property
    def lam(self):
        return self.k / np.arange(1, self.K + 1) ** self.alpha

    @property
    def theta(self):
        return self.theta0 / np.arange(1, self.K + 1) ** self.theta_decay


@dataclass
class Recorder:
    """Book state sampled on a fixed time grid plus every trade."""
    dt: float = 1.0
    times: list = field(default_factory=list)
    bid: list = field(default_factory=list)
    ask: list = field(default_factory=list)
    bid_qty: list = field(default_factory=list)
    ask_qty: list = field(default_factory=list)
    depth: list = field(default_factory=list)   # volume at distance 1..K from the mid (both sides summed)
    trades: list = field(default_factory=list)  # (time, price, qty, taker_side, maker_owner, taker_owner)


class Simulator:
    def __init__(self, params=FlowParams(), seed=0, record_dt=1.0, warmup=60.0):
        self.p = params
        self.rng = np.random.default_rng(seed)
        self.book = OrderBook()
        self.t = 0.0
        self._events = []
        self._seq = 0
        self.rec = Recorder(dt=record_dt)
        self._next_sample = warmup
        self.agents = []
        self.counts = {"limit": 0, "market": 0, "cancel": 0, "cancel_effective": 0}
        self.limit_distance_counts = np.zeros(params.K, dtype=int)
        self.placed = {}  # order id -> (time, distance) for background orders that rested
        self.lifetimes = []  # (time resting before cancel, distance at placement)
        self.ref_price = params.p0
        lam = params.lam
        self._limit_probs = lam / lam.sum()
        self._rate_limit, self._rate_market = 2 * lam.sum(), 2 * params.mu
        self._seed_book()
        self.schedule(self._draw_wait(), "arrival", None)

    # --- event loop ---------------------------------------------------------------------
    def schedule(self, time, kind, payload):
        self._seq += 1
        heapq.heappush(self._events, (time, self._seq, kind, payload))

    def run(self, until):
        events = self._events
        while events and events[0][0] <= until:
            time, _, kind, payload = heapq.heappop(events)
            self._sample_until(time)
            self.t = time
            if kind == "arrival":
                self._arrival()
                self.schedule(self.t + self._draw_wait(), "arrival", None)
            elif kind == "cancel":
                self.counts["cancel"] += 1
                removed = self.book.cancel(payload) > 0
                self.counts["cancel_effective"] += removed
                if removed and payload in self.placed:
                    self.lifetimes.append((self.t - self.placed[payload][0], self.placed[payload][1]))
                self.placed.pop(payload, None)
            else:  # agent-scheduled event
                payload(self)
            for agent in self.agents:
                agent.on_event(self, kind)
        self._sample_until(until)
        self.t = until

    # --- background flow ------------------------------------------------------------------
    def _draw_wait(self):
        return self.rng.exponential(1.0 / (self._rate_limit + self._rate_market))

    def _size(self):
        """Lots: geometric on 1, 2, 3, ... with the given mean."""
        return int(self.rng.geometric(1.0 / self.p.size_mean)) if self.p.size_mean > 1 else 1

    def _arrival(self):
        u = self.rng.random() * (self._rate_limit + self._rate_market)
        side = BUY if self.rng.random() < 0.5 else SELL
        if u < self._rate_market:
            self.counts["market"] += 1
            res = self.book.market(side, self._size(), owner=BACKGROUND)
            self._record_trades(res.trades)
            return
        i = int(self.rng.choice(self.p.K, p=self._limit_probs)) + 1
        self.place_background_limit(side, i)

    def place_background_limit(self, side, i):
        opp = self.book.best(-side)
        anchor = opp if opp is not None else self.ref_price + (1 if side == BUY else -1)
        price = anchor - i if side == BUY else anchor + i
        res = self.book.limit(side, int(price), self._size(), owner=BACKGROUND)
        self.counts["limit"] += 1
        self.limit_distance_counts[i - 1] += 1
        self._record_trades(res.trades)
        if res.order_id is not None:
            self.placed[res.order_id] = (self.t, i)
            self.schedule(self.t + self.rng.exponential(1.0 / self.p.theta[i - 1]), "cancel", res.order_id)

    def _seed_book(self):
        """Start from the stationary-ish depth lam/theta at each distance on both sides."""
        mean_depth = self.p.lam / self.p.theta
        for i in range(1, self.p.K + 1):
            for side in (BUY, SELL):
                for _ in range(max(1, int(round(mean_depth[i - 1])))):
                    price = self.p.p0 - i if side == BUY else self.p.p0 + i
                    res = self.book.limit(side, price, self._size(), owner=BACKGROUND)
                    self.schedule(self.rng.exponential(1.0 / self.p.theta[i - 1]), "cancel", res.order_id)

    # --- recording ----------------------------------------------------------------------
    def _record_trades(self, trades):
        for tr in trades:
            self.rec.trades.append((self.t, tr.price, tr.qty, tr.taker_side, tr.maker_owner, tr.taker_owner))
        if trades:
            self.ref_price = trades[-1].price

    def _sample_until(self, time):
        r = self.rec
        while self._next_sample <= time:
            b, a = self.book.best_bid(), self.book.best_ask()
            r.times.append(self._next_sample)
            r.bid.append(np.nan if b is None else b)
            r.ask.append(np.nan if a is None else a)
            r.bid_qty.append(0 if b is None else self.book.volume_at(BUY, b))
            r.ask_qty.append(0 if a is None else self.book.volume_at(SELL, a))
            if b is not None and a is not None:
                mid = (a + b) / 2
                prof = np.zeros(self.p.K)
                for side, levels in ((BUY, self.book.level_qty[BUY]), (SELL, self.book.level_qty[SELL])):
                    for price, q in levels.items():
                        d = int(np.ceil(abs(price - mid)))
                        if 1 <= d <= self.p.K:
                            prof[d - 1] += q / 2
                r.depth.append(prof)
            self._next_sample += r.dt
