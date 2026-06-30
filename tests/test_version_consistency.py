"""Verify Climate REF component versions are pinned consistently across files.

The AFT repository ships a tested, deployable combination of pinned component
versions. Those pins are duplicated across ``pyproject.toml``, ``versions.toml``,
``helm/Chart.yaml``, ``helm/values.yaml`` and ``docker/docker-compose.yaml``.
These tests fail the build if any of them drift apart.
"""

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# The six pip-installable Climate REF packages that must share one version.
CORE_PACKAGES = [
    "climate-ref",
    "climate-ref-core",
    "climate-ref-celery",
    "climate-ref-esmvaltool",
    "climate-ref-pmp",
    "climate-ref-ilamb",
]


def _strip_v(tag: str) -> str:
    """Normalise an image tag (``v0.14.3``) to a bare version (``0.14.3``)."""
    return tag[1:] if tag.startswith("v") else tag


def _load_toml(rel: str) -> dict:
    return tomllib.loads((REPO_ROOT / rel).read_text())


def _load_yaml(rel: str) -> dict:
    return yaml.safe_load((REPO_ROOT / rel).read_text())


def _pyproject_core_pins() -> dict[str, str]:
    deps = _load_toml("pyproject.toml")["project"]["dependencies"]
    pins = {}
    for dep in deps:
        if "==" in dep:
            name, ver = dep.split("==", 1)
            pins[name.strip()] = ver.strip()
    return pins


def test_pyproject_core_pins_are_internally_consistent():
    pins = _pyproject_core_pins()
    for pkg in CORE_PACKAGES:
        assert pkg in pins, f"{pkg} missing an == pin in pyproject.toml"
    versions = {pins[pkg] for pkg in CORE_PACKAGES}
    assert len(versions) == 1, f"core packages pinned to differing versions: {pins}"


def test_pyproject_matches_versions_toml():
    pins = _pyproject_core_pins()
    components = _load_toml("versions.toml")["components"]
    for pkg in CORE_PACKAGES:
        assert components.get(pkg) == pins[pkg], (
            f"{pkg}: pyproject pins {pins[pkg]!r} but versions.toml has {components.get(pkg)!r}"
        )


def _core_version() -> str:
    return _pyproject_core_pins()["climate-ref"]


def test_chart_appversion_matches_core():
    chart = _load_yaml("helm/Chart.yaml")
    assert str(chart["appVersion"]) == _core_version()


def test_versions_toml_app_version_matches_core():
    helm = _load_toml("versions.toml")["helm"]
    assert helm["app-version"] == _core_version()


def test_helm_default_worker_image_matches_core():
    values = _load_yaml("helm/values.yaml")
    tag = values["defaults"]["image"]["tag"]
    assert _strip_v(tag) == _core_version(), f"helm defaults.image.tag {tag!r} != core {_core_version()!r}"


def test_docker_core_image_matches_core():
    services = _load_yaml("docker/docker-compose.yaml")["services"]
    for svc in ("ref-init", "climate-ref"):
        image = services[svc]["image"]
        assert _strip_v(image.rsplit(":", 1)[1]) == _core_version(), (
            f"{svc} image {image!r} != core {_core_version()!r}"
        )


def test_aft_release_version_consistent():
    aft = _load_toml("pyproject.toml")["project"]["version"]
    versions = _load_toml("versions.toml")
    chart = _load_yaml("helm/Chart.yaml")
    assert versions["aft"]["version"] == aft
    assert versions["helm"]["chart-version"] == aft
    assert str(chart["version"]) == aft


def _frontend_version() -> str:
    return _load_toml("versions.toml")["frontend"]["climate-ref-frontend"]


def test_frontend_tracked_in_versions_toml():
    assert _frontend_version(), "climate-ref-frontend missing from versions.toml [frontend]"


def test_helm_api_image_matches_frontend():
    values = _load_yaml("helm/values.yaml")
    tag = values["api"]["image"]["tag"]
    assert _strip_v(tag) == _frontend_version(), (
        f"helm api.image.tag {tag!r} != frontend {_frontend_version()!r}"
    )


def test_docker_frontend_image_matches_frontend():
    services = _load_yaml("docker/docker-compose.yaml")["services"]
    image = services["ref-app"]["image"]
    tag = image.rsplit(":", 1)[1]
    assert _strip_v(tag) == _frontend_version(), (
        f"docker ref-app image {image!r} != frontend {_frontend_version()!r}"
    )
