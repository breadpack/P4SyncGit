"""ServiceRegistry CRUD 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from p4gitsync.cli.service_registry import ServiceRegistry


@pytest.fixture()
def registry(tmp_path: Path) -> ServiceRegistry:
    return ServiceRegistry(path=tmp_path / "services.json")


def test_add_and_get(registry: ServiceRegistry) -> None:
    registry.add("my-svc", config="/etc/p4gitsync/config.toml", platform="linux")
    entry = registry.get("my-svc")
    assert entry is not None
    assert entry["config"] == "/etc/p4gitsync/config.toml"
    assert entry["platform"] == "linux"
    assert "installed_at" in entry


def test_list_all(registry: ServiceRegistry) -> None:
    registry.add("svc-a", config="a.toml", platform="linux")
    registry.add("svc-b", config="b.toml", platform="win32")
    all_services = registry.list_all()
    assert len(all_services) == 2
    assert "svc-a" in all_services
    assert "svc-b" in all_services


def test_remove(registry: ServiceRegistry) -> None:
    registry.add("temp", config="t.toml", platform="linux")
    registry.remove("temp")
    assert registry.get("temp") is None


def test_persistence(tmp_path: Path) -> None:
    json_path = tmp_path / "services.json"
    reg1 = ServiceRegistry(path=json_path)
    reg1.add("persistent", config="p.toml", platform="linux")

    reg2 = ServiceRegistry(path=json_path)
    entry = reg2.get("persistent")
    assert entry is not None
    assert entry["config"] == "p.toml"


def test_get_nonexistent(registry: ServiceRegistry) -> None:
    assert registry.get("no-such-service") is None
