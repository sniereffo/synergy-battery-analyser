import json
from datetime import date

import pytest

from synergy_analyser.aura import (
    build_apex_action,
    build_chartdata_input,
    build_ip_action,
    parse_ip_result,
)


def test_build_chartdata_input_matches_captured_payload():
    # Ground truth: the exact input the portal sent for 2026-08-02 (half-hourly).
    got = build_chartdata_input(
        service_id="001TESTSERVICE0001",
        start=date(2026, 8, 2),
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
    got = build_chartdata_input(service_id="001X", start=date(2026, 1, 5))
    assert got["StartDate"] == "20260105"
    assert got["EndDate"] == "20260105"
    assert got["UnbilledStartDate"] == "2026-01-05"
    assert got["UnbilledEndDate"] == "2026-01-05"


def test_build_chartdata_input_span():
    # A multi-day span returns half-hourly for the whole range in one request.
    got = build_chartdata_input(service_id="001X", start=date(2026, 1, 1), end=date(2026, 1, 31))
    assert got["StartDate"] == "20260101"
    assert got["EndDate"] == "20260131"
    assert got["PeriodStartDate"] == "2026-01-01"
    assert got["PeriodEndDate"] == "2026-01-31"
    assert got["IntervalType"] == "DAILY"
    assert got["Interval"] == "X"


def test_build_ip_action_wraps_integration_procedure():
    action = build_ip_action(
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


def test_build_apex_action_direct_controller():
    # getLinkedServices is a direct @AuraEnabled controller call (no vlocity wrapper).
    action = build_apex_action(
        namespace="",
        classname="CommunityServiceController",
        method="getLinkedServices",
        params={"contactId": None, "accountId": None},
        request_id=3,
    )
    assert action["id"] == "3;a"
    assert action["descriptor"] == "aura://ApexActionController/ACTION$execute"
    p = action["params"]
    assert p["namespace"] == ""
    assert p["classname"] == "CommunityServiceController"
    assert p["method"] == "getLinkedServices"
    assert p["params"] == {"contactId": None, "accountId": None}
    assert p["cacheable"] is False


def test_parse_ip_result_unwraps_json_string_return_value():
    inner = {"IPResult": {"ChartData": {"Response": []}}}
    body = {"actions": [{"state": "SUCCESS",
                         "returnValue": {"returnValue": json.dumps(inner)}}]}
    assert parse_ip_result(body) == inner


def test_parse_ip_result_direct_controller_return_value():
    body = {"actions": [{"state": "SUCCESS", "returnValue": {"ActiveServices": []}}]}
    assert parse_ip_result(body) == {"ActiveServices": []}


@pytest.mark.parametrize("body", [{}, {"actions": []}, {"actions": None}])
def test_parse_ip_result_no_actions_raises(body):
    with pytest.raises(RuntimeError, match="no actions"):
        parse_ip_result(body)


def test_parse_ip_result_error_state_raises_with_error():
    body = {"actions": [{"state": "ERROR", "error": [{"message": "Apex blew up"}]}]}
    with pytest.raises(RuntimeError, match="Apex blew up"):
        parse_ip_result(body)


def test_parse_ip_result_truncates_huge_errors():
    huge = "x" * 10_000
    with pytest.raises(RuntimeError) as err:
        parse_ip_result({"actions": [{"state": "ERROR", "error": [{"message": huge}]}]})
    msg = str(err.value)
    assert len(msg) < 400
    assert msg.endswith("...[truncated]")

    with pytest.raises(RuntimeError) as err:
        parse_ip_result({"actions": [], "event": huge})
    assert len(str(err.value)) < 400


@pytest.mark.parametrize("action", [
    {"state": "SUCCESS"},
    {"state": "SUCCESS", "error": [], "huge": "x" * 10_000},
    None,
    "not-a-dict",
])
def test_parse_ip_result_missing_return_value_raises(action):
    with pytest.raises(RuntimeError, match="no returnValue") as err:
        parse_ip_result({"actions": [action]})
    assert len(str(err.value)) < 400
