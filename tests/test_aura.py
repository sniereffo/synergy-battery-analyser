import json
from datetime import date

from synergy_analyser.aura import build_chartdata_input, build_apex_action


def test_build_chartdata_input_matches_captured_payload():
    # Ground truth: the exact input the portal sent for 2026-08-02 (half-hourly).
    got = build_chartdata_input(
        service_id="001TESTSERVICE0001",
        day=date(2026, 8, 2),
        unbilled_start=date(2026, 8, 6),
        unbilled_end=date(2026, 8, 14),
    )
    expected = {
        "StartDate": "20260802",
        "EndDate": "20260802",
        "ServiceId": "001TESTSERVICE0001",
        "IntervalType": "DAILY",
        "ChartType": "INTERVAL_DATA",
        "Interval": "X",
        "Daily": "",
        "Monthly": "",
        "DisplayOptionValue": "Daily",
        "Device": [],
        "PeriodStartDate": "2026-08-02",
        "PeriodEndDate": "2026-08-02",
        "PreviousMeters": [],
        "UnbilledStartDate": "2026-08-06",
        "UnbilledEndDate": "2026-08-14",
        "AmiMeterCount": 1,
        "OtherStartDate": None,
        "OtherEndDate": None,
    }
    assert got == expected


def test_build_chartdata_input_defaults_unbilled_to_day():
    got = build_chartdata_input(service_id="001X", day=date(2026, 1, 5))
    assert got["StartDate"] == "20260105"
    assert got["UnbilledStartDate"] == "2026-01-05"
    assert got["UnbilledEndDate"] == "2026-01-05"


def test_build_apex_action_wraps_integration_procedure():
    action = build_apex_action(
        ip_method="MyAccount_ChartData",
        ip_input={"StartDate": "20260802"},
        request_id=7,
    )
    assert action["id"] == "7;a"
    assert action["descriptor"] == "aura://ApexActionController/ACTION$execute"
    p = action["params"]
    assert p["namespace"] == "vlocity_cmt"
    assert p["classname"] == "BusinessProcessDisplayController"
    assert p["method"] == "GenericInvoke2NoCont"
    assert p["params"]["sMethodName"] == "MyAccount_ChartData"
    assert p["params"]["sClassName"] == "vlocity_cmt.IntegrationProcedureService"
    # input is JSON-encoded, not a nested object
    assert json.loads(p["params"]["input"]) == {"StartDate": "20260802"}
