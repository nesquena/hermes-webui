FROM python:3.12-slim

LABEL maintainer="nesquena"
LABEL description="Hermes Web UI — browser interface for Hermes Agent"

# Install system packages
ENV DEBIAN_FRONTEND=noninteractive

# Make use of apt-cacher-ng if available
RUN if [ "A${BUILD_APT_PROXY:-}" != "A" ]; then \
        echo "Using APT proxy: ${BUILD_APT_PROXY}"; \
        printf 'Acquire::http::Proxy "%s";\n' "$BUILD_APT_PROXY" > /etc/apt/apt.conf.d/01proxy; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN apt-get update -y --fix-missing --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    apt-utils \
    locales \
    ca-certificates \
    sudo \
    curl \
    rsync \
    openssh-client \
    git \
    xz-utils \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# UTF-8
RUN localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
ENV LANG=en_US.utf8
ENV LC_ALL=C

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /apptoo

# Every sudo group user does not need a password
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Create a new group for the hermeswebui and hermeswebuitoo users
RUN groupadd -g 1024 hermeswebui \
    && groupadd -g 1025 hermeswebuitoo

# The hermeswebui (resp. hermeswebuitoo) user will have UID 1024 (resp. 1025),
# be part of the hermeswebui (resp. hermeswebuitoo) and users groups and be sudo capable (passwordless)
RUN useradd -u 1024 -d /home/hermeswebui -g hermeswebui -s /bin/bash -m hermeswebui \
    && usermod -G users hermeswebui \
    && adduser hermeswebui sudo
RUN useradd -u 1025 -d /home/hermeswebuitoo -g hermeswebuitoo -s /bin/bash -m hermeswebuitoo \
    && usermod -G users hermeswebuitoo \
    && adduser hermeswebuitoo sudo
RUN chown -R hermeswebuitoo:hermeswebuitoo /apptoo

USER root

# Pre-install uv system-wide so the container doesn't need internet access at runtime.
# Installing as root places uv in /usr/local/bin, available to all users.
# The init script will skip the download when uv is already on PATH.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Install Node.js 22 LTS (for browser tools)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then NODE_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; \
    else echo "Unsupported architecture: $ARCH" && exit 1; fi && \
    INDEX_URL="https://nodejs.org/dist/latest-v22.x/" && \
    TARBALL=$(curl -fsSL "$INDEX_URL" | grep -oE "node-v22\.[0-9]+\.[0-9]+-linux-${NODE_ARCH}\.tar\.xz" | head -1) && \
    [ -n "$TARBALL" ] || { echo "Could not find Node.js tarball"; exit 1; } && \
    curl -fsSL "${INDEX_URL}${TARBALL}" -o /tmp/node.tar.xz && \
    tar -C /usr/local --strip-components=1 -xJf /tmp/node.tar.xz && \
    rm /tmp/node.tar.xz && \
    node --version && npm --version

# Pre-bake the Hermes Agent source into the image to avoid runtime network installs.
# Use /opt/hermes which is not affected by the mounted .hermes volume.
COPY hermes-agent-desktop/hermes-agent /opt/hermes/

# Tell the WebUI where to find the agent (used by bootstrap if manually invoked)
ENV HERMES_WEBUI_AGENT_DIR=/opt/hermes

# Copy the init script (must be before using it as CMD)
COPY --chmod=555 hermes-webui/docker_init.bash /hermeswebui_init.bash

# Marker that we're inside a container
RUN touch /.within_container

# Remove APT proxy configuration and clean up APT downloaded files
RUN rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/01proxy \
    && apt-get clean

USER hermeswebuitoo

# Copy the WebUI application code
COPY --chown=hermeswebuitoo:hermeswebuitoo hermes-webui/ /apptoo/

# Bake the git version tag into the image so the settings badge works even
# when .git is not present (it is excluded by .dockerignore).
# CI passes: --build-arg HERMES_VERSION=$(git describe --tags --always)
# Local builds that omit the arg get "unknown" as the fallback.
ARG HERMES_VERSION=unknown
RUN echo "__version__ = '${HERMES_VERSION}'" > /apptoo/api/_version.py

# Default to binding all interfaces (required for container networking)
ENV HERMES_WEBUI_HOST=0.0.0.0
ENV HERMES_WEBUI_PORT=8787

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8787/health || exit 1

CMD ["/hermeswebui_init.bash"]
