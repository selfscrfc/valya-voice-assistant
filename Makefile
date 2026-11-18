PYTHON ?= python3.14

.PHONY: setup test run
setup:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
	.venv/bin/python scripts/download_model.py

test:
	.venv/bin/python -m unittest discover -s tests -v

run:
	./run
