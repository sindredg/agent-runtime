.PHONY: all up down clean logs test integration chaos

# The group that owns the Docker socket. The worker joins it to start a
# container for each sandboxed tool call. Every compose command needs it.
export DOCKER_GID ?= $(shell stat -c %g /var/run/docker.sock)

# The default target runs the unit tests.
all: test

# Build the images, start every service, and wait until all are healthy.
up:
	docker compose up --build --wait

# Stop the services and keep the database volume.
down:
	docker compose down

# Stop the services and delete the database volume.
clean:
	docker compose down --volumes --remove-orphans

logs:
	docker compose logs --follow

test:
	uv run --frozen pytest tests/unit

# Needs the stack from `make up`: the tests use its Postgres server.
integration:
	uv run --frozen pytest tests/integration

# Kills, pauses and crashes workers on purpose and checks what is left.
# Starts the stack with three workers and a short lease first. CHAOS_RUNS
# sets the number of runs with a random kill time (default 20), CHAOS_SEED the times.
# `make up` afterwards brings the normal stack back.
chaos:
	mkdir -p .chaos
	chmod 777 .chaos
	docker compose -f docker-compose.yml -f docker-compose.chaos.yml up --build --wait
	uv run --frozen pytest -v -s tests/chaos
