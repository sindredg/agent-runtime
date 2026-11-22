"""Every HTTP service answers its health route."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fake_provider.app import app as fake_provider_app
from fake_receiver.app import app as fake_receiver_app
from fake_tracker.app import app as fake_tracker_app
from runtime.api.app import create_app

# No database is needed: the health route does not use one, and the
# application only connects when it is started.
api_app = create_app()


@pytest.mark.parametrize(
    "app",
    [api_app, fake_provider_app, fake_tracker_app, fake_receiver_app],
    ids=["api", "fake-provider", "fake-tracker", "fake-receiver"],
)
def test_healthz_returns_ok(app: FastAPI) -> None:
    # The API answers only an allowed Host, which the test client's default is not.
    response = TestClient(app, base_url="http://localhost").get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
