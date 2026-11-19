"""Request FORMAT builders for the pre-live checks (Phase 7 step 20; design v2.1 §11 "실거래 전 단계").

Produces the request shapes (method, path, params) for Binance USDⓈ-M testnet orders and Upbit's order
dry-run endpoint `/v1/orders/test`. Nothing here signs or sends: signing needs the venue API key, which is
not on this machine. The paths and parameter names below are taken from the design document's references
and are marked for confirmation against the official docs at rehearsal time (`unverified`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    base_url: str
    path: str
    params: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    needs_signature: bool = True
    unverified: bool = True  # confirm path/params against the official docs before any real call


BINANCE_UM_TESTNET = "https://testnet.binancefuture.com"
UPBIT = "https://api.upbit.com"


def binance_um_test_order(
    symbol: str, side: Side, qty: float, client_id: str, *, reduce_only: bool = False
) -> HttpRequest:
    """Market order on the USDⓈ-M testnet (POST /fapi/v1/order). Testnet only: the base URL is fixed."""
    if qty <= 0:
        raise ValueError("qty must be > 0")
    params = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
        "newClientOrderId": client_id,
        "newOrderRespType": "RESULT",
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return HttpRequest("POST", BINANCE_UM_TESTNET, "/fapi/v1/order", params)


def binance_um_countdown_cancel_all(symbol: str, countdown_ms: int) -> HttpRequest:
    """Venue-side dead-man timer (design §11, endpoint family unverified)."""
    return HttpRequest(
        "POST",
        BINANCE_UM_TESTNET,
        "/fapi/v1/countdownCancelAll",
        {"symbol": symbol, "countdownTime": str(countdown_ms)},
    )


def upbit_order_test(
    market: str, side: Side, *, volume: float | None, price: float | None, identifier: str
) -> HttpRequest:
    """Upbit has no testnet: `/v1/orders/test` validates an order without placing it (design §11)."""
    if len(identifier) > 64:
        raise ValueError("Upbit identifier is at most 64 characters")
    ord_type = "limit" if volume is not None and price is not None else ("price" if side == "buy" else "market")
    params = {
        "market": market,
        "side": "bid" if side == "buy" else "ask",
        "ord_type": ord_type,
        "identifier": identifier,
    }
    if volume is not None:
        params["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")
    if price is not None:
        params["price"] = f"{price:.8f}".rstrip("0").rstrip(".")
    return HttpRequest("POST", UPBIT, "/v1/orders/test", params)


def upbit_cancel_all() -> HttpRequest:
    """Bulk cancel (design §11: once per 2 s)."""
    return HttpRequest("DELETE", UPBIT, "/v1/orders/open", {})
