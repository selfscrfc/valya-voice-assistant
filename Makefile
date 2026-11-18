PYTHON ?= python3.14

.PHONY: setup test run
setup:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
	.venv/bin/python scripts/download_model.py
	.venv/bin/python scripts/download_whisper.py

test:
	.venv/bin/python -m unittest discover -s tests -v

run:
	./run

.PHONY: desktop build-desktop
build-desktop:
	.venv/bin/python scripts/build_desktop.py

desktop:
	./desktop

.PHONY: test-native
test-native:
	.venv/bin/python scripts/test_native.py
