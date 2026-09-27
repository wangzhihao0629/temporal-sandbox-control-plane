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
	$(UV) run python -m sandbox.cli.smoke

vm:
	$(UV) run python -m sandbox.cli.vm

vms:
	container ls --all

show:
	$(UV) run python -m sandbox.cli.show

.PHONY: image

image:
	./scripts/build-image.sh

.PHONY: check-sudoers check-network
check-sudoers:
	./scripts/check-sudoers.sh

check-network:
	./scripts/check-network.sh

.PHONY: artifact

artifact:
	$(UV) run python -m sandbox.runner.package

.PHONY: demo session gobuild chaos-kill chaos-stop chaos-delete policy reconcile hold terminate

SECONDS ?= 120

demo:
	SCENARIO=$(SCENARIO) ./scripts/demo.sh

session:
	$(UV) run python -m sandbox.cli.session \
	  $(if $(SCENARIO),--scenario $(SCENARIO)) \
	  $(if $(PROMPT),--prompt "$(PROMPT)") \
	  $(if $(TURNS),--max-turns $(TURNS)) \
	  $(if $(TURN_SECONDS),--turn-seconds $(TURN_SECONDS)) \
	  $(if $(TURN_TIMEOUT),--turn-timeout $(TURN_TIMEOUT)) \
	  $(if $(STEP_TIMEOUT),--step-timeout $(STEP_TIMEOUT))

gobuild:
	$(UV) run python -m sandbox.cli.gobuild $(foreach a,$(ARGS),--arg=$(a))

chaos-kill:
	$(UV) run python -m sandbox.cli.chaos kill $(VM)

chaos-stop:
	$(UV) run python -m sandbox.cli.chaos stop $(VM)

chaos-delete:
	$(UV) run python -m sandbox.cli.chaos delete $(VM)

policy:
	$(UV) run python -m sandbox.cli.policy $(if $(MIN_IDLE),--min-idle $(MIN_IDLE)) $(if $(MAX),--max $(MAX))

reconcile:
	$(UV) run python -m sandbox.cli.reconcile

hold:
	$(UV) run python -m sandbox.cli.hold --seconds $(SECONDS)

terminate:
	$(UV) run python -m sandbox.cli.terminate $(WF)
