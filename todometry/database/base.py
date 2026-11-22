from dotenv import load_dotenv

from todometry.core.runtime_env import runtime_env_lock
from todometry.database.db_activity import DatabaseActivity
from todometry.database.db_labels import DatabaseLabels
from todometry.database.db_projects import DatabaseProjects
from todometry.core.utils import Anonymizable
from loguru import logger


class Database(DatabaseActivity, DatabaseProjects, DatabaseLabels, Anonymizable):
    def __init__(self, dotenv_path: str, *, api_key: str | None = None):
        if api_key is None:
            with runtime_env_lock:
                load_dotenv(dotenv_path, override=True)
        super().__init__(api_key=api_key)

    @property
    def anonimizable_subdatabases(self):
        """
        Returns a list of sub-databases that are anonymizable.
        """
        return list(
            filter(lambda x: hasattr(x, "anonymize_sub_db"), Database.__bases__)
        )

    def _anonymize(
        self, project_mapping: dict[str, str], label_mapping: dict[str, str]
    ):
        """
        Anonymizes project and label names in the database.
        """
        for bs in self.anonimizable_subdatabases:
            logger.debug(f"Anonymizing {bs.__name__}...")
            assert hasattr(bs, "anonymize_sub_db"), f"{bs.__name__} is not anonymizable"
            bs.anonymize_sub_db(self, project_mapping, label_mapping=label_mapping)  # type: ignore[arg-type]
