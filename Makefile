PYTHON ?= python3

.PHONY: install-dev bdd unit test build
install-dev:
	$(PYTHON) -m pip install -e '.[dev]'

bdd:
	$(PYTHON) -m behave --format progress

unit:
	$(PYTHON) -m unittest discover -s tests -t . -v

test:
	$(PYTHON) tools/pipeline.py --check-only

build:
	$(PYTHON) tools/pipeline.py
