# One image for every service: api, worker, migrate, and the fake provider.
# Each compose service runs it with a different command.

# Images are pinned by digest; the tag is kept for readability.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
COPY --from=uv /uv /usr/local/bin/uv
# Use the image's Python instead of downloading one, compile bytecode at build
# time for faster starts, and copy files because the cache mount is another filesystem.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
# Dependencies first, without the project code, so this layer is rebuilt only
# when the lock file changes.
COPY pyproject.toml uv.lock .python-version ./
COPY services/fake_provider/pyproject.toml services/fake_provider/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --all-packages --no-install-workspace
COPY README.md ./
COPY src/ src/
COPY services/ services/
# --no-editable installs the packages into .venv, so the final image needs
# only .venv and not the source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --all-packages --no-editable

FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
# Services run as an unprivileged user with a fixed ID.
RUN useradd --system --uid 10001 --no-create-home runtime
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER 10001
WORKDIR /app
