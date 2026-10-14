"""Replay a real day of order-book data (Tardis.dev price-level updates + trades) with virtual
market-making quotes.

Our quotes never enter the recorded book, so the replay cannot show our own market impact: the
book and trades are what actually happened. Price-level data does not say where in a queue each
order sits, so our queue position is estimated with one of two standard models:

  queue_model = 0  "pessimistic": we join the back of the level; only trades at our price move us
                   forward. A shrinking level can still cap the quantity ahead of us (it cannot
                   exceed the level size), but cancels are otherwise assumed to come from behind us.
  queue_model = 1  "proportional": level decreases not explained by trades are cancels spread
                   evenly over the queue, so they also move us forward in proportion.

A trade at our price fills us only once the quantity ahead of us is used up; a trade at a price
worse than ours (a sell below our bid) means the price traded through us and fills us in full.

Prices are integer cents; sizes are BTC. All the work happens in one Numba-compiled loop.
"""
import gzip
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

BOOK, TRADE = 0, 1


# --- preprocessing --------------------------------------------------------------------------
def load_day(book_csv_gz, trades_csv_gz, cache=None):
    """Merge book updates and trades into one time-ordered event stream (cached as .npz).

    Returns dict of arrays: ts (microseconds), kind (0 book, 1 trade), side (+1 bid / buyer,
    -1 ask / seller), price (cents), size (BTC), snapshot (bool, book rows only)."""
    if cache is not None and Path(cache).exists():
        return dict(np.load(cache))
    cols = ["timestamp", "is_snapshot", "side", "price", "amount"]
    parts = []
    for chunk in pd.read_csv(book_csv_gz, usecols=cols, chunksize=5_000_000,
                             dtype={"timestamp": np.int64, "side": "category", "price": np.float64,
                                    "amount": np.float64, "is_snapshot": bool}):
        parts.append(pd.DataFrame({
            "ts": chunk["timestamp"].to_numpy(np.int64),
            "kind": np.zeros(len(chunk), np.int8),
            "side": np.where(chunk["side"].astype(str).to_numpy() == "bid", 1, -1).astype(np.int8),
            "price": np.rint(chunk["price"].to_numpy() * 100).astype(np.int64),
            "size": chunk["amount"].to_numpy(np.float64),
            "snapshot": chunk["is_snapshot"].to_numpy(bool)}))
    tr = pd.read_csv(trades_csv_gz, usecols=["timestamp", "side", "price", "amount"])
    parts.append(pd.DataFrame({
        "ts": tr["timestamp"].to_numpy(np.int64),
        "kind": np.ones(len(tr), np.int8),
        "side": np.where(tr["side"].to_numpy() == "buy", 1, -1).astype(np.int8),   # aggressor side
        "price": np.rint(tr["price"].to_numpy() * 100).astype(np.int64),
        "size": tr["amount"].to_numpy(np.float64),
        "snapshot": np.zeros(len(tr), bool)}))
    ev = pd.concat(parts, ignore_index=True)
    # time order; at equal timestamps trades first (the book update that reflects a trade follows it)
    order = np.lexsort((-ev["kind"].to_numpy(), ev["ts"].to_numpy()))
    out = {c: ev[c].to_numpy()[order] for c in ev.columns}
    if cache is not None:
        np.savez(cache, **out)
    return out


# --- replay ---------------------------------------------------------------------------------------
@njit(cache=True)
def _best_bid(bid, start):
    i = start
    while i >= 0 and bid[i] <= 0.0:
        i -= 1
    return i


@njit(cache=True)
def _best_ask(ask, start):
    i = start
    n = len(ask)
    while i < n and ask[i] <= 0.0:
        i += 1
    return i


@njit(cache=True)
def replay(ts, kind, side, price, size, snapshot, base, width, t_start, t_end,
           strategy, param, gamma, sigma2, k, tau, qsize, queue_model, dt_us, sample_us, max_lots=np.inf):
    """Run one virtual market maker over the event stream.

    strategy 0: fixed offset `param` cents from the mid, rounded outward to whole cents
                (param = 0.5 joins the best prices when the spread is one cent)
    strategy 2: join the best bid and best ask
    strategy 1: Avellaneda-Stoikov with inventory in lots of `qsize` BTC, sigma2 in cents^2/s,
                k per cent, constant horizon tau seconds; quotes rounded to the nearest cent and
                kept passive.
    Quotes are re-evaluated every dt_us microseconds; an unchanged quote keeps its queue place.
    max_lots: position limit; the side that would push |inventory| / qsize beyond it is not quoted.

    Returns (fills[n, 5]: ts, signed BTC, price cents, mid before, our side;
             path[m, 4]: ts, mid, inventory BTC, cash cents)."""
    bid = np.zeros(width)
    ask = np.zeros(width)
    trd_bid = np.zeros(width)     # traded at each level since its last book update
    trd_ask = np.zeros(width)
    bb, ba = -1, width            # best bid / ask indices (relative to base)
    in_snapshot = False

    q_px = np.array([-1, -1])     # our bid / ask price index (-1 = none)
    q_ahead = np.zeros(2)
    q_left = np.zeros(2)

    inv = 0.0
    cash = 0.0
    fills = np.zeros((200_000, 5))
    nf = 0
    path = np.zeros((int((t_end - t_start) / sample_us) + 2, 4))
    npth = 0
    next_quote = t_start
    next_sample = t_start

    for e in range(len(ts)):
        t = ts[e]
        if t > t_end:
            break
        p = price[e] - base
        if p < 0 or p >= width:
            continue
        mid_before = (bb + ba) / 2.0 + base if (bb >= 0 and ba < width) else np.nan

        if kind[e] == 0:                                   # --- book update: new total size at p
            if snapshot[e] and not in_snapshot:            # a snapshot block starts: rebuild from scratch
                bid[:] = 0.0
                ask[:] = 0.0
                trd_bid[:] = 0.0
                trd_ask[:] = 0.0
                bb, ba = -1, width
            in_snapshot = snapshot[e]
            new = size[e]
            if side[e] == 1:
                old = bid[p]
                bid[p] = new
                if t >= t_start and q_px[0] == p and new < old:
                    cancelled = max(old - new - trd_bid[p], 0.0)
                    if queue_model == 1 and old > 0:
                        q_ahead[0] -= cancelled * q_ahead[0] / old
                    q_ahead[0] = max(min(q_ahead[0], new), 0.0)
                trd_bid[p] = 0.0
                if new > 0 and p > bb:
                    bb = p
                elif new <= 0 and p == bb:
                    bb = _best_bid(bid, p)
            else:
                old = ask[p]
                ask[p] = new
                if t >= t_start and q_px[1] == p and new < old:
                    cancelled = max(old - new - trd_ask[p], 0.0)
                    if queue_model == 1 and old > 0:
                        q_ahead[1] -= cancelled * q_ahead[1] / old
                    q_ahead[1] = max(min(q_ahead[1], new), 0.0)
                trd_ask[p] = 0.0
                if new > 0 and p < ba:
                    ba = p
                elif new <= 0 and p == ba:
                    ba = _best_ask(ask, p)
        else:                                             # --- trade
            q = size[e]
            if side[e] == -1:                              # a seller hit bids
                trd_bid[p] += q
                j = 0
            else:                                          # a buyer lifted asks
                trd_ask[p] += q
                j = 1
            if t >= t_start and q_px[j] >= 0 and q_left[j] > 0:
                through = (p < q_px[j]) if j == 0 else (p > q_px[j])
                at = p == q_px[j]
                fill = 0.0
                if through:
                    fill = q_left[j]
                elif at:
                    if q > q_ahead[j]:
                        fill = min(q_left[j], q - q_ahead[j])
                        q_ahead[j] = 0.0
                    else:
                        q_ahead[j] -= q
                if fill > 0:
                    signed = fill if j == 0 else -fill
                    px = q_px[j] + base
                    inv += signed
                    cash -= signed * px
                    q_left[j] -= fill
                    if nf < fills.shape[0]:
                        fills[nf, 0] = t
                        fills[nf, 1] = signed
                        fills[nf, 2] = px
                        fills[nf, 3] = mid_before
                        fills[nf, 4] = 1.0 if j == 0 else -1.0
                        nf += 1
                    if q_left[j] <= 1e-12:
                        q_px[j] = -1

        if t < t_start or bb < 0 or ba >= width or bb >= ba:
            continue
        mid = (bb + ba) / 2.0
        while next_sample <= t and npth < path.shape[0]:
            path[npth, 0] = next_sample
            path[npth, 1] = mid + base
            path[npth, 2] = inv
            path[npth, 3] = cash
            npth += 1
            next_sample += sample_us
        if t >= next_quote:                                # --- re-quote
            next_quote = t + dt_us
            if strategy == 0:
                want_b = int(np.floor(mid - param))
                want_a = int(np.ceil(mid + param))
            elif strategy == 2:
                want_b = bb
                want_a = ba
            else:
                lots = inv / qsize
                r = mid - lots * gamma * sigma2 * tau
                d = gamma * sigma2 * tau + (2.0 / gamma) * np.log1p(gamma / k)
                want_b = int(np.floor(r - d / 2 + 0.5))
                want_a = int(np.ceil(r + d / 2 - 0.5))
                want_b = min(want_b, ba - 1)
                want_a = max(want_a, bb + 1)
            lots_now = inv / qsize
            for j in range(2):
                want = want_b if j == 0 else want_a
                if (j == 0 and lots_now + 1 > max_lots + 1e-9) or (j == 1 and lots_now - 1 < -max_lots - 1e-9):
                    q_px[j] = -1                           # position limit: do not quote this side
                    q_left[j] = 0.0
                    continue
                if want == q_px[j] and q_left[j] > 0:
                    continue                               # unchanged: keep queue place
                if want < 0 or want >= width:
                    q_px[j] = -1
                    continue
                q_px[j] = want
                q_left[j] = qsize
                if j == 0:
                    q_ahead[j] = bid[want] if want <= bb else 0.0   # inside the spread: nobody ahead
                else:
                    q_ahead[j] = ask[want] if want >= ba else 0.0
    return fills[:nf], path[:npth]


@njit(cache=True)
def book_stats(ts, kind, side, price, size, snapshot, base, width, t_start, t_end, sample_us):
    """Sample the reconstructed top of book every sample_us: (ts, best bid, best ask, bid size,
    ask size) and count events where the reconstructed book is crossed or one side is empty."""
    bid = np.zeros(width)
    ask = np.zeros(width)
    bb, ba = -1, width
    in_snapshot = False
    out = np.zeros((int((t_end - t_start) / sample_us) + 2, 5))
    n = 0
    crossed = 0
    checked = 0
    nxt = t_start
    for e in range(len(ts)):
        t = ts[e]
        if t > t_end:
            break
        p = price[e] - base
        if kind[e] == 0 and 0 <= p < width:
            if snapshot[e] and not in_snapshot:
                bid[:] = 0.0
                ask[:] = 0.0
                bb, ba = -1, width
            in_snapshot = snapshot[e]
            if side[e] == 1:
                bid[p] = size[e]
                if size[e] > 0 and p > bb:
                    bb = p
                elif size[e] <= 0 and p == bb:
                    bb = _best_bid(bid, p)
            else:
                ask[p] = size[e]
                if size[e] > 0 and p < ba:
                    ba = p
                elif size[e] <= 0 and p == ba:
                    ba = _best_ask(ask, p)
        if t < t_start:
            continue
        checked += 1
        if bb < 0 or ba >= width or bb >= ba:
            crossed += 1
            continue
        while nxt <= t and n < out.shape[0]:
            out[n, 0] = nxt
            out[n, 1] = bb + base
            out[n, 2] = ba + base
            out[n, 3] = bid[bb]
            out[n, 4] = ask[ba]
            n += 1
            nxt += sample_us
    return out[:n], crossed, checked
