# IntelliKnow KMS — developer commands
#
# `make test` is the single documented command that runs the complete test
# suite (unit + property-based + integration) in one pytest run, reporting a
# pass/fail result per test and a total/passed/failed summary (Req 14.3, 14.5).

VENV   ?= .venv
PYTHON := $(VENV)/bin/python

.PHONY: test venv install

# Create the virtual environment.
venv:
	python3.11 -m venv $(VENV)

# Install pinned dependencies into the virtual environment.
install:
	$(PYTHON) -m pip install -r requirements.txt

# Run the full test suite in one command.
test:
	$(PYTHON) -m pytest
