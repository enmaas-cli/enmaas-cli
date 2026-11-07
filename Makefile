.PHONY: check lint type test live check-install
check: lint type test
lint:
	ruff check src tests
	ruff format --check src tests
type:
	mypy
test:
	pytest --tb=short -q
live:
	pytest -m live --tb=short -v
check-install:
	bash scripts/test-install.sh
