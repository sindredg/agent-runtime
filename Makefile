.PHONY: all up down clean logs test integration

# The default target runs the unit tests.
all: test

# Build the image, start every service, and wait until all are healthy.
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
