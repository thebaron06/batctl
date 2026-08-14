# batctl - Fronius Gen24 battery charge/discharge controller
#
# Supported platforms (single Debian-based image):
#   linux/amd64        x86_64
#   linux/arm/v6       Raspberry Pi Zero W (original)
#   linux/arm/v7       Raspberry Pi 2, Pi 3 (32-bit OS), Pi 4 (32-bit OS)
#   linux/arm64        Raspberry Pi 3/4/5, Pi Zero 2 W (64-bit OS)
#
# Build for the current host platform:
#   docker compose up -d --build
#
# Cross-compile for a specific platform (requires docker buildx + QEMU binfmt):
#   docker buildx build --platform linux/amd64  -t batctl:amd64  .
#   docker buildx build --platform linux/arm/v7  -t batctl:armv7  .
#   docker buildx build --platform linux/arm64   -t batctl:arm64  .

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        cron \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies into a venv (excludes pytest which is dev-only)
COPY requirements.txt .
RUN python3 -m venv /app/.venv && \
    grep -v '^pytest' requirements.txt > /tmp/reqs.txt && \
    /app/.venv/bin/pip install --no-cache-dir -r /tmp/reqs.txt && \
    rm /tmp/reqs.txt

COPY batctl.py .
COPY dispatch.py .

# System cron drop-in (Debian /etc/cron.d/ format, requires username field)
COPY docker/crontab /etc/cron.d/batctl
RUN chmod 644 /etc/cron.d/batctl

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expected mount point for the user-supplied config
RUN mkdir -p /config

ENV BATCTL_CONFIG=/config/batctl.conf
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/entrypoint.sh"]
