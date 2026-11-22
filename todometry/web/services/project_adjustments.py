"""Resolve saved and inferred project adjustment mappings."""

import os
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile

from loguru import logger

from todometry.core.types import Project
from todometry.database.base import Database
from todometry.database.dataframe import (
    DEFAULT_ADJUSTMENTS_FILENAME,
    ProjectAdjustments,
    ProjectHierarchy,
    load_adjustments_file,
    normalize_adjustment_filename,
    render_adjustments_file_content,
    resolve_personal_dir,
)
from todometry.web.api_components.runtime import resolve_env_path


_REMAPPABLE_ACTIVE_ROOT_PROJECTS = frozenset({"Inbox"})


def _project_choices(projects: list[Project]) -> tuple[set[str], set[str]]:
    active_roots: set[str] = set()
    archived_names: set[str] = set()
    for project in projects:
        entry = project.project_entry
        if project.is_archived:
            archived_names.add(entry.name)
        elif not (entry.parent_id or entry.v2_parent_id):
            active_roots.add(entry.name)
    return active_roots, archived_names


def _automatic_mappings(
    projects: dict[str, Project],
    rules: ProjectAdjustments,
    hierarchy: ProjectHierarchy,
) -> dict[str, str]:
    """Project resolved assignments into name rules for the board and Android export."""
    automatic: dict[str, str] = {}
    name_counts = Counter(project.project_entry.name for project in projects.values())
    explicit_names = set(rules.mappings) | set(rules.archived_parents)
    for source_id, project in projects.items():
        source_name = project.project_entry.name
        if (
            not project.is_archived
            or name_counts[source_name] != 1
            or source_name in explicit_names
            or source_id in rules.project_mappings
        ):
            continue
        group = hierarchy.groups[source_id]
        target_id = group.root_id if group.alias is None else None
        target_name = (
            projects[group.root_id].project_entry.name
            if group.alias is None
            else group.alias
        )
        if target_name == source_name or (
            target_id is not None
            and hierarchy.legacy_target_ids.get(target_name) != target_id
        ):
            continue
        automatic[source_name] = target_name
    return dict(sorted(automatic.items()))


def _available_mapping_files() -> list[str]:
    personal_dir = resolve_personal_dir()
    if not personal_dir.exists():
        return [DEFAULT_ADJUSTMENTS_FILENAME]

    mapping_files: list[str] = []
    for file in sorted(personal_dir.iterdir()):
        if not file.is_file() or file.name.startswith("__") or file.suffix != ".py":
            continue
        try:
            normalized = normalize_adjustment_filename(file.name)
            load_adjustments_file(file)
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping invalid project adjustment file {}: {}", file, exc)
            continue
        mapping_files.append(normalized)

    return sorted(mapping_files) if mapping_files else [DEFAULT_ADJUSTMENTS_FILENAME]


def _load_mapping_file(filename: str) -> ProjectAdjustments:
    safe_filename = normalize_adjustment_filename(filename)
    target = resolve_personal_dir() / safe_filename
    return (
        load_adjustments_file(target)
        if target.exists()
        else ProjectAdjustments({}, [], {})
    )


def _save_mapping_file(filename: str, rules: ProjectAdjustments) -> None:
    safe_filename = normalize_adjustment_filename(filename)
    personal_dir = resolve_personal_dir()
    personal_dir.mkdir(parents=True, exist_ok=True)
    target = personal_dir / safe_filename
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=personal_dir, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(render_adjustments_file_content(*rules))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_projects_for_adjustments_sync(
    cached_database: Database | None, refresh: bool
) -> list[Project]:
    dbio = (
        cached_database
        if not refresh and cached_database is not None
        else Database(str(resolve_env_path()))
    )
    return dbio.fetch_projects(include_tasks=False) + dbio.fetch_archived_projects()
