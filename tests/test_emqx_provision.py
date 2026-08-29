from __future__ import annotations

from typing import Any

import pytest

from car_telemetry.device_identity import allowed_publish_topics, provision, revoke
from car_telemetry.emqx_provision import (
    AUTHN_ID,
    AUTHZ_PATH,
    EmqxError,
    EmqxProvisioner,
    HttpResponse,
    authentication_payload,
    authorization_payload,
    diff_expected_rules,
)


class FakeEmqx:
    """Records every call so the exact provisioning sequence is testable."""

    def __init__(self, *, existing_user: bool = False, existing_rules: bool = False):
        self.calls: list[tuple[str, str, Any]] = []
        self.existing_user = existing_user
        self.existing_rules = existing_rules
        self.status_overrides: dict[str, int] = {}

    def request(self, method: str, path: str, *, payload: Any = None) -> HttpResponse:
        self.calls.append((method, path, payload))
        key = f"{method} {path}"
        if key in self.status_overrides:
            return HttpResponse(self.status_overrides[key])
        if method == "POST" and path.endswith("/users") and "authentication" in path:
            return HttpResponse(409 if self.existing_user else 201)
        if method == "POST" and path == AUTHZ_PATH:
            return HttpResponse(409 if self.existing_rules else 204)
        if method == "GET":
            return HttpResponse(200, {"username": "DEV-001", "rules": []})
        return HttpResponse(204)

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.calls if method is None or m == method]


@pytest.fixture
def credential():
    return provision("DEV-001")


# --- payload shape ----------------------------------------------------------


def test_each_device_becomes_its_own_broker_user(credential):
    payload = authentication_payload(credential)

    assert payload["user_id"] == "DEV-001"
    assert payload["password"] == credential.secret
    assert payload["is_superuser"] is False, "a device is never a superuser"


def test_broker_rules_mirror_the_device_side_model():
    payload = authorization_payload("DEV-001")

    allows = [
        rule["topic"]
        for rule in payload["rules"]
        if rule["permission"] == "allow" and rule["action"] == "publish"
    ]
    assert tuple(allows) == allowed_publish_topics("DEV-001")


def test_rules_deny_all_subscription():
    rules = authorization_payload("DEV-001")["rules"]

    assert any(
        r["permission"] == "deny" and r["action"] == "subscribe" and r["topic"] == "#"
        for r in rules
    ), "a device must never read fleet traffic"


def test_rules_never_grant_another_device(credential):
    payload = authorization_payload("DEV-001")

    for rule in payload["rules"]:
        assert "DEV-002" not in rule["topic"]


# --- apply ------------------------------------------------------------------


def test_apply_installs_rules_before_the_credential(credential):
    broker = FakeEmqx()

    EmqxProvisioner(broker).apply(credential)

    paths = broker.paths()
    # Rules must exist before the credential can authenticate.
    assert paths.index(AUTHZ_PATH) < next(
        index for index, p in enumerate(paths) if "authentication" in p
    )


def test_apply_updates_an_existing_credential_in_place(credential):
    broker = FakeEmqx(existing_user=True)

    EmqxProvisioner(broker).apply(credential)

    puts = [(m, p, body) for m, p, body in broker.calls if m == "PUT"]
    assert any("authentication" in p for _, p, _ in puts)
    assert any(body.get("password") == credential.secret for _, _, body in puts if body)


def test_rotation_replaces_the_secret_without_changing_rules(credential):
    from car_telemetry.device_identity import rotate

    rotated = rotate(credential)
    broker = FakeEmqx(existing_user=True, existing_rules=True)

    EmqxProvisioner(broker).apply(rotated)

    assert authorization_payload("DEV-001")["rules"] == authorization_payload("DEV-001")["rules"]
    assert any(
        body.get("password") == rotated.secret
        for m, p, body in broker.calls
        if m == "PUT" and "authentication" in p and body
    )


def test_apply_refuses_a_revoked_credential(credential):
    """A revoked secret must not be reinstated by re-running provisioning."""
    broker = FakeEmqx()

    with pytest.raises(EmqxError, match="unusable"):
        EmqxProvisioner(broker).apply(revoke(credential))

    assert broker.calls == [], "nothing may reach the broker"


def test_apply_raises_on_a_broker_error(credential):
    broker = FakeEmqx()
    broker.status_overrides[f"POST {AUTHZ_PATH}"] = 500

    with pytest.raises(EmqxError, match="authorization rules"):
        EmqxProvisioner(broker).apply(credential)


# --- revoke -----------------------------------------------------------------


def test_revoke_removes_credential_rules_and_live_session():
    broker = FakeEmqx()

    EmqxProvisioner(broker).revoke("DEV-001")

    deletes = broker.paths("DELETE")
    assert f"/authentication/{AUTHN_ID}/users/DEV-001" in deletes
    assert f"{AUTHZ_PATH}/DEV-001" in deletes
    # Without the disconnect, an already-connected stolen device keeps going.
    assert "/clients/DEV-001" in deletes


def test_revoke_is_idempotent_when_nothing_exists():
    broker = FakeEmqx()
    broker.status_overrides = {
        f"DELETE /authentication/{AUTHN_ID}/users/DEV-001": 404,
        f"DELETE {AUTHZ_PATH}/DEV-001": 404,
        "DELETE /clients/DEV-001": 404,
    }

    EmqxProvisioner(broker).revoke("DEV-001")


def test_revoke_raises_on_an_unexpected_error():
    broker = FakeEmqx()
    broker.status_overrides = {f"DELETE /authentication/{AUTHN_ID}/users/DEV-001": 500}

    with pytest.raises(EmqxError):
        EmqxProvisioner(broker).revoke("DEV-001")


# --- drift detection --------------------------------------------------------


def test_audit_reports_what_the_broker_enforces():
    broker = FakeEmqx()

    report = EmqxProvisioner(broker).audit("DEV-001")

    assert report["deviceId"] == "DEV-001"
    assert report["credentialPresent"] is True
    assert report["rulesPresent"] is True


def test_no_drift_for_correctly_installed_rules():
    assert diff_expected_rules("DEV-001", authorization_payload("DEV-001")) == []


def test_drift_detects_a_missing_publish_rule():
    payload = authorization_payload("DEV-001")
    payload["rules"] = [r for r in payload["rules"] if not r["topic"].endswith("/frame")]

    problems = diff_expected_rules("DEV-001", payload)

    assert any("missing publish allow" in p and "frame" in p for p in problems)


def test_drift_detects_an_over_broad_rule():
    """The A1 failure mode: the broker granting more than the model allows."""
    payload = authorization_payload("DEV-001")
    payload["rules"].append(
        {"permission": "allow", "action": "publish", "topic": "roadnode/v2/devices/DEV-002/frame"}
    )

    problems = diff_expected_rules("DEV-001", payload)

    assert any("unexpected publish allow" in p and "DEV-002" in p for p in problems)


def test_drift_detects_a_missing_subscribe_denial():
    payload = authorization_payload("DEV-001")
    payload["rules"] = [r for r in payload["rules"] if r["action"] != "subscribe"]

    assert "missing subscribe denial" in diff_expected_rules("DEV-001", payload)


def test_drift_reports_absent_rules():
    assert diff_expected_rules("DEV-001", None) == ["no rules installed"]
    assert diff_expected_rules("DEV-001", {"rules": "nonsense"}) == [
        "rules payload is malformed"
    ]
