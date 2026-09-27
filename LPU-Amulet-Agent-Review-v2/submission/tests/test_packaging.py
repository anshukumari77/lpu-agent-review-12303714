"""Package entrypoints are real callable paths, not metadata-only promises."""
import importlib
import tomllib
from pathlib import Path


def test_declared_console_entrypoints_resolve():
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    for command, target in project["project"]["scripts"].items():
        module, symbol = target.split(":")
        assert callable(getattr(importlib.import_module(module), symbol, None)), command


def test_oauth_sdk_minimums_cover_tested_constructor_and_validation_apis():
    from packaging.requirements import Requirement
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    requirements = {item.name: item for item in map(Requirement, project["project"]["dependencies"])}
    assert "2.54.0" in requirements["openai"].specifier
    assert "2.53.0" not in requirements["openai"].specifier
    assert "2.13.5" in requirements["pydantic"].specifier
    assert "2.8.0" not in requirements["pydantic"].specifier
