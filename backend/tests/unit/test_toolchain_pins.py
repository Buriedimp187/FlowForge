"""Dependency declaration, lock files, and test/CI image pins stay consistent.

``backend/pyproject.toml`` declares dependencies; ``requirements-lock.txt`` and
``requirements-dev-lock.txt`` are the exact-version installation locks; the
PostgreSQL image is named once in ``tests/integration/support.py`` and must be
identical in the CI ``services:`` block (docs/adr/0015). This is a static check:
it installs and runs nothing.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from tests.integration.support import POSTGRES_IMAGE

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(?P<version>[A-Za-z0-9.!+_-]+)$")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_pin(requirement: str) -> tuple[str, str]:
    match = _PIN.match(requirement.strip())
    assert match, f"not an exact '==' pin: {requirement!r}"
    return _normalize(match["name"]), match["version"]


def _read_lock(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()  # drop comments and markers
        if not line:
            continue
        name, version = _parse_pin(line)
        assert name not in pins, f"duplicate lock entry for {name}"
        pins[name] = version
    return pins


class DependencyLockTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        cls.runtime_lock = _read_lock(BACKEND / "requirements-lock.txt")
        cls.dev_lock = _read_lock(BACKEND / "requirements-dev-lock.txt")

    def test_every_direct_dependency_is_pinned_exactly(self) -> None:
        for requirement in self.project["dependencies"] + self.project["optional-dependencies"]["test"]:
            with self.subTest(requirement=requirement):
                _parse_pin(requirement)

    def test_runtime_dependencies_match_the_runtime_lock(self) -> None:
        for requirement in self.project["dependencies"]:
            name, version = _parse_pin(requirement)
            with self.subTest(name=name):
                self.assertEqual(self.runtime_lock.get(name), version)

    def test_test_dependencies_match_the_dev_lock_and_stay_out_of_runtime(self) -> None:
        for requirement in self.project["optional-dependencies"]["test"]:
            name, version = _parse_pin(requirement)
            with self.subTest(name=name):
                self.assertEqual(self.dev_lock.get(name), version)
                self.assertNotIn(name, self.runtime_lock)
        self.assertEqual(set(self.runtime_lock) & set(self.dev_lock), set(), "locks must not overlap")

    def test_the_approved_persistence_stack_is_declared_and_no_orm_or_broker_is(self) -> None:
        declared = {_parse_pin(r)[0] for r in self.project["dependencies"]}
        self.assertTrue({"sqlalchemy", "alembic", "psycopg"} <= declared)
        every_locked = set(self.runtime_lock) | set(self.dev_lock)
        for banned in ("redis", "celery", "psycopg2", "psycopg2-binary", "asyncpg", "sqlmodel"):
            self.assertNotIn(banned, every_locked)

    def test_python_floor_covers_uuid7(self) -> None:
        # uuid.uuid7() first exists in Python 3.14 (design D1).
        self.assertTrue(self.project["requires-python"].startswith(">=3.14"))


class CiImagePinTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_test_image_is_digest_pinned(self) -> None:
        self.assertRegex(POSTGRES_IMAGE, r"^postgres:17\.\d+@sha256:[0-9a-f]{64}$")

    def test_ci_postgres_service_uses_the_same_image(self) -> None:
        images = re.findall(r"^\s*image:\s*(\S+)\s*$", self.workflow, flags=re.MULTILINE)
        self.assertEqual(images, [POSTGRES_IMAGE])

    def test_ci_hands_the_service_to_the_tests_and_forbids_silent_skips(self) -> None:
        self.assertIn("FLOWFORGE_TEST_DATABASE_URL", self.workflow)
        self.assertIn('FLOWFORGE_REQUIRE_DB_TESTS: "1"', self.workflow)

    def test_every_third_party_action_is_pinned_to_a_commit_sha(self) -> None:
        uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", self.workflow, flags=re.MULTILINE)
        self.assertTrue(uses)
        for reference in uses:
            with self.subTest(uses=reference):
                self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$")
