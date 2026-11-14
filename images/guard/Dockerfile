# The guard of a scoped tool call (ADR 0009). The worker starts one per call.
# It sets nftables rules in its own network namespace and waits; the tool
# container joins that namespace without the right to change them. Small on
# purpose: a shell and nftables.

# Pinned by digest, the same base as the sandbox image.
FROM debian:trixie-20261104-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update \
    && apt-get install -y --no-install-recommends nftables=1.1.3-1 \
    && rm -rf /var/lib/apt/lists/*
