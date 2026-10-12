# Built on the Pi by the Home Assistant Supervisor when the app is installed.
# Since Supervisor 2026.04 the base image must be named here explicitly (no BUILD_FROM).
FROM python:3.12-slim

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/homewatch
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY homewatch_agent ./homewatch_agent

# Runs as root inside the app container: /data (options + outbox) is root-owned.
CMD ["python", "-m", "homewatch_agent"]
