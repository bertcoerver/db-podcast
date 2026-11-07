# syntax=docker/dockerfile:1
#
# Built by .github/workflows/addon.yml, with the *repository root* as the build
# context, so the source in src/ can be copied in directly. To build it by hand,
# from the repository root:
#
#   docker build -f addon/Dockerfile .
ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.4.0
FROM ${BUILD_FROM}

ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.0.0.dev0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Prepend, never replace: the base image's own s6 services need /usr/sbin.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# There is no .git in the context (see .dockerignore), so setuptools-scm is
# told the version instead of reading it from the tags.
COPY pyproject.toml README.md LICENSE /tmp/db-podcast/
COPY src /tmp/db-podcast/src
RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip; \
    SETUPTOOLS_SCM_PRETEND_VERSION="${BUILD_VERSION}" \
        /opt/venv/bin/pip install --no-cache-dir /tmp/db-podcast; \
    rm -rf /tmp/db-podcast; \
    ln -sf /opt/venv/bin/db-podcast /usr/local/bin/db-podcast; \
    db-podcast --help > /dev/null

COPY addon/rootfs /

RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/db-podcast/run \
    /etc/s6-overlay/s6-rc.d/db-podcast/finish \
    /usr/local/bin/db-podcast-run

LABEL \
    io.hass.name="db-podcast" \
    io.hass.description="Publish feeds as podcasts through Dropbox" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.source="https://github.com/bertcoerver/db-podcast"
