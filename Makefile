UV ?= uv

.PHONY: bootstrap test lint

bootstrap:
	@command -v temporal >/dev/null || (echo "missing temporal CLI: brew install temporal" && exit 1)
	@command -v container >/dev/null || (echo "missing Apple container: brew install container" && exit 1)
	$(UV) sync

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check sandbox tests
