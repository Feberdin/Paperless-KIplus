"""
Purpose: Guard the production worker image against accidental root execution.
Input/Output: Reads the committed Dockerfile and Broker Compose YAML; performs no network access.
Important invariants: The image has a non-root default and Unraid keeps its established UID/GID.
Debugging: If this fails, inspect Dockerfile USER plus the production Compose user and port settings.
"""

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = REPOSITORY_ROOT / "docker" / "Dockerfile"
BROKER_COMPOSE_PATH = REPOSITORY_ROOT / "docker" / "docker-compose.unraid-broker.yml"


def test_worker_dockerfile_uses_specific_base_and_non_root_user() -> None:
    """The default image must satisfy the Trivy non-root Dockerfile policy."""

    dockerfile_lines = [
        line.strip()
        for line in DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    from_instructions = [line for line in dockerfile_lines if line.upper().startswith("FROM ")]
    user_instructions = [line for line in dockerfile_lines if line.upper().startswith("USER ")]

    assert from_instructions == ["FROM python:3.12.13-slim-trixie"]
    assert user_instructions
    assert user_instructions[-1].split(maxsplit=1)[1].lower() not in {"root", "0", "0:0"}


def test_broker_compose_preserves_unraid_appdata_identity() -> None:
    """Production maps the non-root process to the existing Unraid appdata owner."""

    compose = yaml.safe_load(BROKER_COMPOSE_PATH.read_text(encoding="utf-8"))
    worker = compose["services"]["paperless-kiplus-worker"]

    assert worker["user"] == "99:100"
    assert "/mnt/user/appdata/paperless-kiplus:/data" in worker["volumes"]


def test_broker_compose_uses_stable_review_port() -> None:
    """Redeployments must keep the documented review URL on host port 8788."""

    compose = yaml.safe_load(BROKER_COMPOSE_PATH.read_text(encoding="utf-8"))
    worker = compose["services"]["paperless-kiplus-worker"]

    assert worker["ports"] == ["8788:8788"]
