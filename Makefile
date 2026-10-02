.DEFAULT_GOAL := help
PYTHON ?= python
NPM ?= npm

.PHONY: help pilot dataset train candidate evaluate-candidate replay api web smoke test lint

help:
	@echo "Targets: pilot dataset train candidate evaluate-candidate replay api web smoke test lint"

pilot:
	$(PYTHON) scripts/pilot.py

dataset:
	$(PYTHON) scripts/build_dataset.py

train:
	$(PYTHON) scripts/train_all.py --replace

candidate:
	$(PYTHON) scripts/train_reduced_c00.py

evaluate-candidate:
	$(PYTHON) scripts/evaluate_reduced_c00.py

replay:
	$(PYTHON) scripts/export_replay.py --replace

api:
	$(PYTHON) scripts/serve.py

web:
	$(PYTHON) scripts/build_web.py

smoke:
	$(PYTHON) scripts/smoke.py

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests scripts
