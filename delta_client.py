"""Market data from Delta Exchange India, plus signed reads of the user's own positions."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal

BASE_URL = "https://api.india.delta.exchange"
ATTEMPTS = 3


class DeltaError(Exception):
    """Delta's API could not be reached or returned something unusable."""


class DeltaRejected(DeltaError):
    """Delta clearly refused the request, so an order was not placed."""


def _get(path, params=None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"User-Agent": "delta-strike-alerts", "Accept": "application/json"}
    )
    last_error = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(attempt)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)["result"]
        except (OSError, ValueError, KeyError) as error:
            last_error = error
    raise DeltaError(f"Could not get data from Delta ({path}): {last_error}")


def _error_code(error):
    """Delta's short error code from a rejected request. Never includes the key or secret."""
    try:
        detail = json.load(error).get("error")
    except (OSError, ValueError, AttributeError):
        return "no details"
    return detail.get("code", "no details") if isinstance(detail, dict) else str(detail)


def _signed_request(method, path, body=None):
    """Make one signed request. It is never retried, so an order can't be placed twice."""
    key = os.environ.get("DELTA_API_KEY")
    secret = os.environ.get("DELTA_API_SECRET")
    if not key or not secret:
        raise DeltaError("Add DELTA_API_KEY and DELTA_API_SECRET to .env, then restart the app.")
    payload = json.dumps(body) if body is not None else ""
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{method}{timestamp}{path}{payload}".encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        BASE_URL + path,
        data=payload.encode() if body is not None else None,
        method=method,
        headers={
            "api-key": key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "delta-strike-alerts",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)["result"]
    except urllib.error.HTTPError as error:
        raise DeltaRejected(f"Delta rejected the request: HTTP {error.code}, {_error_code(error)}.") from error
    except (OSError, ValueError, KeyError) as error:
        raise DeltaError(f"No clear reply from Delta: {type(error).__name__}.") from error


def get_positions():
    """All open positions on the account, which can lag by up to 10 seconds."""
    return _signed_request("GET", "/v2/positions/margined")


def get_wallets():
    return _signed_request("GET", "/v2/wallet/balances")


def place_order(order):
    return _signed_request("POST", "/v2/orders", order)


def get_pending_orders(product_id):
    """Orders waiting for a trigger, such as stoplosses, on one contract."""
    return _signed_request("GET", f"/v2/orders?states=pending&product_ids={product_id}")


def cancel_order(order_id, product_id):
    return _signed_request("DELETE", "/v2/orders", {"id": order_id, "product_id": product_id})


def get_order(order_id):
    return _signed_request("GET", f"/v2/orders/{order_id}")


def find_order_by_client_id(client_order_id):
    """The order with this client id, or None if Delta has no such order."""
    try:
        return _signed_request("GET", f"/v2/orders/client_order_id/{client_order_id}")
    except DeltaRejected:
        return None


def get_ltp(symbol):
    """The contract's last traded price, or None if it has none."""
    return _get(f"/v2/tickers/{symbol}").get("close")


def get_best_quotes(symbol):
    """(best bid, best ask) from the option's live order book, with None for an empty side."""
    try:
        book = _get(f"/v2/l2orderbook/{symbol}")
        bids = (Decimal(str(level["price"])) for level in book.get("buy") or [])
        asks = (Decimal(str(level["price"])) for level in book.get("sell") or [])
        return max(bids, default=None), min(asks, default=None)
    except (AttributeError, KeyError, TypeError, ArithmeticError) as error:
        raise DeltaError(f"Delta returned an unreadable order book for {symbol}: {error!r}") from error


def expiry_label(expiry):
    return datetime.strptime(expiry, "%d%m%y").strftime("%d %b %Y")


def get_option_tickers(symbol):
    return _get(
        "/v2/tickers",
        {"contract_types": "call_options,put_options", "underlying_asset_symbols": symbol},
    )


def list_expiries(symbol):
    """Every expiry currently listed for the symbol, nearest first."""
    codes = {ticker["symbol"].rsplit("-", 1)[-1] for ticker in get_option_tickers(symbol)}
    ordered = sorted(codes, key=lambda code: datetime.strptime(code, "%d%m%y"))
    return [{"value": code, "label": expiry_label(code)} for code in ordered]


def get_reference_price(symbol, atm_reference, option_tickers):
    """Spot index price, or the last traded price of the symbol's perpetual future."""
    try:
        if atm_reference == "spot":
            price = float(option_tickers[0]["spot_price"])
        else:
            price = float(_get(f"/v2/tickers/{symbol}USD")["close"])
    except (TypeError, ValueError, KeyError, IndexError) as error:
        raise DeltaError(f"Delta returned no {atm_reference} price for {symbol}: {error}") from error
    if price <= 0:
        raise DeltaError(f"Delta returned no {atm_reference} price for {symbol}.")
    return price
