# The image every sandboxed tool call runs in (ADR 0008). The worker starts one
# container from it per call, with no network, a read-only root and no
# capabilities. Small on purpose: a shell and the base image's core utilities.

# Pinned by digest; the tag is kept for readability.
FROM debian:trixie-20261104-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
# The uid the worker runs as, so that the files a command writes in a
# workspace belong to the worker and it can remove them.
RUN groupadd --system --gid 10001 sandbox \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin sandbox \
    && mkdir /workspace
USER 10001:10001
WORKDIR /workspace
