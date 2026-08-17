"""
Liquid Charts (trader.liquidcharts.com) REST API client.

Built against the public docs at:
https://liquid-charts.gitbook.io/liquid-charts-api-docs/

IMPORTANT — things you must confirm with Liquid Charts / your account rep
before relying on this in production:
  1. The exact header name used to pass the session token after login.
     DXtrade-family platforms (which this API matches) typically use
     `Authorization: DXAPI <token>`. This client defaults to that, but
     it is NOT explicitly confirmed in the public docs — verify it with
     a real login response, or ask your Liquid Charts contact.
  2. Whether your account has REST API trading enabled at all — the docs
     describe three integration modes (non-disclosed / gateway / direct)
     and API trading access is usually granted separately from normal
     platform login.
  3. Your `domain` value for login (separate from username/password).

This client NEVER places an order on its own — every trading call here
is invoked explicitly by the caller (the dashboard's confirm button),
never automatically by a background loop.
"""
from __future__ import annotations

import time
import uuid
import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

logger = logging.getLogger("liquidcharts_client")

BASE_URL = "https://api.liquidcharts.com/dxsca-web"

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "STOP"]


class LiquidChartsError(RuntimeError):
    def __init__(self, status_code: int, error_code: Any, description: str):
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        super().__init__(f"[{status_code}] error_code={error_code}: {description}")


@dataclass
class Session:
    token: str
    created_at: float


class LiquidChartsClient:
    def __init__(
        self,
        username: str,
        password: str,
        domain: str,
        account_code: str,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
    ):
        self.username = username
        self.password = password
        self.domain = domain
        self.account_code = account_code
        self.base_url = base_url.rstrip("/")
        self._session: Session | None = None
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def login(self) -> Session:
        resp = self._client.post(
            f"{self.base_url}/login",
            json={
                "username": self.username,
                "domain": self.domain,
                "password": self.password,
            },
        )
        self._raise_for_status(resp)
        data = resp.json()
        # Field name for the token is not nailed down in the public docs
        # (commonly "sessionToken" or "token" on DXtrade-family APIs).
        token = data.get("sessionToken") or data.get("token")
        if not token:
            raise LiquidChartsError(
                resp.status_code, "no_token", f"Login succeeded but no token found in response: {data}"
            )
        self._session = Session(token=token, created_at=time.time())
        logger.info("Logged in to Liquid Charts API")
        return self._session

    def ping(self) -> None:
        self._request("POST", "/ping")

    def logout(self) -> None:
        if self._session is None:
            return
        try:
            self._request("POST", "/logout")
        finally:
            self._session = None

    def _auth_headers(self) -> dict[str, str]:
        if self._session is None:
            self.login()
        assert self._session is not None
        # See module docstring — header name is our best guess based on
        # DXtrade-family conventions and should be verified.
        return {"Authorization": f"DXAPI {self._session.token}"}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers.update(self._auth_headers())
        resp = self._client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        if resp.status_code == 401 and self._session is not None:
            # session expired — re-login once and retry
            self._session = None
            headers.update(self._auth_headers())
            resp = self._client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        self._raise_for_status(resp)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                body = resp.json()
                error_code = body.get("code") or body.get("errorCode")
                description = body.get("message") or body.get("description") or resp.text
            except Exception:
                error_code = None
                description = resp.text
            raise LiquidChartsError(resp.status_code, error_code, description)

    # ------------------------------------------------------------------
    # Reference data
    # ------------------------------------------------------------------
    def find_instrument(self, query: str) -> list[dict]:
        """Search instruments by symbol substring, e.g. 'NAS100'."""
        resp = self._request("GET", "/instruments/query", params={"symbols": query})
        return resp.json()

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_candles(
        self,
        symbol: str,
        candle_type: str = "5m",
        from_time_ms: int | None = None,
        to_time_ms: int | None = None,
        count: int = 200,
    ) -> list[dict]:
        if to_time_ms is None:
            to_time_ms = int(time.time() * 1000)
        if from_time_ms is None:
            # default lookback window sized for a few hundred candles
            span_ms = {
                "m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                "h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
                "d": 86_400_000, "w": 604_800_000, "mo": 2_629_800_000,
            }.get(candle_type, 300_000)
            from_time_ms = to_time_ms - span_ms * count

        payload = {
            "eventTypes": [{
                "type": "Candle",
                "format": "COMPACT",
                "candleType": candle_type,
                "fromTime": from_time_ms,
                "toTime": to_time_ms,
                "count": count,
            }],
            "symbols": [symbol],
        }
        resp = self._request("POST", "/marketdata", json=payload)
        return resp.json()

    def get_quote(self, symbol: str) -> dict:
        payload = {
            "eventTypes": [{"type": "Quote", "format": "COMPACT"}],
            "symbols": [symbol],
        }
        resp = self._request("POST", "/marketdata", json=payload)
        return resp.json()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_positions(self) -> list[dict]:
        resp = self._request("GET", f"/accounts/{self.account_code}/positions")
        return resp.json()

    def get_open_orders(self) -> list[dict]:
        resp = self._request("GET", f"/accounts/{self.account_code}/orders")
        return resp.json()

    def get_portfolio(self) -> dict:
        resp = self._request("GET", f"/accounts/{self.account_code}/portfolio")
        return resp.json()

    # ------------------------------------------------------------------
    # Trading — every method here results in a real order on a real
    # account if called against a live (non-demo) account_code. Nothing
    # in this class calls these automatically; the caller must invoke
    # them explicitly (e.g. from a user-clicked "confirm" button).
    # ------------------------------------------------------------------
    def place_order(
        self,
        instrument: str,
        side: Side,
        quantity: float,
        order_type: OrderType = "MARKET",
        limit_price: float | None = None,
        stop_price: float | None = None,
        tif: str = "GTC",
        client_order_id: str | None = None,
    ) -> dict:
        order_code = client_order_id or f"nas100bot-{uuid.uuid4().hex[:12]}"
        body: dict[str, Any] = {
            "orderCode": order_code,
            "type": order_type,
            "instrument": instrument,
            "quantity": quantity,
            "side": side,
            "positionEffect": "OPEN",
            "tif": tif,
        }
        if order_type == "LIMIT":
            body["limitPrice"] = limit_price
        if order_type == "STOP":
            body["stopPrice"] = stop_price

        resp = self._request(
            "POST", f"/accounts/{self.account_code}/orders", json=body
        )
        return resp.json()

    def close_position(self, position_code: str, instrument: str, side: Side) -> dict:
        order_code = f"nas100bot-close-{uuid.uuid4().hex[:12]}"
        body = {
            "orderCode": order_code,
            "type": "MARKET",
            "instrument": instrument,
            "side": side,
            "positionEffect": "CLOSE",
            "positionCode": position_code,
            "tif": "GTC",
        }
        resp = self._request(
            "POST", f"/accounts/{self.account_code}/orders", json=body
        )
        return resp.json()

    def cancel_order(self, order_code: str) -> None:
        self._request(
            "DELETE", f"/accounts/{self.account_code}/orders/{order_code}"
        )
