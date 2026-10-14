"""Informed order flow: traders who know where the price is going.

At Poisson "news" times the fair value jumps to mid ± U{jump_min..jump_max} ticks. While the mid
is at least one tick away from it, informed traders send market orders toward it at Poisson
rate `trade_rate` (size `size`), until the mid reaches it or `max_chase` seconds pass. Anyone
quoting in their way is filled just before the price moves further: adverse selection that
lasts, unlike the purely mechanical kind in the zero-intelligence flow.

Uses its own random stream, so the background flow's random draws are unchanged.
"""
import numpy as np

from .book import BUY, SELL

INFORMED = 2  # owner id


class InformedTraders:
    def __init__(self, news_rate=1 / 120, jump_min=2, jump_max=6, trade_rate=2.0, size=2, max_chase=60.0, seed=0):
        self.news_rate, self.jump_min, self.jump_max = news_rate, jump_min, jump_max
        self.trade_rate, self.size, self.max_chase = trade_rate, size, max_chase
        self.rng = np.random.default_rng(10_000 + seed)
        self.target = None
        self.deadline = None
        self.news = []          # (time, mid at news, target)
        self.orders_sent = 0

    def attach(self, sim, start=0.0):
        sim.schedule(start + self.rng.exponential(1 / self.news_rate), "agent", self._on_news)

    def _on_news(self, sim):
        mid = sim.book.mid()
        if mid is not None:
            jump = int(self.rng.integers(self.jump_min, self.jump_max + 1)) * (1 if self.rng.random() < 0.5 else -1)
            self.target = mid + jump
            self.deadline = sim.t + self.max_chase
            self.news.append((sim.t, mid, self.target))
            sim.schedule(sim.t + self.rng.exponential(1 / self.trade_rate), "agent", self._on_trade)
        sim.schedule(sim.t + self.rng.exponential(1 / self.news_rate), "agent", self._on_news)

    def _on_trade(self, sim):
        if self.target is None or sim.t > self.deadline:
            self.target = None
            return
        mid = sim.book.mid()
        if mid is None or abs(self.target - mid) < 1:
            self.target = None                         # price has caught up
            return
        side = BUY if self.target > mid else SELL
        res = sim.book.market(side, self.size, owner=INFORMED)
        sim._record_trades(res.trades, mid)
        self.orders_sent += 1
        sim.schedule(sim.t + self.rng.exponential(1 / self.trade_rate), "agent", self._on_trade)
