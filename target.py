"""Watches Straddle + Target % trades and buys every leg back once profit reaches the target.

The target is a share of the premium received: target = target % x sum(size x sell price).
Profit is judged on the best ask, what buying back would cost right now:
profit = sum(size x (sell price - best ask)). With the same size on both legs this is the
per-lot rule, for example a target of 30% on 135 + 201 = 336 is reached at 100.8 points.

Exits are reduce-only limit buys at the best ask, re-priced until filled. Another strategy may
hold the same contracts in the account, so a new exit order is only sent once the previous one is
confirmed finished, and only for this trade's own remaining size. If the target is never reached,
the trade is held until the contracts expire and Delta settles them.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import delta_client
from delta_client import DeltaError, DeltaRejected
from strikes import fmt

IST = timezone(timedelta(hours=5, minutes=30))
CHECK_SECONDS = 5
REPRICE_SECONDS = 10
RETRY_SECONDS = 30
EXPIRY_HOUR, EXPIRY_MINUTE = 17, 30  # Delta India's options expire at 17:30 IST.
CONTRACT_VALUES = {"BTC": Decimal("0.001"), "ETH": Decimal("0.01")}
FINISHED = ("closed", "cancelled")


def new_trade(watch_items, tag, expiry, target_percent):
    """A trade to watch, built from the entry orders that Delta accepted."""
    return {
        "id": tag,
        "expiry": expiry,
        "target_percent": target_percent,
        "state": "open",
        "legs": [
            {
                "name": item["name"],
                "label": item["label"],
                "symbol": item["symbol"],
                "product_id": item["product_id"],
                "tick_size": item["tick_size"],
                "entry_order_id": item["order_id"],
                "entry_done": False,
                "sold": 0,
                "sell_price": None,
                "closed_before": 0,
                "cost_before": "0",
                "exit_order_id": None,
                "exit_limit": None,
                "exit_placed": 0,
                "exit_count": 0,
                "uncertain": None,
            }
            for item in watch_items
        ],
        "next_try": 0,
        "warned": False,
        "announced": False,
        "profit": None,
        "target": None,
    }


def expires_at(expiry):
    return datetime.strptime(expiry, "%d%m%y").replace(hour=EXPIRY_HOUR, minute=EXPIRY_MINUTE, tzinfo=IST)


def _decimal(value):
    return Decimal(str(value))


def _plain(value):
    """A price as Delta takes it, such as 101.5."""
    return format(value.normalize(), "f")


def _text(value):
    """A price for messages, such as 1,234.5."""
    return fmt(float(value))


def _sold_legs(trade):
    return [leg for leg in trade["legs"] if leg["sold"]]


def _per_lot(total, legs):
    """Totals over contracts, shown per lot when every leg has the same size, as in the strategy's rule."""
    sizes = {leg["sold"] for leg in legs}
    return total / sizes.pop() if len(sizes) == 1 else total


def _size_note(legs):
    sizes = {leg["sold"] for leg in legs}
    return f"size {next(iter(sizes))}" if len(sizes) == 1 else "different sizes, so totals are summed over contracts"


def _update_entry(leg, cancel):
    """Read the entry order's fills. When exiting, cancel what hasn't filled yet."""
    if leg["entry_done"]:
        return
    order = delta_client.get_order(leg["entry_order_id"])
    filled = int(order["size"]) - int(order["unfilled_size"])
    leg["sold"] = filled
    if filled:
        leg["sell_price"] = str(order.get("average_fill_price") or leg["sell_price"])
    if order.get("state") in FINISHED:
        leg["entry_done"] = True
    elif cancel:
        try:
            delta_client.cancel_order(leg["entry_order_id"], leg["product_id"])
        except DeltaRejected:
            pass  # It may have just filled. The next check reads its final state.


def _exit_leg(leg, now):
    """Move one leg's buy-back on by one step. Raises DeltaError on a clear refusal."""
    if leg["uncertain"]:
        found = delta_client.find_order_by_client_id(leg["uncertain"])
        leg["uncertain"] = None
        if found:
            leg["exit_order_id"] = found["id"]
            leg["exit_limit"] = str(found.get("limit_price"))
            leg["exit_placed"] = now

    if leg["exit_order_id"]:
        order = delta_client.get_order(leg["exit_order_id"])
        if order.get("state") not in FINISHED:
            if now - leg["exit_placed"] >= REPRICE_SECONDS:
                _, ask = delta_client.get_best_quotes(leg["symbol"])
                if ask is not None and ask != _decimal(leg["exit_limit"]):
                    try:
                        delta_client.cancel_order(leg["exit_order_id"], leg["product_id"])
                    except DeltaRejected:
                        pass  # It may have just filled. The next check reads its final state.
            return
        filled = int(order["size"]) - int(order["unfilled_size"])
        if filled:
            leg["closed_before"] += filled
            leg["cost_before"] = str(_decimal(leg["cost_before"]) + filled * _decimal(order["average_fill_price"]))
        leg["exit_order_id"] = None

    remaining = leg["sold"] - leg["closed_before"]
    if remaining <= 0:
        return
    _, ask = delta_client.get_best_quotes(leg["symbol"])
    if ask is None:
        return  # No sellers right now. Try again on the next check.
    leg["exit_count"] += 1
    client_order_id = f"TX{leg['entry_order_id']}N{leg['exit_count']}"
    order = {
        "product_id": leg["product_id"],
        "product_symbol": leg["symbol"],
        "size": remaining,
        "side": "buy",
        "order_type": "limit_order",
        "limit_price": _plain(ask),
        "time_in_force": "gtc",
        "reduce_only": True,
        "client_order_id": client_order_id,
    }
    try:
        response = delta_client.place_order(order)
    except DeltaRejected:
        raise
    except DeltaError:
        leg["uncertain"] = client_order_id  # Looked up before anything is sent again.
        return
    leg["exit_order_id"] = response["id"]
    leg["exit_limit"] = order["limit_price"]
    leg["exit_placed"] = now


def _warn_once(trade, now, text):
    trade["next_try"] = now + RETRY_SECONDS
    if trade["warned"]:
        return []
    trade["warned"] = True
    return [text]


def _booked(trade, rate_lookup):
    try:
        inr_rate = rate_lookup() if rate_lookup else None
    except (DeltaError, ArithmeticError, KeyError, TypeError, ValueError):
        inr_rate = None  # Shown in dollars only.
    legs = _sold_legs(trade)
    lines = ["✅ TARGET BOOKED: all legs bought back"]
    total = Decimal(0)
    usd = Decimal(0)
    for leg in legs:
        bought = _decimal(leg["cost_before"]) / leg["closed_before"]
        profit = leg["closed_before"] * (_decimal(leg["sell_price"]) - bought)
        total += profit
        usd += profit * CONTRACT_VALUES.get(leg["symbol"].split("-")[1], Decimal(0))
        lines.append(f"{leg['label']}: sold {_text(_decimal(leg['sell_price']))}, bought back {fmt(float(bought))}")
    lines.append(f"Profit: {fmt(float(_per_lot(total, legs)))} points ({_size_note(legs)})")
    money = f"${float(usd):,.2f}"
    if inr_rate:
        money = f"₹{float(usd) * inr_rate:,.2f} ({money})"
    lines.append(f"Booked: {money}")
    return "\n".join(lines)


def step(trade, now_time, rate_lookup=None):
    """Move a trade on by one check. Returns (finished, messages). Updates trade in place.
    rate_lookup gives the rupee rate, and is only called when a trade is booked."""
    now = now_time.timestamp()
    if now < trade["next_try"]:
        return False, []
    trade["next_try"] = now + CHECK_SECONDS
    messages = []
    try:
        exiting = trade["state"] == "exiting"
        if not exiting and now_time >= expires_at(trade["expiry"]):
            progress = ""
            if trade["profit"] is not None:
                progress = f"\nLast check: profit {trade['profit']} of {trade['target']} points."
            return True, [f"⌛ EXPIRED: the target wasn't reached, so Delta settles the legs at expiry.{progress}"]

        for leg in trade["legs"]:
            _update_entry(leg, cancel=exiting)
        legs = _sold_legs(trade)
        entries_done = all(leg["entry_done"] for leg in trade["legs"])
        if not legs:
            if entries_done:
                return True, ["⚠️ TARGET WATCH STOPPED: no entry order filled, so there is nothing to exit."]
            return False, []

        if not exiting:
            premium = sum(leg["sold"] * _decimal(leg["sell_price"]) for leg in legs)
            target = premium * _decimal(trade["target_percent"]) / 100
            trade["target"] = fmt(float(_per_lot(target, legs)))
            if entries_done and not trade["announced"]:
                trade["announced"] = True
                sold = ", ".join(f"{leg['label']} at {_text(_decimal(leg['sell_price']))}" for leg in legs)
                messages.append(
                    f"🎯 TARGET WATCH: sold {sold} ({_size_note(legs)})\n"
                    f"Premium {fmt(float(_per_lot(premium, legs)))} | Target {fmt(trade['target_percent'])}% = "
                    f"{trade['target']} points\nExits when buying back costs "
                    f"{fmt(float(_per_lot(premium - target, legs)))} or less (best ask)."
                )
            asks = {leg["symbol"]: delta_client.get_best_quotes(leg["symbol"])[1] for leg in legs}
            if any(ask is None for ask in asks.values()):
                trade["warned"] = False
                return False, messages  # No sellers on a leg, so the cost to exit is unknown.
            profit = sum(leg["sold"] * (_decimal(leg["sell_price"]) - asks[leg["symbol"]]) for leg in legs)
            trade["profit"] = fmt(float(_per_lot(profit, legs)))
            if profit < target:
                trade["warned"] = False
                return False, messages
            trade["state"] = "exiting"
            for leg in trade["legs"]:
                _update_entry(leg, cancel=True)
            messages.append(
                f"🎯 TARGET HIT: profit {trade['profit']} points ≥ target {trade['target']}\n"
                "Buying back: " + ", ".join(f"{leg['label']} at {_text(asks[leg['symbol']])}" for leg in legs)
            )

        for leg in _sold_legs(trade):
            _exit_leg(leg, now)
        legs = _sold_legs(trade)
        settled = all(leg["entry_done"] for leg in trade["legs"]) and all(
            not leg["exit_order_id"] and not leg["uncertain"] and leg["closed_before"] >= leg["sold"] for leg in legs
        )
        trade["warned"] = False
        if settled:
            return True, messages + [_booked(trade, rate_lookup)]
        return False, messages
    except DeltaError as error:
        return False, messages + _warn_once(trade, now, f"⚠️ TARGET WATCH PROBLEM: {error} Retrying. Check Delta.")
    except (ArithmeticError, KeyError, TypeError, ValueError) as error:
        return False, messages + _warn_once(trade, now, f"⚠️ TARGET WATCH PROBLEM: unexpected data {error!r}. Retrying.")
