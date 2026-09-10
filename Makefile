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

.PHONY: up down workers status smoke vm vms show

up:
	./scripts/up.sh

down:
	./scripts/down.sh

workers:
	$(UV) run honcho start

status:
	$(UV) run python -m sandbox.status.api

smoke:
	$(UV) run python -m sandbox.orchestrator.run_smoke

vm:
	$(UV) run python -m sandbox.manager.launch_vm

vms:
	container ls --all

show:
	$(UV) run python -m sandbox.registry.show

.PHONY: image

image:
	./scripts/build-image.sh

.PHONY: artifact

artifact:
	$(UV) run python -m sandbox.runner.package

.PHONY: demo session chaos-kill chaos-stop chaos-delete policy reconcile hold terminate

SECONDS ?= 120

demo:
	SCENARIO=$(SCENARIO) ./scripts/demo.sh

session:
	$(UV) run python -m sandbox.orchestrator.run_session \
	  $(if $(SCENARIO),--scenario $(SCENARIO)) \
	  $(if $(PROMPT),--prompt "$(PROMPT)") \
	  $(if $(TURNS),--max-turns $(TURNS)) \
	  $(if $(TURN_SECONDS),--turn-seconds $(TURN_SECONDS))

chaos-kill:
	$(UV) run python -m sandbox.manager.chaos kill $(VM)

chaos-stop:
	$(UV) run python -m sandbox.manager.chaos stop $(VM)

chaos-delete:
	$(UV) run python -m sandbox.manager.chaos delete $(VM)

policy:
	$(UV) run python -m sandbox.manager.policy_cli $(if $(MIN_IDLE),--min-idle $(MIN_IDLE)) $(if $(MAX),--max $(MAX))

reconcile:
	$(UV) run python -m sandbox.manager.reconcile_once

hold:
	$(UV) run python -m sandbox.orchestrator.run_hold --seconds $(SECONDS)

terminate:
	$(UV) run python -m sandbox.orchestrator.terminate $(WF)
