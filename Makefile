.PHONY: all up up-claude down clean logs test integration escape chaos lab kills kills-claude
.PHONY: switched-off switched-off-rehearsal notify-timing long-up

# The group that owns the Docker socket. The worker joins it to start a
# container for each sandboxed tool call. Every compose command needs it.
export DOCKER_GID ?= $(shell stat -c %g /var/run/docker.sock)

# The model of `make kills-claude`, as raw text: $(value ...) keeps make from
# expanding a $(shell ...) in it, and override applies that to a value from
# the command line too. ifdef does not expand it either.
ifdef KILLS_MODEL
override export KILLS_MODEL := $(value KILLS_MODEL)
endif

# The default target runs the unit tests.
all: test

# Build the images, start every service, and wait until all are healthy.
# The workers are always on the fake provider.
up:
	docker compose up --build --wait

# The same stack with the workers on the Claude API. Every job costs money.
# Needs CLAUDE_API_KEY in .env; CLAUDE_MODEL picks the model. The test suites
# refuse to run against it, all but the kill runs of `make kills-claude`.
# `make up` puts the workers back on the fake.
# The last line prints the provider and model the worker got, not the key.
up-claude:
	docker compose -f docker-compose.yml -f docker-compose.claude.yml up --build --wait
	docker compose exec -T worker printenv ANTHROPIC_BASE_URL MODEL

# The stack for a job longer than the context window: two workers, no
# sandbox, the fake provider with the scenarios of LONG_SCENARIO_DIR, and
# issue numbers from 100000. SUMMARY_TRIGGER comes from the shell; unset,
# no job summarises. Starts the stack and runs nothing. `make up` puts the
# normal stack back.
long-up:
	docker compose -f docker-compose.yml -f docker-compose.long.yml up --build --wait

# Stop the services and keep the database volume.
down:
	docker compose down

# Stop the services and delete the database volume, and the lab's Gitea data.
clean:
	docker compose -f docker-compose.yml -f docker-compose.lab.yml down --volumes --remove-orphans

logs:
	docker compose logs --follow

test:
	uv run --frozen pytest tests/unit

# Needs the stack from `make up`: the tests use its Postgres server.
integration:
	uv run --frozen pytest tests/integration

# Tries to get out of the sandbox: the network, the host's files, another
# job's workspace, the Docker socket, privileges, processes and memory. Builds
# the sandbox image first; needs no stack. ESCAPE_DOCKER_DEFAULTS=1 runs the
# same attempts with Docker's default container settings, which must fail.
escape:
	docker compose build sandbox-image guard-image
	uv run --frozen pytest -v -s tests/escape

# Kills, pauses and crashes workers on purpose and checks what is left.
# Starts the stack with three workers and a short lease first. CHAOS_RUNS
# sets the number of runs with a random kill time (default 20), CHAOS_SEED the times.
# `make up` afterwards brings the normal stack back.
chaos:
	mkdir -p .chaos
	chmod 777 .chaos
	docker compose -f docker-compose.yml -f docker-compose.chaos.yml up --build --wait
	uv run --frozen pytest -v -s tests/chaos

# G12's timed run: 50 questions on the stack of `make up`, no kill, each
# first delivery within 5 s of its park. Records in .notify-timing/.
# Recorded runs start from the Manual runs workflow (ADR 0020).
notify-timing: up
	uv run --frozen pytest -v -s tests/notify_timing

# One switched-off run from scripts/switched_off.py, named by SWITCHED_OFF:
# switches one mechanism off in the checkout, starts the stack, runs the tests
# that guard it, and fails unless each ends as the list says (ADR 0021).
# Changes the checkout: `git checkout -- .` afterwards. The entry names its
# provider. One on the Claude API costs money, and starts only with
# SWITCHED_OFF_PROVIDER=claude and CLAUDE_API_KEY in .env or the environment
# (ADR 0022). Recorded runs start from the Manual runs workflow.
switched-off:
	uv run --frozen python scripts/switched_off.py run "$$SWITCHED_OFF"

# An entry of the Claude API on the fake provider: every step of the run, no
# key, no cost, and no result. It passes only if the test passes and the
# run's one record says that no job sent a rebuilt thinking block.
switched-off-rehearsal:
	uv run --frozen python scripts/switched_off.py rehearse "$$SWITCHED_OFF"

# The lab demo: create_issue against Gitea in a container, and a worker that
# stops itself in the middle of the call. Sets Gitea up (users, repository,
# a token that can only write issues), then runs the two cases. Fake provider.
# At the end Gitea stops, its data kept, and the normal stack is back.
lab:
	docker compose -f docker-compose.yml -f docker-compose.lab.yml build
	uv run --frozen pytest -v -s tests/lab

# Kill runs that are judged without a reference run: workers killed at
# random times and stopped inside the call that opens an issue. Three
# workers, a 6 s lease, the fake provider. KILLS_RUNS sets the number of
# random kills (default 20), KILLS_SEED the times. KILLS_TESTS picks tests
# by name (pytest -k), for a must-fail run. Records in .kills/.
# `make up` afterwards brings the normal stack back.
KILLS_SELECT = $(if $(KILLS_TESTS),-k '$(KILLS_TESTS)')

kills:
	docker compose -f docker-compose.yml -f docker-compose.kills.yml up --build --wait
	uv run --frozen pytest -x -v -s tests/kills $(KILLS_SELECT)

# The same runs with the workers on the Claude API. Every job costs money:
# 33 jobs of at most 50,000 tokens each. Needs CLAUDE_API_KEY in .env or the
# environment, and the model named here: `make kills-claude
# KILLS_MODEL=claude-haiku-5-5`. Recorded runs start from the Manual runs
# workflow (ADR 0020). Stops after the first test that fails. `make up` puts the
# workers back on the fake.
# KILLS_MODEL reaches the recipe through the environment, never pasted into a
# shell line, and the whole string must be a model id: no newline, no other
# character. It is taken as raw text where it is defined, below.
kills-claude:
	@case "$$KILLS_MODEL" in claude-?*) ;; *) echo "Name the model: make kills-claude KILLS_MODEL=claude-haiku-5-5" >&2; exit 1;; esac
	@case "$$KILLS_MODEL" in *[!a-z0-9.-]*) echo "Not a model id: lower-case letters, digits, dots and hyphens only" >&2; exit 1;; esac
	docker compose -f docker-compose.yml -f docker-compose.kills.yml -f docker-compose.claude.yml up --build --wait
	KILLS_PROVIDER=claude uv run --frozen pytest -x -v -s tests/kills $(KILLS_SELECT)
