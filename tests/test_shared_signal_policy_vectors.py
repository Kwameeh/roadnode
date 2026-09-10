import json
from pathlib import Path

from car_telemetry.signal_policy import plan_selection


def test_shared_android_and_pi_signal_policy_vectors():
    path = Path(__file__).parents[2] / "contracts" / "signal-policy" / "v1" / "vectors.json"
    for vector in json.loads(path.read_text(encoding="utf-8")):
        plan = plan_selection(
            vector["supported"], vector["requested"], deselected=vector["deselected"],
            round_trip_seconds=vector["roundTripSeconds"], budget_seconds=vector["budgetSeconds"],
        )
        assert list(plan.selected) == vector["selected"], vector["name"]
        assert list(plan.unavailable) == vector["unavailable"], vector["name"]
        assert list(plan.rejected) == vector["rejected"], vector["name"]
