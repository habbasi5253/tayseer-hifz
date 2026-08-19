"""Guards on the deployment manifests.

Two dependency lists now exist — `pyproject.toml` for Vercel's `uv lock`, and
`requirements.txt` for the Dockerfile and local setup. Two lists drift, and the
symptom is a build that succeeds locally and fails in production, or worse a
production install of a different version than anything tested. These are cheap
checks that keep them honest.
"""

from __future__ import annotations

import pathlib

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def requirements():
    return {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith(("#", "-"))
    }


def test_dependency_lists_agree():
    """The exact failure that broke the first deploy: a pin in one list only."""
    assert set(pyproject()["project"]["dependencies"]) == requirements()


def test_every_dependency_is_pinned():
    """An unpinned dependency means production can install something untested."""
    unpinned = [d for d in pyproject()["project"]["dependencies"] if "==" not in d]
    assert unpinned == [], f"unpinned: {unpinned}"


def test_vercel_entrypoint_is_a_module_path():
    """A bare filename gets loaded as a file, breaking package-relative imports."""
    entry = pyproject()["tool"]["vercel"]["entrypoint"]
    assert entry == "app.main:app"
    assert ":" in entry and "/" not in entry


def test_pyproject_has_a_project_table():
    """Vercel runs `uv lock`, which fails outright without one."""
    assert "project" in pyproject()
    assert pyproject()["project"]["name"]


def test_uv_does_not_try_to_build_the_app_as_a_package():
    """It is an application; there is no packaging metadata for uv to use."""
    assert pyproject()["tool"]["uv"]["package"] is False


def test_the_postgres_driver_ships_to_production():
    """Vercel runs Postgres; without psycopg the app cannot start."""
    assert any(d.startswith("psycopg") for d in pyproject()["project"]["dependencies"])
