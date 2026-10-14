# Limit Order Book Market-Making Simulator

A price-time-priority matching engine, synthetic order flow and market-making agents (naive and Avellaneda-Stoikov), with P&L split into spread capture, inventory and adverse selection.

## Progress
- [x] Phase 1: Matching engine: [report](reports/phase1_engine.md)
- [x] Phase 2: Synthetic order flow: [report](reports/phase2_flow.md)
- [ ] Phase 3: Naive market maker
- [ ] Phase 4: Inventory-aware (Avellaneda-Stoikov) quotes
- [ ] Phase 5: Realism (latency, queue position, fees, informed flow, limits)
- [ ] Phase 6: Replay real data (stretch)
- [ ] Phase 7: Package

## Reproduce
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
.venv/bin/pytest -q
.venv/bin/python scripts/run_phase1.py
.venv/bin/python scripts/run_phase2.py
```
