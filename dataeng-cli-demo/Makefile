PYTHON ?= python

.PHONY: help verify demo

help:
	$(PYTHON) dataeng_cli.py --help

verify:
	$(PYTHON) -m py_compile dataeng_cli.py
	$(PYTHON) dataeng_cli.py validate data/processed --format json

demo:
	$(PYTHON) dataeng_cli.py fetch --source pubchem --query aspirin --output data --mock --result-output data/fetch-result.json --log-file data/events.jsonl
	$(PYTHON) dataeng_cli.py sync --source pubchem --query aspirin --state data/state.db --mock --output data/sync-result.json --log-file data/events.jsonl
	$(PYTHON) dataeng_cli.py validate data/processed --format json --output data/quality-report.json --log-file data/events.jsonl
