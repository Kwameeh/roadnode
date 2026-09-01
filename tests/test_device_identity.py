from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from car_telemetry.device_identity import (
    AuthorizationError,
    CredentialError,
    RevokedCredential,
    allowed_publish_topics,
    assert_publish_allowed,
    authorize_publish,
    load_credential,
    provision,
    render_emqx_acl,
    revoke,
    rotate,
    save_credential,
    topic_for,
    verify_secret,
)

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(offset_days: float = 0) -> str:
    return (BASE + timedelta(days=offset_days)).isoformat().replace("+00:00", "Z")


# --- no shared secret -------------------------------------------------------


def test_every_device_gets_a_unique_secret():
    credentials = [provision(f"DEV-{index:03d}") for index in range(25)]

    secrets_issued = {credential.secret for credential in credentials}

    assert len(secrets_issued) == 25, "no secret may ever be shared between devices"
    assert all(len(credential.secret) >= 32 for credential in credentials)


def test_reprovisioning_the_same_device_still_changes_the_secret():
    first = provision("DEV-001")
    second = provision("DEV-001")

    assert first.secret != second.secret


# --- exact namespace ACL ----------------------------------------------------


def test_device_may_publish_only_its_own_channels():
    assert allowed_publish_topics("DEV-001") == (
        "roadnode/v2/devices/DEV-001/frame",
        "roadnode/v2/devices/DEV-001/metadata",
        "roadnode/v2/devices/DEV-001/dtc",
        "roadnode/v2/devices/DEV-001/status",
    )
    for topic in allowed_publish_topics("DEV-001"):
        assert authorize_publish("DEV-001", topic) is True


@pytest.mark.parametrize(
    "topic",
    [
        "roadnode/v2/devices/DEV-002/frame",           # another device
        "roadnode/v2/devices/DEV-0011/frame",          # prefix look-alike
        "roadnode/v2/devices/DEV-001",                 # parent namespace
        "roadnode/v2/devices/DEV-001/frame/extra",     # deeper topic
        "roadnode/v2/devices/DEV-001/+",               # single-level wildcard
        "roadnode/v2/devices/DEV-001/#",               # multi-level wildcard
        "#",
        "roadnode/v2/devices/DEV-001/unknown",         # undeclared channel
        "roadnode/v2/devices/DEV-002/frame",           # another device
        "",
    ],
)
def test_publishing_outside_the_device_namespace_is_denied(topic):
    assert authorize_publish("DEV-001", topic) is False
    with pytest.raises(AuthorizationError):
        assert_publish_allowed("DEV-001", topic)


def test_topic_for_rejects_unknown_channel():
    assert topic_for("DEV-001", "frame") == "roadnode/v2/devices/DEV-001/frame"
    with pytest.raises(ValueError):
        topic_for("DEV-001", "firehose")


def test_generated_broker_acl_matches_the_device_side_model():
    acl = render_emqx_acl(["DEV-001", "DEV-002"])

    for topic in allowed_publish_topics("DEV-001"):
        assert f'{{allow, {{user, "DEV-001"}}, publish, ["{topic}"]}}.' in acl
    assert '{deny, {user, "DEV-001"}, subscribe, ["#"]}.' in acl
    assert '{deny, {user, "DEV-002"}, subscribe, ["#"]}.' in acl
    assert acl.strip().endswith("{deny, all}.")
    # DEV-001 must never be granted DEV-002's topics.
    assert '{allow, {user, "DEV-001"}, publish, ["roadnode/v2/devices/DEV-002/frame"]}.' not in acl


# --- rotation ---------------------------------------------------------------


def test_rotation_keeps_identity_and_advances_version():
    original = provision("DEV-001", issued_at=iso(0))

    rotated = rotate(original, issued_at=iso(30))

    assert rotated.device_id == original.device_id
    assert rotated.username == original.username
    assert rotated.secret != original.secret
    assert rotated.credential_version == original.credential_version + 1
    assert rotated.usable(now=BASE + timedelta(days=30))
    # ACL is identity-scoped, so rotation must not change authorization.
    assert allowed_publish_topics(rotated.device_id) == allowed_publish_topics(
        original.device_id
    )


def test_rotating_a_revoked_credential_is_refused():
    revoked = revoke(provision("DEV-001"))

    with pytest.raises(RevokedCredential):
        rotate(revoked)


# --- revocation (stolen device) ---------------------------------------------


def test_revoked_credential_cannot_authenticate():
    credential = provision("DEV-001")
    assert verify_secret(credential, credential.secret) is True

    stolen = revoke(credential, revoked_at=iso(1))

    assert stolen.revoked is True
    assert stolen.revoked_at == iso(1)
    assert stolen.usable() is False
    assert verify_secret(stolen, stolen.secret) is False, "revoke must win over a correct secret"


def test_revoke_is_idempotent():
    first = revoke(provision("DEV-001"), revoked_at=iso(1))
    second = revoke(first, revoked_at=iso(2))

    assert second.revoked_at == iso(1), "the original revocation time is preserved"


def test_expired_credential_is_unusable():
    credential = provision("DEV-001", issued_at=iso(0), valid_for_days=30)

    assert credential.usable(now=BASE + timedelta(days=10)) is True
    assert credential.expired(now=BASE + timedelta(days=31)) is True
    assert credential.usable(now=BASE + timedelta(days=31)) is False


def test_wrong_secret_is_rejected():
    credential = provision("DEV-001")

    assert verify_secret(credential, "not-the-secret") is False
    assert verify_secret(credential, "") is False


# --- persistence ------------------------------------------------------------


def test_credential_round_trips_through_disk(tmp_path):
    path = tmp_path / "credential.json"
    credential = provision("DEV-001", issued_at=iso(0))

    save_credential(path, credential)
    loaded = load_credential(path)

    assert loaded == credential


def test_loading_rejects_missing_and_malformed_credentials(tmp_path):
    with pytest.raises(CredentialError):
        load_credential(tmp_path / "absent.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(CredentialError):
        load_credential(broken)

    wrong_version = tmp_path / "old.json"
    wrong_version.write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
    with pytest.raises(CredentialError):
        load_credential(wrong_version)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(
        json.dumps({"schemaVersion": 1, "deviceId": "DEV-001"}), encoding="utf-8"
    )
    with pytest.raises(CredentialError):
        load_credential(incomplete)
