"""What the lab leaves open while Gitea is up.

Gitea is published on 127.0.0.1 with an admin login, and it runs next to the
stack. Its admin password is new on each run, and it shares no network with
Postgres, the API or the fakes. A separate network stops name lookups only:
whether Docker blocks a connection by address between two networks depends on
the engine (docs/lab-demo.md).
"""

import json
import os

import pytest

from .conftest import ADMIN, COMPOSE, Lab, run

# The admin password the lab used before issue 133. It is in the repository's
# history, so it must not open the instance.
OLD_DEFAULT = "lab-admin-dev"
LAB_NETWORK = "agent-runtime_lab"
STACK_NETWORK = "agent-runtime_default"


def networks(service: str) -> set[str]:
    """The Docker networks of a service's running containers."""
    found: set[str] = set()
    for container in run(*COMPOSE, "ps", "--quiet", service).split():
        out = run("docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container)
        found |= set(json.loads(out))
    return found


@pytest.mark.skipif(
    bool(os.environ.get("LAB_GITEA_PASSWORD")), reason="the operator set the admin password"
)
def test_the_old_default_admin_password_is_refused(lab: Lab) -> None:
    response = lab.gitea.get("/user", auth=(ADMIN, OLD_DEFAULT))
    assert response.status_code == 401
    # The run's own password works: the refusal is about the password.
    assert lab.gitea.get("/user").status_code == 200


def test_gitea_shares_a_network_with_the_workers_only(lab: Lab) -> None:
    lab.workers_up()
    assert networks("gitea") == {LAB_NETWORK}
    assert networks("worker") == {STACK_NETWORK, LAB_NETWORK}
    for service in ["postgres", "api", "fake-provider", "fake-tracker"]:
        assert LAB_NETWORK not in networks(service), service


def test_the_lab_workers_deny_scopes_both_networks(lab: Lab) -> None:
    """The guard's deny list, not Docker, keeps a scoped tool away from Gitea."""
    lab.workers_up()
    for container in run(*COMPOSE, "ps", "--quiet", "worker").split():
        value = run("docker", "exec", container, "printenv", "SANDBOX_DENY_NETWORKS").strip()
        assert set(value.split(",")) == {STACK_NETWORK, LAB_NETWORK}
