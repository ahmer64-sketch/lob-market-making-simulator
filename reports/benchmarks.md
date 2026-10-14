# Benchmarks

Python 3.13.2 on arm64 (Darwin), single core, best of several runs.

| Engine | What is counted | Events | Seconds | **Events / second** |
|---|---|---|---|---|
| Matching engine (Phase 1), pure Python | limit / market / cancel orders | 300,000 | 0.55 | **542,951** |
| Order-flow simulator (Phase 2), pure Python | events incl. flow generation | 142,304 | 0.99 | **143,862** |
| Real-data replay (Phase 6), Numba | book updates + trades, full day | 35,206,507 | 0.75 | **47,088,848** |
| Same replay loop, plain Python (first 1M events) | book updates + trades | 1,000,000 | 2.73 | **366,047** |

Compiling the replay loop with Numba makes it **201× faster** than the same code run as plain Python.

## How the matching engine could go faster

The Phase 1 engine favours clarity: Python dicts per price level (FIFO with O(1) cancel), a heap per side for the best price, an order-id map. The replay row shows the payoff of the next steps:
- **Arrays instead of objects:** price levels as a dense array over a price window (as the replay does), orders in preallocated arrays indexed by id; no per-order Python objects.
- **Compile the hot loop:** Numba (shown above) or Cython; the same logic runs one to two orders of magnitude faster.
- **Memory layout:** intrusive linked lists per level stored in arrays, so cancels stay O(1) without dict overhead and data stays cache-friendly.
- **Language:** C++ or Rust for production latency, where predictable memory and no garbage collection matter more than raw throughput.
