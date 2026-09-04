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

.PHONY: up down workers smoke vm vms show

up:
	./scripts/up.sh

down:
	./scripts/down.sh

workers:
	$(UV) run honcho start

smoke:
	$(UV) run python -m sandbox.orchestrator.run_smoke

vm:
	$(UV) run python -m sandbox.manager.launch_vm

vms:
	container ls --all

show:
	$(UV) run python -m sandbox.registry.show
