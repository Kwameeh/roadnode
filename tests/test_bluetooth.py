import pytest

from car_telemetry.bluetooth import BluetoothCandidate, parse_candidates


def test_parse_bluetooth_candidates_with_channels():
    assert parse_candidates("00:10:CC:4F:36:03@1, EC:46:2C:93:7E:F4") == [
        BluetoothCandidate("00:10:CC:4F:36:03", 1),
        BluetoothCandidate("EC:46:2C:93:7E:F4", None),
    ]


def test_parse_bluetooth_candidates_deduplicates_in_order():
    assert parse_candidates("00:10:cc:4f:36:03@1,00:10:CC:4F:36:03@2") == [
        BluetoothCandidate("00:10:CC:4F:36:03", 1),
    ]


def test_parse_bluetooth_candidates_falls_back_to_legacy_mac():
    assert parse_candidates("", "00:10:CC:4F:36:03", 1) == [
        BluetoothCandidate("00:10:CC:4F:36:03", 1),
    ]


@pytest.mark.parametrize("raw", ["not-a-mac", "00:10:CC:4F:36:03@0", "00:10:CC:4F:36:03@31"])
def test_parse_bluetooth_candidates_rejects_bad_values(raw):
    with pytest.raises(ValueError):
        parse_candidates(raw)
