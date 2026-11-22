"""Shared fixtures for API tests."""

from collections.abc import Iterator

from fastapi.testclient import TestClient
import pytest

from todometry.web.api import create_app


@pytest.fixture
def api_client() -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client
