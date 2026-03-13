"""Regression tests for packaging non-Python assets."""

from __future__ import annotations

from pathlib import Path


def test_pyproject_includes_approval_template_package_data():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    content = pyproject.read_text()
    assert "[tool.setuptools.package-data]" in content
    assert 'sandclaude = ["templates/*.html"]' in content
