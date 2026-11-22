"""Shared helpers for web API endpoint tests."""

import pandas as pd


def _single_event_df() -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "date": "2025-01-15",
                "id": "e1",
                "title": "t1",
                "type": "completed",
                "parent_project_name": "A",
                "root_project_name": "A",
                "task_id": "1",
            }
        ]
    )
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")
