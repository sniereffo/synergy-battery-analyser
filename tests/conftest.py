import pytest
from datetime import datetime


@pytest.fixture
def sample_api_response():
    """Realistic API response structure based on actual Synergy API."""
    # 48 slots for 1 day
    return {
        "chartType": "ELEC",
        "startDate": "2025-04-01",
        "endDate": "2025-04-01",
        "kwHalfHourlyValues": (
            [0.3] * 12    # 00:00-06:00
            + [0.5] * 6   # 06:00-09:00
            + [0.1] * 12  # 09:00-15:00
            + [0.8] * 12  # 15:00-21:00
            + [0.4] * 4   # 21:00-23:00
            + [0.3] * 2   # 23:00-00:00
        ),
        "kwhHalfHourlyValuesGeneration": (
            [0.0] * 14         # 00:00-07:00
            + [0.3, 0.6]      # 07:00-08:00
            + [0.8, 1.0]      # 08:00-09:00
            + [1.5, 1.8, 2.0, 2.2, 2.2, 2.0, 1.8, 1.5, 1.2, 0.8]  # 09:00-14:00
            + [0.5, 0.3]      # 14:00-15:00
            + [0.2, 0.1]      # 15:00-16:00
            + [0.0] * 16       # 16:00-00:00
        ),
        "peakKwhHalfHourlyValues": (
            [None] * 30        # 00:00-15:00
            + [0.8] * 12      # 15:00-21:00
            + [None] * 6       # 21:00-00:00
        ),
        "offpeakKwhHalfHourlyValues": (
            [None] * 12        # 00:00-06:00
            + [0.5] * 6       # 06:00-09:00
            + [None] * 24      # 09:00-21:00
            + [0.4] * 4       # 21:00-23:00
            + [None] * 2       # 23:00-00:00
        ),
        "overNightKwhHalfHourlyValues": (
            [0.3] * 12         # 00:00-06:00
            + [None] * 34      # 06:00-23:00
            + [0.3] * 2        # 23:00-00:00
        ),
        "statusCodeHalfHourlyValues": ["Actual"] * 48,
        "retrieveIntervalDetails": [{
            "productCode": "EV Add on",
            "productID": "SER-TRF-EVC02",
            "TOU_type": "TOU",
            "basePeriod": "OFF PEAK(AM)",
            "period2": "SUPER OFF PEAK",
            "period2Hours": "0900-1500",
            "period2Days": "XXXXXXX",
            "period3": "PEAK",
            "period3Hours": "1500-2100",
            "period3Days": "XXXXXXX",
            "period4": "OFF-PEAK",
            "period4Hours": "2100-2300",
            "period4Days": "XXXXXXX",
            "period5": "OVERNIGHT",
            "period5Hours": "2300-0600",
            "period5Days": "XXXXXXX",
        }],
    }


@pytest.fixture
def sample_ev_plan_details():
    """The retrieveIntervalDetails entry for EV Add On."""
    return {
        "productCode": "EV Add on",
        "productID": "SER-TRF-EVC02",
        "TOU_type": "TOU",
        "basePeriod": "OFF PEAK(AM)",
        "period2": "SUPER OFF PEAK",
        "period2Hours": "0900-1500",
        "period2Days": "XXXXXXX",
        "period3": "PEAK",
        "period3Hours": "1500-2100",
        "period3Days": "XXXXXXX",
        "period4": "OFF-PEAK",
        "period4Hours": "2100-2300",
        "period4Days": "XXXXXXX",
        "period5": "OVERNIGHT",
        "period5Hours": "2300-0600",
        "period5Days": "XXXXXXX",
    }


@pytest.fixture
def sample_start_date():
    return datetime(2025, 4, 1)
