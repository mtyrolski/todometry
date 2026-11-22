"""Regression coverage for the web architecture checks."""

from pathlib import Path

import pytest

from scripts.check_architecture import check_ruff, check_tree


def _check_source(tmp_path: Path, filename: str, source: str) -> list[str]:
    path = tmp_path / filename
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return check_tree(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "def _sync_api_globals(): pass\n",
        "globals().update({'injected': 1})\n",
        "globals()['injected'] = 1\n",
        "from todometry.web import api as source\nnamespace = vars(source)\n",
        "import todometry.web.api as source\nnamespace = source.__dict__\n",
    ],
)
def test_rejects_new_namespace_injection(tmp_path: Path, source: str) -> None:
    errors = _check_source(tmp_path, "todometry/web/new.py", source)
    assert any("new.py" in error for error in errors)


def test_former_api_bridge_has_no_exemption(tmp_path: Path) -> None:
    errors = _check_source(tmp_path, "todometry/web/api.py", "_sync_api_globals()\n")
    assert any("api.py: sync: 1 occurrences" in error for error in errors)


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("todometry/database/data.py", "from todometry.web.api import app\n"),
        ("todometry/web/routes/new.py", "from ..api import app\n"),
        ("todometry/web/settings.py", "from . import api as root\n"),
        (
            "todometry/features/new.py",
            'from importlib import import_module as load\nload("todometry.web.api")\n',
        ),
        ("todometry/launcher.py", "from todometry.web.api import create_app\n"),
    ],
)
def test_rejects_imports_back_to_the_composition_root(
    tmp_path: Path, filename: str, source: str
) -> None:
    errors = _check_source(tmp_path, filename, source)
    assert errors == [f"import of API composition root in {Path(filename)}"]


def test_allows_the_launcher_entrypoint(tmp_path: Path) -> None:
    assert (
        _check_source(
            tmp_path,
            "todometry/launcher.py",
            "from todometry.web.api import app as api\n",
        )
        == []
    )


@pytest.mark.parametrize("filename", ["api.pyi", "api_components/runtime.py"])
def test_rejects_module_level_catch_all_exports(tmp_path: Path, filename: str) -> None:
    errors = _check_source(
        tmp_path,
        f"todometry/web/{filename}",
        "def __getattr__(name): ...\n",
    )
    assert errors == [f"catch-all web export in {Path('todometry/web') / filename}"]


def test_ruff_rejects_undefined_name_exemptions() -> None:
    def config(lint):
        return {"tool": {"ruff": {"lint": lint}}}

    assert check_ruff(
        config({"per-file-ignores": {"todometry/web/routes/*.py": ["F821"]}})
    )
    assert check_ruff({"tool": {"ruff": {"ignore": ["F821"]}}})
    assert check_ruff(config({"ignore": ["F821"]}))
    assert check_ruff(config({"extend-ignore": ["F"]}))
    assert check_ruff(config({"per-file-ignores": {"*.py": ["F821"]}}))
    assert check_ruff(
        config({"per-file-ignores": {"todometry/web/routes/*.py": ["ALL"]}})
    )
    assert check_ruff(
        config({"extend-per-file-ignores": {"todometry/web/routes/*.py": ["F821"]}})
    )
