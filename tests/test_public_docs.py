"""Files a stranger relies on in the public mirror. Content checks only; prose is reviewed."""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _project() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]


def _workflows() -> Path:
    # Private repo: templates under public/.github (the export maps them to .github).
    public = REPO / "public" / ".github" / "workflows"
    return public if public.is_dir() else REPO / ".github" / "workflows"


def test_licence_is_apache_everywhere() -> None:
    licence = (REPO / "LICENSE").read_text()
    assert "Apache License" in licence and "Version 2.0" in licence
    project = _project()
    assert project["license"] == "Apache-2.0"
    assert project["authors"] == [{"name": "LINC Innovations"}]
    assert not any("MIT" in c for c in project.get("classifiers", []))
    assert "Copyright 2026 LINC Innovations LLC" in (REPO / "NOTICE").read_text()


def test_changelog_top_section_matches_package_version() -> None:
    top = re.search(r"^## \[([^\]]+)\]", (REPO / "CHANGELOG.md").read_text(), re.M)
    assert top, "CHANGELOG.md needs a '## [x.y.z]' section"
    assert top.group(1) == _project()["version"]


def test_readme_states_mirror_model_licence_and_cta() -> None:
    readme = (REPO / "README.md").read_text()
    assert "release mirror" in readme
    assert "Apache-2.0" in readme
    assert "lincinnovations.com/audit?utm_source=github" in readme


def test_contributing_requires_dco_and_security_has_contact() -> None:
    assert "Signed-off-by" in (REPO / "CONTRIBUTING.md").read_text()
    assert "Report a vulnerability" in (REPO / "SECURITY.md").read_text()


def test_release_uses_trusted_publishing_and_no_secrets() -> None:
    release = (_workflows() / "release.yml").read_text()
    assert "id-token: write" in release
    assert "pypa/gh-action-pypi-publish" in release
    for wf in ("test.yml", "release.yml"):
        text = (_workflows() / wf).read_text()
        assert "secrets." not in text, wf
        assert "LincForge/linc-" not in text, wf
