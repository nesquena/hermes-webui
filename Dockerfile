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
    PYTHONPATH=/app \
    PYTHONIOENCODING=utf-8

WORKDIR /app

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
RUN chown -R hermeswebuitoo:hermeswebuitoo /app

USER hermeswebuitoo

# Install uv
# https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/hermeswebuitoo/.local/bin/:$PATH"
ENV UV_PROJECT_ENVIRONMENT=venv

# Verify that python3 and uv are installed
RUN which python3 && python3 --version
RUN which uv && uv --version

COPY . /app
RUN --mount=type=cache,target=/uv_cache,uid=1025,gid=1025,mode=0755 \
    export UV_CACHE_DIR=/uv_cache \
    && cd /app \
    && uv venv venv \
    && VIRTUAL_ENV=/app/venv uv pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org \
    && VIRTUAL_ENV=/app/venv uv pip install -U pip setuptools --trusted-host pypi.org --trusted-host files.pythonhosted.org \
    && test -d /app/venv \
    && test -f /app/venv/bin/activate \
    && test -x /app/venv/bin/python3 \
    && test -x /app/venv/bin/pip \
    && VIRTUAL_ENV=/app/venv uv pip install uv \
    && test -x /app/venv/bin/uv \
    && unset UV_CACHE_DIR

USER root

COPY --chmod=555 docker_init.bash /hermeswebui_init.bash

# Remove APT proxy configuration and clean up APT downloaded files
RUN rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/01proxy \
    && apt-get clean

USER hermeswebuitoo

# Default to binding all interfaces (required for container networking)
ENV HERMES_WEBUI_HOST=0.0.0.0
ENV HERMES_WEBUI_PORT=8787

# State directory (mount as volume for persistence)
ENV HERMES_WEBUI_STATE_DIR=/data

EXPOSE 8787

CMD ["/hermeswebui_init.bash"]

