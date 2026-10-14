"""A deliberately naive order book used only to test the real one.

No clever data structures: one flat list of resting orders, re-sorted on every match. Slow but
easy to verify by eye, so agreement with the fast engine on random order streams is strong
evidence the fast engine is right.
"""
BUY, SELL = 1, -1


class ReferenceBook:
    def __init__(self):
        self.resting = []        # [order_id, side, price, qty, seq]
        self.next_id = 1
        self.seq = 0

    def _new(self):
        self.seq += 1
        oid = self.next_id
        self.next_id += 1
        return oid

    def _match(self, side, qty, limit, taker_id):
        trades = []
        while qty:
            book = [o for o in self.resting if o[1] == -side and
                    (limit is None or (o[2] <= limit if side == BUY else o[2] >= limit))]
            if not book:
                break
            # best price first (lowest ask / highest bid), then earliest arrival
            book.sort(key=lambda o: (o[2] if side == BUY else -o[2], o[4]))
            maker = book[0]
            fill = min(qty, maker[3])
            maker[3] -= fill
            qty -= fill
            trades.append((maker[2], fill, side, maker[0], taker_id))
            if maker[3] == 0:
                self.resting.remove(maker)
        return trades, qty

    def limit(self, side, price, qty, post_only=False, ioc=False):
        oid = self._new()
        if post_only:
            opp = [o[2] for o in self.resting if o[1] == -side]
            if opp and (price >= min(opp) if side == BUY else price <= max(opp)):
                return trades_none(qty)
        trades, left = self._match(side, qty, price, oid)
        if left and not ioc:
            self.resting.append([oid, side, price, left, self.seq])
        return trades, left

    def market(self, side, qty):
        oid = self._new()
        return self._match(side, qty, None, oid)

    def cancel(self, order_id):
        for o in self.resting:
            if o[0] == order_id:
                self.resting.remove(o)
                return o[3]
        return 0

    def snapshot(self):
        out = ({}, {})
        for o in sorted(self.resting, key=lambda o: o[4]):
            out[0 if o[1] == BUY else 1].setdefault(o[2], []).append((o[0], o[3]))
        return out


def trades_none(qty):
    return [], qty
