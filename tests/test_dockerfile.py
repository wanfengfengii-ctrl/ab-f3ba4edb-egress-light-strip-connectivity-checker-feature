"""Repo-side guard: the image must ship every file the test suite reads.

tests/test_compose.py validates docker-compose.yml at runtime, so the
Dockerfile has to copy it into the image; otherwise the verify service's
in-container pytest run dies with a missing-file error before acceptance.py
can produce a verdict. This test fails fast in the repo/CI when the COPY
list drifts. Inside the runtime image the Dockerfile itself is not shipped,
so the guard skips there (the compose tests still cover the file's presence).
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"

pytestmark = pytest.mark.skipif(
    not DOCKERFILE.is_file(),
    reason="Dockerfile is not shipped in the runtime image; repo-side guard only",
)


def copied_sources():
    sources = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("COPY "):
            parts = line.split()
            sources.update(parts[1:-1])  # final token is the destination
    return sources


def test_compose_file_is_copied_into_the_image():
    assert "docker-compose.yml" in copied_sources()


def test_suite_and_acceptance_files_are_copied():
    sources = copied_sources()
    for required in ("app", "tests", "acceptance.py", "pytest.ini"):
        assert required in sources, f"Dockerfile must COPY {required}"
