"""
StandX API Methods Extension Module
This module contains trading and query API methods separated from authentication logic.
Provides cleaner separation of concerns - standx_auth.py handles auth, this module handles API calls.
"""

import json
from standx_auth import StandXAuth
from logger import get_logger

logger = get_logger(__name__)


def query_balance(auth: StandXAuth) -> dict:
    """Query unified user balance snapshot"""
    try:
        return auth.make_api_call("/api/query_balance")
    except Exception as e:
        msg = str(e)
        if "status=404" in msg and "user balance not found" in msg:
            logger.info("未找到账户余额记录，返回零值快照（可能尚未入金/转入保证金）。")
            return {
                "isolated_balance": "0",
                "isolated_upnl": "0",
                "cross_balance": "0",
                "cross_margin": "0",
                "cross_upnl": "0",
                "locked": "0",
                "cross_available": "0",
                "balance": "0",
                "upnl": "0",
                "equity": "0",
                "pnl_freeze": "0",
            }
        raise


def query_symbol_price(auth: StandXAuth, symbol: str) -> dict:
    """Public: Query symbol price snapshot (index/mark/last/mid)"""
    return auth.make_api_call("/api/query_symbol_price", params={"symbol": symbol})


def query_positions(auth: StandXAuth, symbol: str = None) -> list:
    """Query user positions (optionally filtered by symbol)"""
    params = {"symbol": symbol} if symbol else None
    result = auth.make_api_call("/api/query_positions", params=params)
    # API returns a list directly
    return result if isinstance(result, list) else []


def new_limit_order(
    auth: StandXAuth,
    symbol: str,
    side: str,
    qty: str,
    price: str,
    time_in_force: str = "gtc",
    reduce_only: bool = False,
    margin_mode: str = None,
    leverage: int = None,
) -> dict:
    """Place a signed limit order (requires body signature).
    
    Args:
        auth: StandXAuth instance
        symbol: Trading pair (e.g., "BTC-USD")
        side: "buy" or "sell"
        qty: Order quantity as decimal string (e.g., "0.01")
        price: Order price as decimal string (e.g., "50000.00")
        time_in_force: "gtc" (default), "ioc", etc.
        reduce_only: If True, only reduce existing position
        margin_mode: Optional margin mode (must match position if provided)
        leverage: Optional leverage (must match position if provided)
    """
    payload = {
        "symbol": symbol,
        "side": side,
        "order_type": "limit",
        "qty": qty,
        "price": price,
        "time_in_force": time_in_force,
        "reduce_only": reduce_only,
    }
    if margin_mode is not None:
        payload["margin_mode"] = margin_mode
    if leverage is not None:
        payload["leverage"] = leverage
    payload_str = json.dumps(payload, separators=(",", ":"))
    headers_extra = auth._body_signature_headers(payload_str)
    return auth.make_api_call(
        "/api/new_order",
        method="POST",
        data=payload,
        headers_extra=headers_extra,
        raw_body=payload_str,
    )


def new_market_order(
    auth: StandXAuth,
    symbol: str,
    side: str,
    qty: str,
    reduce_only: bool = False,
    margin_mode: str = None,
    leverage: int = None,
    time_in_force: str = "ioc",
) -> dict:
    """Place a signed market order (requires body signature).
    
    Args:
        auth: StandXAuth instance
        symbol: Trading pair (e.g., "BTC-USD")
        side: "buy" or "sell"
        qty: Order quantity as decimal string (e.g., "0.01")
        reduce_only: If True, only reduce existing position
        margin_mode: Optional margin mode (must match position if provided)
        leverage: Optional leverage (must match position if provided)
        time_in_force: Market order TIF, defaults to IOC
    """
    payload = {
        "symbol": symbol,
        "side": side,
        "order_type": "market",
        "qty": qty,
        "reduce_only": reduce_only,
        "time_in_force": time_in_force,
    }
    if margin_mode is not None:
        payload["margin_mode"] = margin_mode
    if leverage is not None:
        payload["leverage"] = leverage
    payload_str = json.dumps(payload, separators=(",", ":"))
    headers_extra = auth._body_signature_headers(payload_str)
    return auth.make_api_call(
        "/api/new_order",
        method="POST",
        data=payload,
        headers_extra=headers_extra,
        raw_body=payload_str,
    )


def cancel_order(auth: StandXAuth, order_id: int = None, cl_ord_id: str = None) -> dict:
    """
    Cancel an existing order (requires body signature).
    
    Args:
        auth: StandXAuth instance
        order_id: Order ID to cancel (at least one of order_id or cl_ord_id required)
        cl_ord_id: Client order ID to cancel
        
    Returns:
        Response with code, message, and request_id
    """
    if order_id is None and cl_ord_id is None:
        raise ValueError("At least one of order_id or cl_ord_id is required")
    
    payload = {}
    if order_id is not None:
        payload["order_id"] = order_id
    if cl_ord_id is not None:
        payload["cl_ord_id"] = cl_ord_id
    
    payload_str = json.dumps(payload, separators=(",", ":"))
    headers_extra = auth._body_signature_headers(payload_str)
    return auth.make_api_call(
        "/api/cancel_order",
        method="POST",
        data=payload,
        headers_extra=headers_extra,
        raw_body=payload_str,
    )


def query_order(auth: StandXAuth, order_id: int = None, cl_ord_id: str = None) -> dict:
    """Query order status by order_id or cl_ord_id (at least one required)."""
    params = {}
    if order_id is not None:
        params["order_id"] = order_id
    if cl_ord_id is not None:
        params["cl_ord_id"] = cl_ord_id
    if not params:
        raise ValueError("At least one of order_id or cl_ord_id is required")
    return auth.make_api_call("/api/query_order", params=params)


def query_open_orders(auth: StandXAuth, symbol: str = None, limit: int = None) -> dict:
    """Query all open orders, optionally filtered by symbol."""
    params = {}
    if symbol is not None:
        params["symbol"] = symbol
    if limit is not None:
        params["limit"] = limit
    return auth.make_api_call("/api/query_open_orders", params=params)


def query_orders(auth: StandXAuth, symbol: str = None, status: str = None, limit: int = None) -> dict:
    """Query all orders (open/closed), optionally filtered by symbol/status."""
    params = {}
    if symbol is not None:
        params["symbol"] = symbol
    if status is not None:
        params["status"] = status
    if limit is not None:
        params["limit"] = limit
    return auth.make_api_call("/api/query_orders", params=params)
