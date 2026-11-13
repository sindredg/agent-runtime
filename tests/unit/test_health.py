"""Both HTTP services answer their health route."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_provider.app import app as fake_provider_app
from runtime.api.app import app as api_app


@pytest.mark.parametrize("app", [api_app, fake_provider_app], ids=["api", "fake-provider"])
def test_healthz_returns_ok(app: FastAPI) -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
