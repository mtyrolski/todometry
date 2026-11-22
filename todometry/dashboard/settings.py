import os
from pathlib import Path

from todometry.core.env import EnvVar


def resolve_dashboard_config_path() -> Path:
    override = os.getenv(str(EnvVar.CONFIG_DIR))
    if override:
        return Path(override).expanduser().resolve() / "dashboard.yaml"
    data_dir = os.getenv(str(EnvVar.DATA_DIR))
    if data_dir:
        return Path(data_dir).expanduser().resolve() / "configs" / "dashboard.yaml"
    return Path(__file__).resolve().parents[2] / "configs" / "dashboard.yaml"
