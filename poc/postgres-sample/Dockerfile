# syntax=docker/dockerfile:1
FROM agent-workbench-m1:local
USER root
COPY package.json package-lock.json /opt/workbench-postgres/
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --prefix /opt/workbench-postgres --omit=dev --ignore-scripts --no-audit --no-fund
ENV NODE_PATH=/opt/workbench-postgres/node_modules
USER node
