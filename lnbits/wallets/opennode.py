import ast
import asyncio
from typing import AsyncGenerator, Optional

import httpx
from loguru import logger
from websockets.client import connect
from lnbits.helpers import url_for
from lnbits.settings import settings

from .base import (
    InvoiceResponse,
    PaymentPendingStatus,
    PaymentResponse,
    PaymentStatus,
    StatusResponse,
    UnsupportedError,
    Wallet,
)
class OpenNodeWallet(Wallet):
    """https://developers.opennode.com/"""

    def __init__(self):
        if not settings.opennode_api_endpoint:
            raise ValueError(
                "cannot initialize OpenNodeWallet: missing opennode_api_endpoint"
            )
        key = (
            settings.opennode_key
            or settings.opennode_admin_key
            or settings.opennode_invoice_key
        )
        if not key:
            raise ValueError(
                "cannot initialize OpenNodeWallet: "
                "missing opennode_key or opennode_admin_key or opennode_invoice_key"
            )
        self.key = key

        self.endpoint = self.normalize_endpoint(settings.opennode_api_endpoint)

        headers = {
            "Authorization": self.key,
            "User-Agent": settings.user_agent,
        }
        self.client = httpx.AsyncClient(base_url=self.endpoint, headers=headers)

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")

    async def status(self) -> StatusResponse:
        try:
            r = await self.client.get("/v1/account/balance", timeout=40)
        except (httpx.ConnectError, httpx.RequestError):
            return StatusResponse(f"Unable to connect to '{self.endpoint}'", 0)

        if r.is_error:
            error_message = r.json()["message"]
            return StatusResponse(error_message, 0)

        data = r.json()["data"]
        # multiply balance by 1000 to get msats balance
        return StatusResponse(None, data["balance"]["BTC"] * 1000)

    async def create_invoice(
        self,
        amount: int,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        **kwargs,
    ) -> InvoiceResponse:
        if description_hash or unhashed_description:
            raise UnsupportedError("description_hash")
        
        
        r = await self.client.post(
            "/v1/charges",
            json={
                "amount": amount,
                "description": memo or "",
                "callback_url": settings.lnbits_baseurl+url_for(endpoint="api/v1/opennode-webhook")
                # "callback_url":'https://bb48-103-157-238-89.ngrok-free.app/api/v1/opennode-webhook'
            },
            timeout=40,
        )

        if r.is_error:
            error_message = r.json()["message"]
            return InvoiceResponse(False, None, None, error_message)

        data = r.json()["data"]
        checking_id = data["id"]
        payment_request = data["lightning_invoice"]["payreq"]
        return InvoiceResponse(True, checking_id, payment_request, None)

    async def pay_invoice(self, bolt11: str, fee_limit_msat: int) -> PaymentResponse:
        r = await self.client.post(
            "/v2/withdrawals",
            json={"type": "ln", "address": bolt11},
            timeout=None,
        )

        if r.is_error:
            error_message = r.json()["message"]
            return PaymentResponse(False, None, None, None, error_message)

        data = r.json()["data"]
        checking_id = data["id"]
        fee_msat = -data["fee"] * 1000

        if data["status"] != "paid":
            return PaymentResponse(None, checking_id, fee_msat, None, "payment failed")

        return PaymentResponse(True, checking_id, fee_msat, None, None)

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        r = await self.client.get(f"/v1/charge/{checking_id}")
        if r.is_error:
            return PaymentPendingStatus()
        data = r.json()["data"]
        statuses = { "paid": True, "expired": None}
        return PaymentStatus(statuses[data.get("status")])

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        r = await self.client.get(f"/v1/withdrawal/{checking_id}")

        if r.is_error:
            return PaymentPendingStatus()

        data = r.json()["data"]
        statuses = {
            "initial": None,
            "pending": None,
            "confirmed": True,
            "error": None,
            "failed": False,
        }
        fee_msat = -data.get("fee") * 1000
        return PaymentStatus(statuses[data.get("status")], fee_msat)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        while settings.lnbits_running:
            try:
                async with connect(
                    settings.lnbits_baseurl.replace('http','ws').replace('https','ws')+url_for('api/v1/ws/opennode_ws'),
                    # extra_headers=[("Authorization", self.headers["Authorization"])],
                ) as ws:
                    logger.info("connected to opennode invoices stream")
                    while settings.lnbits_running:
                        message = await ws.recv()
                    
                        message_dict=ast.literal_eval(message)
                        if (
                            message_dict
                            and message_dict.get("status") == "paid"
                        ):
                            logger.info(
                                f'payment-received: {message_dict["id"]}'
                            )
                            yield message_dict["id"]

            except Exception as exc:
                logger.error(
                    f"lost connection to opennode invoices stream: '{exc}'"
                    "retrying in 5 seconds"
                )
                await asyncio.sleep(5)