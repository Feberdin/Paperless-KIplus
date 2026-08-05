# Purpose:
# - Runs the full Paperless KIplus engine outside Home Assistant.
# - Serves the standalone web UI and JSON API from src/worker_api.py.
#
# Inputs:
# - /data volume with config, state, logs and exports.
# - Environment variables for bind address, port and optional API token.
#
# Important invariants:
# - The runtime process must not run as root.
# - Production may override the numeric UID/GID to match an existing Unraid appdata directory.
# - /app is immutable at runtime; all persistent writes belong below /data.
#
# Debugging:
# - Use the deployment broker's redacted logs_tail output.
# - Open /api/status and /api/logs on the worker port.
# - A startup error naming /data means the mounted directory is not writable by the runtime UID/GID.

FROM python:3.12.13-slim-trixie

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPERLESS_KIPLUS_DATA_DIR=/data \
    PAPERLESS_KIPLUS_HOST=0.0.0.0 \
    PAPERLESS_KIPLUS_PORT=8787

WORKDIR /app

# Why this section exists:
# The image is safe by default for Docker users, while production Compose can
# map the process to Unraid's established appdata ownership without root access.
RUN groupadd --gid "${APP_GID}" paperless-kiplus \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        paperless-kiplus \
    && mkdir -p /data/config /data/state /data/logs /data/exports \
    && chown -R paperless-kiplus:paperless-kiplus /data

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=paperless-kiplus:paperless-kiplus src /app/src
COPY --chown=paperless-kiplus:paperless-kiplus docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

EXPOSE 8787
VOLUME ["/data"]

USER paperless-kiplus:paperless-kiplus

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "/app/src/worker_api.py"]
