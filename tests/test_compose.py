"""Regression guards for docker-compose.yml.

The acceptance flow broke once because two services declared the same image
name while both had build sections, so Compose built and tagged the image
twice and the one-shot verify service could not start. These tests pin the
invariants that keep `docker compose up` and the verify profile working.
"""
from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def load_services():
    if not COMPOSE_PATH.is_file():
        pytest.fail(
            f"{COMPOSE_PATH} is missing; the Dockerfile must COPY "
            "docker-compose.yml into the image so the verify service can "
            "validate the compose configuration"
        )
    document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return document["services"]


def test_exactly_one_service_builds_the_image():
    services = load_services()
    builders = [name for name, svc in services.items() if "build" in svc]
    assert builders == ["api"], f"only the api service may build, got {builders}"


def test_all_services_share_a_single_image_name():
    services = load_services()
    names = {svc.get("image") for svc in services.values()}
    assert len(names) == 1 and None not in names, (
        f"every service must reference the one shared image, got {names}"
    )


def test_verify_is_a_profile_gated_one_shot_service():
    verify = load_services()["verify"]
    assert "build" not in verify, "verify must reuse the api image, not rebuild it"
    assert "verify" in verify["profiles"]
    assert verify["depends_on"]["api"]["condition"] == "service_healthy"
    assert "restart" not in verify or verify["restart"] in ("no", "on-failure")


def test_api_host_port_is_overridable_via_api_port():
    ports = load_services()["api"]["ports"]
    assert ports == ["${API_PORT:-8000}:8000"]
