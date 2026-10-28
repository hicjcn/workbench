# Build the pinned toolchain first: docker build -t agent-workbench-m1:local poc/container
FROM agent-workbench-m1:local

# Extend this image with project-specific compilers and package managers.
# Networked installation steps must preserve TLS verification and use the session CA.
RUN node --version && git --version && opencode --version
LABEL org.agent-workbench.sample-profile="node"

# Keep the inherited non-root user, working directory and container keepalive.
# Workbench starts the pinned Agent CLI separately after the container starts.
# Application source and configuration are supplied as independent workspace mounts.
