"""Push device identity into EMQX so the broker actually enforces it.

Closes threat A1. Until this runs, `device_identity` describes an ACL that only
the device and the server check; the broker itself would accept anything it
authenticated. Provisioning writes the credential into EMQX's built-in
authentication database and installs the exact-topic rules alongside it, so a
stolen or revoked credential is refused at the broker rather than downstream.

The HTTP client is injected, so every payload and call sequence is testable
without a live broker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .device_identity import (
    DeviceCredential,
    allowed_publish_topics,
    device_namespace,
)

AUTHN_ID = "password_based:built_in_database"
AUTHZ_PATH = "/authorization/sources/built_in_database/rules/users"


class EmqxError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any = None


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> HttpResponse: ...


def authentication_payload(credential: DeviceCredential) -> dict[str, Any]:
    """One EMQX built-in-database user per device. No shared secret exists."""
    return {
        "user_id": credential.username,
        "password": credential.secret,
        "is_superuser": False,
    }


def authorization_payload(device_id: str) -> dict[str, Any]:
    """Exact-topic publish rules plus an explicit subscribe denial.

    Mirrors `allowed_publish_topics` so the broker and the device enforce the
    same list. The trailing deny-all makes the intent explicit even though
    EMQX is configured with `no_match = deny`.
    """
    rules: list[dict[str, str]] = [
        {"permission": "allow", "action": "publish", "topic": topic}
        for topic in allowed_publish_topics(device_id)
    ]
    # A device must never read fleet traffic, including its own namespace.
    rules.append({"permission": "deny", "action": "subscribe", "topic": "#"})
    rules.append(
        {"permission": "deny", "action": "all", "topic": f"{device_namespace(device_id)}/#"}
    )
    return {"username": device_id, "rules": rules}


def _expect(response: HttpResponse, *ok: int, action: str) -> HttpResponse:
    if response.status not in ok:
        raise EmqxError(
            f"{action} failed with status {response.status}: {json.dumps(response.body, default=str)}"
        )
    return response


class EmqxProvisioner:
    """Applies and revokes device access on the broker."""

    def __init__(self, client: HttpClient):
        self._client = client

    def apply(self, credential: DeviceCredential) -> None:
        """Install or update one device's credential and rules.

        Refuses to provision a credential that is already unusable, so a
        revoked or expired secret cannot be reinstated by re-running the tool.
        """
        if not credential.usable():
            raise EmqxError(
                f"refusing to provision unusable credential for {credential.device_id}"
            )

        device_id = credential.device_id
        # Authorization first: a credential that authenticates before its rules
        # exist would be denied by `no_match = deny` anyway, but installing
        # rules first removes the window entirely.
        rules = authorization_payload(device_id)
        created = self._client.request("POST", AUTHZ_PATH, payload=[rules])
        if created.status == 409:
            # Rules already exist; replace them so the broker matches the model.
            _expect(
                self._client.request("PUT", f"{AUTHZ_PATH}/{device_id}", payload=rules),
                200,
                204,
                action=f"authorization update for {device_id}",
            )
        else:
            _expect(created, 200, 201, 204, action=f"authorization rules for {device_id}")

        response = self._client.request(
            "POST",
            f"/authentication/{AUTHN_ID}/users",
            payload=authentication_payload(credential),
        )
        if response.status == 409:
            # Already present: rotation replaces the secret in place so the
            # device identity and its rules are untouched.
            _expect(
                self._client.request(
                    "PUT",
                    f"/authentication/{AUTHN_ID}/users/{credential.username}",
                    payload={"password": credential.secret, "is_superuser": False},
                ),
                200,
                204,
                action=f"credential update for {device_id}",
            )
            return
        _expect(response, 200, 201, 204, action=f"credential create for {device_id}")

    def revoke(self, device_id: str) -> None:
        """Remove a stolen device's access at the broker.

        Deletes the credential and its rules, then disconnects any live
        session: without the kick, an already-connected stolen device would
        keep publishing until it happened to reconnect.
        """
        _expect(
            self._client.request(
                "DELETE", f"/authentication/{AUTHN_ID}/users/{device_id}"
            ),
            200,
            204,
            404,
            action=f"credential delete for {device_id}",
        )
        _expect(
            self._client.request("DELETE", f"{AUTHZ_PATH}/{device_id}"),
            200,
            204,
            404,
            action=f"authorization delete for {device_id}",
        )
        _expect(
            self._client.request("DELETE", f"/clients/{device_id}"),
            200,
            204,
            404,
            action=f"disconnect for {device_id}",
        )

    def audit(self, device_id: str) -> dict[str, Any]:
        """Read back what the broker actually enforces for one device."""
        credential = self._client.request(
            "GET", f"/authentication/{AUTHN_ID}/users/{device_id}"
        )
        rules = self._client.request("GET", f"{AUTHZ_PATH}/{device_id}")
        return {
            "deviceId": device_id,
            "credentialPresent": credential.status == 200,
            "rulesPresent": rules.status == 200,
            "rules": rules.body if rules.status == 200 else None,
        }


def diff_expected_rules(device_id: str, actual: Any) -> list[str]:
    """Report where the broker's rules disagree with the model.

    Drift here means the broker is enforcing something other than what the
    device and server assume, which is exactly the A1 failure mode.
    """
    problems: list[str] = []
    if not isinstance(actual, dict):
        problems.append("no rules installed")
        return problems

    installed = actual.get("rules")
    if not isinstance(installed, list):
        problems.append("rules payload is malformed")
        return problems

    allowed = {
        rule.get("topic")
        for rule in installed
        if isinstance(rule, dict)
        and rule.get("permission") == "allow"
        and rule.get("action") == "publish"
    }
    expected = set(allowed_publish_topics(device_id))

    for topic in sorted(expected - allowed):
        problems.append(f"missing publish allow: {topic}")
    for topic in sorted(allowed - expected):
        problems.append(f"unexpected publish allow: {topic}")

    denies_subscribe = any(
        isinstance(rule, dict)
        and rule.get("permission") == "deny"
        and rule.get("action") == "subscribe"
        and rule.get("topic") == "#"
        for rule in installed
    )
    if not denies_subscribe:
        problems.append("missing subscribe denial")
    return problems
