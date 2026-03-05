import pytest
import httpx
from unittest.mock import patch
from synergy_analyser.synergy_client import SynergyClient


@pytest.mark.asyncio
async def test_search_premise_returns_results():
    mock_response = httpx.Response(
        200,
        json=[{"code": "2001747538", "label": "5 Pepper Gr, Byford"}],
        request=httpx.Request("GET", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
        client = SynergyClient()
        results = await client.search_premise("5 Pepper Gr, Byford")
        assert results[0]["code"] == "2001747538"


@pytest.mark.asyncio
async def test_request_email_token_returns_allow_contract():
    mock_response = httpx.Response(
        200,
        json={"message": "token sent"},
        headers={"Allow-Contract": "abc123"},
        request=httpx.Request("POST", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "post", return_value=mock_response):
        client = SynergyClient()
        allow_contract = await client.request_email_token("test@example.com", "123456")
        assert allow_contract == "abc123"


@pytest.mark.asyncio
async def test_login_with_token():
    mock_response = httpx.Response(
        200,
        json={"status": "ok"},
        request=httpx.Request("POST", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "post", return_value=mock_response):
        client = SynergyClient()
        result = await client.login_with_email_token("755553", "abc123")
        assert result is True


@pytest.mark.asyncio
async def test_get_account_number():
    mock_response = httpx.Response(
        200,
        json=[{"contractAccountNumber": "000395347610"}],
        request=httpx.Request("GET", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
        client = SynergyClient()
        account = await client.get_account_number()
        assert account == "000395347610"


@pytest.mark.asyncio
async def test_get_device_id():
    mock_response = httpx.Response(
        200,
        json={"installationDetails": {"intervalDevices": [{"deviceId": "000000000620003997"}]}},
        request=httpx.Request("GET", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
        client = SynergyClient()
        device_id = await client.get_device_id("000395347610")
        assert device_id == "000000000620003997"


@pytest.mark.asyncio
async def test_get_usage_data_returns_expected_keys():
    mock_data = {
        "kwHalfHourlyValues": [0.5] * 48,
        "kwhHalfHourlyValuesGeneration": [0.0] * 48,
        "retrieveIntervalDetails": [{"productCode": "EV Add on"}],
    }
    mock_response = httpx.Response(
        200,
        json=mock_data,
        request=httpx.Request("GET", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
        client = SynergyClient()
        from datetime import date
        data = await client.get_usage_data("000395347610", "000000000620003997", date(2025, 4, 1), date(2025, 4, 30))
        assert "kwHalfHourlyValues" in data
        assert "kwhHalfHourlyValuesGeneration" in data
        assert "retrieveIntervalDetails" in data


@pytest.mark.asyncio
async def test_search_premise_http_error():
    """A 500 response from the API should raise httpx.HTTPStatusError."""
    mock_response = httpx.Response(
        500,
        json={"error": "Internal Server Error"},
        request=httpx.Request("GET", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
        client = SynergyClient()
        with pytest.raises(httpx.HTTPStatusError):
            await client.search_premise("5 Pepper Gr, Byford")


@pytest.mark.asyncio
async def test_request_email_token_missing_header():
    """A 200 response without the Allow-Contract header should raise KeyError."""
    mock_response = httpx.Response(
        200,
        json={"message": "token sent"},
        request=httpx.Request("POST", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "post", return_value=mock_response):
        client = SynergyClient()
        with pytest.raises(KeyError):
            await client.request_email_token("test@example.com", "123456")


@pytest.mark.asyncio
async def test_get_account_number_empty_response():
    """A 200 response with an empty list should raise IndexError."""
    mock_response = httpx.Response(
        200,
        json=[],
        request=httpx.Request("GET", "https://example.com"),
    )
    with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
        client = SynergyClient()
        with pytest.raises(IndexError):
            await client.get_account_number()
