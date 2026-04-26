# Paperless KIplus standalone worker for Docker / Unraid.
#
# Purpose:
# - Runs the full Paperless KIplus engine outside Home Assistant.
# - Serves the standalone web UI and JSON API from src/worker_api.py.
#
# Inputs:
# - /data volume with config, state, logs and exports.
# - Environment variables for bind address, port and optional API token.
#
# Debugging:
# - docker logs <container>
# - Open /api/status and /api/logs on the worker port.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPERLESS_KIPLUS_DATA_DIR=/data \
    PAPERLESS_KIPLUS_HOST=0.0.0.0 \
    PAPERLESS_KIPLUS_PORT=8787

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8787
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "/app/src/worker_api.py"]
