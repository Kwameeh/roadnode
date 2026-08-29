from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import write_json_atomic
from .observations import parse_utc, utc_now

CREDENTIAL_SCHEMA_VERSION = 1
TOPIC_ROOT = "roadnode/v2/devices"
PUBLISHABLE_CHANNELS = ("frame", "metadata", "dtc", "status")
SECRET_BYTES = 32


class CredentialError(RuntimeError):
    pass


class RevokedCredential(CredentialError):
    pass


class AuthorizationError(CredentialError):
    """Raised when a device attempts to publish outside its own namespace."""


@dataclass(frozen=True)
class DeviceCredential:
    device_id: str
    username: str
    secret: str
    credential_version: int
    issued_at: str
    expires_at: str | None
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return current > parse_utc(self.expires_at)

    def usable(self, *, now: datetime | None = None) -> bool:
        return not self.revoked and not self.expired(now=now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": CREDENTIAL_SCHEMA_VERSION,
            "deviceId": self.device_id,
            "username": self.username,
            "secret": self.secret,
            "credentialVersion": self.credential_version,
            "issuedAt": self.issued_at,
            "expiresAt": self.expires_at,
            "revokedAt": self.revoked_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DeviceCredential":
        if raw.get("schemaVersion") != CREDENTIAL_SCHEMA_VERSION:
            raise CredentialError(
                f"unsupported credential schema version: {raw.get('schemaVersion')}"
            )
        try:
            return cls(
                device_id=str(raw["deviceId"]),
                username=str(raw["username"]),
                secret=str(raw["secret"]),
                credential_version=int(raw["credentialVersion"]),
                issued_at=str(raw["issuedAt"]),
                expires_at=raw["expiresAt"],
                revoked_at=raw["revokedAt"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialError("malformed device credential") from exc


def device_namespace(device_id: str) -> str:
    return f"{TOPIC_ROOT}/{device_id}"


def topic_for(device_id: str, channel: str) -> str:
    if channel not in PUBLISHABLE_CHANNELS:
        raise ValueError(f"unsupported channel: {channel}")
    return f"{device_namespace(device_id)}/{channel}"


def allowed_publish_topics(device_id: str) -> tuple[str, ...]:
    return tuple(topic_for(device_id, channel) for channel in PUBLISHABLE_CHANNELS)


def authorize_publish(device_id: str, topic: str) -> bool:
    """Exact-namespace check mirroring the broker ACL.

    A device may publish only to its own namespace. Wildcards, parent topics,
    traversal, and prefix look-alikes (``DEV-0011`` against ``DEV-001``) are
    all denied.
    """
    if not device_id.strip() or not topic:
        return False
    if "+" in topic or "#" in topic:
        return False
    return topic in allowed_publish_topics(device_id)


def assert_publish_allowed(device_id: str, topic: str) -> None:
    if not authorize_publish(device_id, topic):
        raise AuthorizationError(
            f"device {device_id} may not publish to {topic}"
        )


def provision(
    device_id: str,
    *,
    valid_for_days: int | None = 365,
    issued_at: str | None = None,
    credential_version: int = 1,
) -> DeviceCredential:
    """Mint a unique credential. Every device gets its own random secret."""
    if not device_id.strip():
        raise ValueError("device_id must not be empty")
    issued = issued_at or utc_now()
    expires = None
    if valid_for_days is not None:
        if valid_for_days <= 0:
            raise ValueError("valid_for_days must be positive")
        expiry = parse_utc(issued) + timedelta(days=valid_for_days)
        expires = expiry.isoformat().replace("+00:00", "Z")
    return DeviceCredential(
        device_id=device_id,
        username=device_id,
        secret=secrets.token_urlsafe(SECRET_BYTES),
        credential_version=credential_version,
        issued_at=issued,
        expires_at=expires,
        revoked_at=None,
    )


def rotate(
    credential: DeviceCredential,
    *,
    valid_for_days: int | None = 365,
    issued_at: str | None = None,
) -> DeviceCredential:
    """Issue a new secret for the same device identity.

    The broker identity (``deviceId``/``username``) and therefore the ACL are
    unchanged; only the secret and version move forward.
    """
    if credential.revoked:
        raise RevokedCredential(
            f"cannot rotate revoked credential for {credential.device_id}"
        )
    rotated = provision(
        credential.device_id,
        valid_for_days=valid_for_days,
        issued_at=issued_at,
        credential_version=credential.credential_version + 1,
    )
    if rotated.secret == credential.secret:  # pragma: no cover - 256-bit collision
        raise CredentialError("rotation produced a duplicate secret")
    return rotated


def revoke(
    credential: DeviceCredential, *, revoked_at: str | None = None
) -> DeviceCredential:
    """Permanently disable a credential, e.g. for a stolen device."""
    if credential.revoked:
        return credential
    return replace(credential, revoked_at=revoked_at or utc_now())


def verify_secret(credential: DeviceCredential, presented: str) -> bool:
    """Constant-time secret check that always fails for unusable credentials."""
    if not credential.usable():
        return False
    return hmac.compare_digest(credential.secret, presented)


def save_credential(path: str | Path, credential: DeviceCredential) -> None:
    target = Path(path).expanduser()
    write_json_atomic(str(target), credential.to_dict())
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - filesystem without POSIX modes
        pass


def load_credential(path: str | Path) -> DeviceCredential:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise CredentialError(f"no device credential at {resolved}")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError("unreadable device credential") from exc
    return DeviceCredential.from_dict(raw)


def render_emqx_acl(device_ids: list[str]) -> str:
    """Generate the broker ACL from the same model the device enforces.

    Each device may publish only to its own exact topics and may never
    subscribe to fleet traffic.
    """
    lines = [
        "%% Generated from car_telemetry.device_identity - do not edit by hand.",
    ]
    for device_id in device_ids:
        for topic in allowed_publish_topics(device_id):
            lines.append(f'{{allow, {{user, "{device_id}"}}, publish, ["{topic}"]}}.')
        lines.append(f'{{deny, {{user, "{device_id}"}}, subscribe, ["#"]}}.')
    lines.append("{deny, all}.")
    return "\n".join(lines) + "\n"
