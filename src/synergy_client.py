import httpx
from datetime import date

BASE_URL = "https://selfserve.synergy.net.au/apps/rest"


class SynergyClient:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    async def search_premise(self, address: str) -> list[dict]:
        response = await self._client.get(
            "/addressSearch/searchPremise.json",
            params={"query": address},
        )
        response.raise_for_status()
        return response.json()

    async def request_email_token(self, email: str, premise_id: str) -> str:
        """Request email OTP. Returns the Allow-Contract header needed for login."""
        response = await self._client.post(
            "/emailLogin/getEmailToken",
            json={"emailAddress": email, "premiseId": premise_id},
        )
        response.raise_for_status()
        return response.headers["Allow-Contract"]

    async def login_with_email_token(self, token: str, allow_contract: str) -> bool:
        response = await self._client.post(
            "/emailLogin/loginWithEmailToken",
            json={"emailToken": token},
            headers={
                "Content-Type": "application/json",
                "Allow-Contract": allow_contract,
            },
        )
        response.raise_for_status()
        return response.status_code == 200

    async def request_sms_code(self, mobile: str, premise_id: str) -> str:
        """Request SMS OTP. Returns the Allow-Contract header needed for login."""
        response = await self._client.post(
            "/session/getSMSCode",
            json={"mobile": mobile, "premiseId": premise_id, "accountNumber": ""},
        )
        response.raise_for_status()
        return response.headers["Allow-Contract"]

    async def login_with_sms_code(self, code: str, allow_contract: str) -> bool:
        response = await self._client.post(
            "/session/loginWithSMSCode.json",
            json={"smsCode": code},
            headers={
                "Content-Type": "application/json",
                "Allow-Contract": allow_contract,
            },
        )
        response.raise_for_status()
        return response.status_code == 200

    async def get_account_number(self) -> str:
        response = await self._client.get("/account/index.json")
        response.raise_for_status()
        data = response.json()
        return data[0]["contractAccountNumber"]

    async def get_device_id(self, account_number: str) -> str:
        response = await self._client.get(f"/account/{account_number}/show.json")
        response.raise_for_status()
        data = response.json()
        return data["installationDetails"]["intervalDevices"][0]["deviceId"]

    async def get_usage_data(
        self,
        account_number: str,
        device_id: str,
        start_date: date,
        end_date: date,
    ) -> dict:
        response = await self._client.get(
            f"/intervalData/{account_number}/getHalfHourlyElecIntervalData",
            params={
                "intervalDeviceIds": device_id,
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
            },
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self._client.aclose()
