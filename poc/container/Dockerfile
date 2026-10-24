# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=1.19.4
ARG SUPERPOWERS_COMMIT=2ce8aa15d05bd57213e6f746f953aafb7ded7a8b

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Session trust is mounted, not copied into the image. TLS verification remains enabled.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm install --global "opencode-ai@${OPENCODE_VERSION}"

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export GIT_SSL_CAINFO=/run/secrets/proxy_ca; fi; \
    git init /opt/superpowers \
    && git -C /opt/superpowers remote add origin https://github.com/obra/superpowers.git \
    && git -C /opt/superpowers fetch --depth=1 origin "${SUPERPOWERS_COMMIT}" \
    && git -C /opt/superpowers checkout --detach FETCH_HEAD \
    && rm -rf /opt/superpowers/.git

LABEL org.agent-workbench.opencode-version="${OPENCODE_VERSION}" \
      org.agent-workbench.superpowers-commit="${SUPERPOWERS_COMMIT}"

USER node
WORKDIR /workspace/project
EXPOSE 4096
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]

