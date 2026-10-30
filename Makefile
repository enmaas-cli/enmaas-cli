.PHONY: check lint type test live
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
