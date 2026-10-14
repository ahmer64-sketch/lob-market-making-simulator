# One command reproduces every report and figure:
#     make all        tests + simulation phases 1-5 + real-data replay (downloads ~400 MB the first time)
#     make sim        tests + simulation phases 1-5 only (offline, ~15 min)
#     make fetch-real download and verify the real data for Phase 6

PY  := .venv/bin/python
EXT := data/external
BOOK   := $(EXT)/incremental_book_L2_BTC-USD_2024-01-01.csv.gz
TRADES := $(EXT)/trades_BTC-USD_2024-01-01.csv.gz
BOOK_URL   := https://datasets.tardis.dev/v1/coinbase/incremental_book_L2/2024/01/01/BTC-USD.csv.gz
TRADES_URL := https://datasets.tardis.dev/v1/coinbase/trades/2024/01/01/BTC-USD.csv.gz
BOOK_SHA   := 49618c2885cd6aa9a48fb7b92b45aee72ed8a689eb5ee7e9d1a2ef88c13dfb37
TRADES_SHA := 7a95f20fc40d5a6c21974df5115584e7f109ca5828a908d998d62588a2bebd4c

.PHONY: all sim setup test phase1 phase2 phase3 phase4 phase5 phase6 benchmark summary fetch-real clean

all: sim phase6 benchmark summary

sim: test phase1 phase2 phase3 phase4 phase5

setup: .venv/.installed

.venv/.installed: requirements.txt pyproject.toml
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt
	$(PY) -m pip install -q -e .
	touch $@

test: setup
	$(PY) -m pytest -q

phase1: setup
	$(PY) scripts/run_phase1.py > /dev/null

phase2: setup
	$(PY) scripts/run_phase2.py > /dev/null

phase3: setup
	$(PY) scripts/run_phase3.py > /dev/null

phase4: setup
	$(PY) scripts/run_phase4.py > /dev/null

phase5: setup
	$(PY) scripts/run_phase5.py > /dev/null

phase6: setup fetch-real
	$(PY) scripts/run_phase6.py > /dev/null

benchmark: setup fetch-real
	$(PY) scripts/benchmark.py > /dev/null

summary: setup
	$(PY) scripts/make_summary_figure.py > /dev/null

fetch-real: $(BOOK) $(TRADES)

$(BOOK):
	mkdir -p $(EXT)
	curl -sSL --fail -o $@.part $(BOOK_URL)
	echo "$(BOOK_SHA)  $@.part" | shasum -a 256 -c - && mv $@.part $@

$(TRADES):
	mkdir -p $(EXT)
	curl -sSL --fail -o $@.part $(TRADES_URL)
	echo "$(TRADES_SHA)  $@.part" | shasum -a 256 -c - && mv $@.part $@

clean:
	rm -rf .venv .pytest_cache .hypothesis src/*.egg-info $(EXT)/*.npz
