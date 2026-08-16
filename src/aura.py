"""Construct Salesforce Aura RPC payloads for the Synergy portal.

The portal talks to `/s/sfsites/aura` with form-encoded requests whose `message`
field is JSON describing a list of `actions`. Business data is fetched by
invoking a Vlocity Integration Procedure through the
`BusinessProcessDisplayController.GenericInvoke2NoCont` Apex action. These
builders reproduce that shape exactly (verified against captured traffic).
"""

import json
from datetime import date


def build_chartdata_input(
    service_id: str,
    day: date,
    unbilled_start: date | None = None,
    unbilled_end: date | None = None,
) -> dict:
    """Input for `MyAccount_ChartData` requesting one day of half-hourly data.

    IntervalType=DAILY + Interval="X" + ChartType=INTERVAL_DATA yields the 48
    half-hourly rows. Unbilled dates come from `MyAccount_HeaderDataAPI`; they
    do not gate historical data, so they default to the requested day.
    """
    unbilled_start = unbilled_start or day
    unbilled_end = unbilled_end or day
    ymd = day.strftime("%Y%m%d")
    iso = day.strftime("%Y-%m-%d")
    return {
        "StartDate": ymd,
        "EndDate": ymd,
        "ServiceId": service_id,
        "IntervalType": "DAILY",
        "ChartType": "INTERVAL_DATA",
        "Interval": "X",
        "Daily": "",
        "Monthly": "",
        "DisplayOptionValue": "Daily",
        "Device": [],
        "PeriodStartDate": iso,
        "PeriodEndDate": iso,
        "PreviousMeters": [],
        "UnbilledStartDate": unbilled_start.strftime("%Y-%m-%d"),
        "UnbilledEndDate": unbilled_end.strftime("%Y-%m-%d"),
        "AmiMeterCount": 1,
        "OtherStartDate": None,
        "OtherEndDate": None,
    }


def build_service_info_input(service_id: str) -> dict:
    """Input for `MyAccount_ServiceAccountInformation`."""
    return {"ServiceId": service_id}


def build_apex_action(
    namespace: str,
    classname: str,
    method: str,
    params: dict,
    request_id: int,
) -> dict:
    """Build a generic Aura ApexAction execute action for any @AuraEnabled method."""
    return {
        "id": f"{request_id};a",
        "descriptor": "aura://ApexActionController/ACTION$execute",
        "callingDescriptor": "UNKNOWN",
        "params": {
            "namespace": namespace,
            "classname": classname,
            "method": method,
            "params": params,
            "cacheable": False,
            "isContinuation": False,
        },
    }


def build_ip_action(ip_method: str, ip_input: dict, request_id: int) -> dict:
    """Build an action that invokes a Vlocity Integration Procedure via
    BusinessProcessDisplayController.GenericInvoke2NoCont."""
    return build_apex_action(
        namespace="vlocity_cmt",
        classname="BusinessProcessDisplayController",
        method="GenericInvoke2NoCont",
        params={
            "input": json.dumps(ip_input),
            "options": "{}",
            "sClassName": "vlocity_cmt.IntegrationProcedureService",
            "sMethodName": ip_method,
        },
        request_id=request_id,
    )


def build_message(actions: list[dict]) -> str:
    """Serialise actions into the Aura `message` form field."""
    return json.dumps({"actions": actions})


def parse_ip_result(aura_response: dict) -> dict:
    """Pull the Integration Procedure's IPResult out of an Aura response.

    The Aura response is `{"actions":[{"returnValue":{"returnValue":"<json str>",
    ...}}]}`; the inner returnValue is a JSON string wrapping `{"IPResult": ...}`.
    """
    if not aura_response.get("actions"):
        raise RuntimeError(f"Aura response had no actions: {aura_response!r}")
    action = aura_response["actions"][0]
    if action.get("state") and action["state"] != "SUCCESS":
        raise RuntimeError(f"Aura action failed: {action.get('error')}")
    rv = action["returnValue"]
    # IP calls wrap the payload again as returnValue -> a JSON string; direct
    # controller calls may return the object directly. Handle both.
    if isinstance(rv, dict) and "returnValue" in rv:
        inner = rv["returnValue"]
    else:
        inner = rv
    if isinstance(inner, str):
        inner = json.loads(inner)
    return inner
