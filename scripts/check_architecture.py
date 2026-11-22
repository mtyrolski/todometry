"""Report source size and check web architecture boundaries."""

import ast
import tokenize
import tomllib
from collections import Counter
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_MODULES = {"todometry.web.api", "todoist.web.api"}


def _globals_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "globals"
        and not node.args
        and not node.keywords
    )


def inspect_python(path: Path, relative: Path) -> Counter[str]:
    """Check imports back to the composition root and known injection mechanisms."""
    with path.open("rb") as stream:
        counts = Counter(
            {
                "sync": sum(
                    t.type == tokenize.NAME
                    and t.string in {"_sync_api_globals", "_call_dashboard_runtime"}
                    for t in tokenize.tokenize(stream.readline)
                )
            }
        )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(relative.parent.parts)
    nodes = list(ast.walk(tree))
    aliases = {"web_api"}
    module_loaders = {"__import__", "importlib.import_module"}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            if module in API_MODULES and not (
                relative.as_posix() == "todometry/launcher.py"
                and module == "todometry.web.api"
                and [item.name for item in node.names] == ["app"]
            ):
                counts["api_import"] += 1
            for item in node.names:
                if f"{module}.{item.name}" in API_MODULES:
                    counts["api_import"] += 1
                    aliases.add(item.asname or item.name)
                if module == "importlib" and item.name == "import_module":
                    module_loaders.add(item.asname or item.name)
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name in API_MODULES:
                    counts["api_import"] += 1
                    aliases.add(item.asname or item.name)
                if item.name == "importlib" and item.asname:
                    module_loaders.add(f"{item.asname}.import_module")

    def api_module(node: ast.AST) -> bool:
        return (
            isinstance(node, (ast.Name, ast.Attribute)) and ast.unparse(node) in aliases
        )

    for node in nodes:
        if isinstance(node, ast.Subscript) and _globals_call(node.value):
            counts["globals_subscript"] += 1
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and _globals_call(node.func.value)
        ):
            counts["globals_update"] += 1
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "vars"
            and len(node.args) == 1
            and api_module(node.args[0])
        ):
            counts["api_namespace"] += 1
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__dict__"
            and api_module(node.value)
        ):
            counts["api_namespace"] += 1
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in API_MODULES
            and ast.unparse(node.func) in module_loaders
        ):
            counts["api_import"] += 1
    if relative.parts[1:2] == ("web",) and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__getattr__"
        for node in tree.body
    ):
        counts["catch_all_export"] += 1
    return counts


def check_ruff(config: dict) -> list[str]:
    ruff = config.get("tool", {}).get("ruff", {})
    errors = []
    forbidden = {"F", "F821", "ALL"}
    # Ruff also accepts these keys in its older [tool.ruff] layout.
    for section_name, section in (("ruff", ruff), ("ruff.lint", ruff.get("lint", {}))):
        for key in ("ignore", "extend-ignore"):
            if forbidden.intersection(section.get(key, [])):
                errors.append(f"global {section_name}.{key} suppresses undefined names")
        for key in ("per-file-ignores", "extend-per-file-ignores"):
            for pattern, rules in section.get(key, {}).items():
                for rule in forbidden.intersection(rules):
                    errors.append(f"Ruff undefined-name exemption: {pattern}: {rule}")
    return errors


def check_tree(root: Path) -> list[str]:
    errors = []
    paths = [
        path
        for namespace in ("todoist", "todometry")
        for path in (root / namespace).rglob("*.py*")
        if path.suffix in {".py", ".pyi"} and path.is_file()
    ]
    for path in sorted(paths):
        relative = path.relative_to(root)
        for kind, count in inspect_python(path, relative).items():
            if kind == "api_import":
                errors.append(f"import of API composition root in {relative}")
            elif kind == "catch_all_export":
                errors.append(f"catch-all web export in {relative}")
            elif count:
                errors.append(
                    f"runtime namespace injection in {relative}: {kind}: {count} occurrences"
                )
    with (root / "pyproject.toml").open("rb") as stream:
        errors.extend(check_ruff(tomllib.load(stream)))
    return errors


def report_size(root: Path) -> None:
    groups = {
        "python": (root / "todometry", {".py"}),
        "frontend": (root / "frontend" / "app", {".ts", ".tsx", ".css"}),
        "android": (root / "android" / "app" / "src" / "main", {".kt", ".java"}),
    }
    total_files = total_lines = 0
    for name, (directory, extensions) in groups.items():
        files = [
            p for p in directory.rglob("*") if p.is_file() and p.suffix in extensions
        ]
        lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files)
        total_files += len(files)
        total_lines += lines
        print(f"{name}: {len(files)} files, {lines} lines")
    with (root / "pyproject.toml").open("rb") as stream:
        dependencies = tomllib.load(stream)["project"]["dependencies"]
    print(
        f"total: {total_files} files, {total_lines} lines; {len(dependencies)} direct runtime dependencies"
    )


def main() -> int:
    report_size(ROOT)
    errors = check_tree(ROOT)
    if errors:
        print("Architecture check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Architecture check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
