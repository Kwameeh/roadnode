from __future__ import annotations

import json
from pathlib import Path

from car_telemetry.device_identity import allowed_publish_topics
from car_telemetry.emqx_provision import authorization_payload


POLICY = json.loads(
    (Path(__file__).parents[2] / "contracts" / "mqtt" / "v2" / "broker-policy.json").read_text(
        encoding="utf-8"
    )
)


def test_python_publisher_policy_matches_the_shared_frame_only_fixture():
    expected = POLICY["publisher"]["topicTemplate"].format(deviceId="DEV-001")

    assert allowed_publish_topics("DEV-001") == (expected,)
    allow = authorization_payload("DEV-001")["rules"][0]
    assert allow == {
        "permission": "allow",
        "action": "publish",
        "topic": expected,
        "qos": POLICY["publisher"]["qos"],
        "retain": POLICY["publisher"]["retain"],
        "username_re": "\\ADEV-001\\z",
    }
