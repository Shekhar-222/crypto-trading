"""Places the sell orders for an entry signal and makes sure each one gets a stoploss.

Normally the stoploss is a bracket held by Delta. Delta allows one bracket per open position and
resizes it to the whole position, so adding to a held strike sells without a bracket. After the
fill, watch_order gives the new lots their own stop and replaces the bracket with a stop for the
older lots, so each trade keeps its own level. Until then the bracket still covers everything.
"""
from decimal import ROUND_CEILING, Decimal

import delta_client
from delta_client import DeltaError, DeltaRejected
from strikes import STOPLOSS_MULTIPLE, fmt

STOP_LIMIT_BUFFER = Decimal("1.1")
TRIGGER_METHOD = "mark_price"
MAX_SIZE = 1000
RETRY_SECONDS = 30
BRACKET_RESIZE_WAIT_SECONDS = 10
BRACKET_RESIZE_TRIES = 6
MIDPOINT_ABOVE_SPREAD = Decimal("3")
ASK_ABOVE_SPREAD = Decimal("4")


class SkipTrade(Exception):
    """No order should be placed for this signal."""


def _round_up(value, tick):
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _text(value):
    return format(value.normalize(), "f")


def choose_limit(best_bid, best_ask, tick):
    """Return (price, label): wider spreads wait for a better price than the best bid."""
    spread = best_ask - best_bid
    if spread > ASK_ABOVE_SPREAD:
        return best_ask, f"best ask, spread {_text(spread)}"
    if spread > MIDPOINT_ABOVE_SPREAD:
        return _round_up((best_bid + best_ask) / 2, tick), f"midpoint, spread {_text(spread)}"
    return best_bid, f"best bid, spread {_text(spread)}"


def stop_levels(price, tick):
    """The stop-limit for a sold option: trigger at 2x the price, limit 10% above the trigger."""
    stop = _round_up(price * STOPLOSS_MULTIPLE, tick)
    return stop, _round_up(stop * STOP_LIMIT_BUFFER, tick)


def build_order(leg, limit_price, size, client_order_id, separate_stop=False):
    """Sell at the limit, with a bracket stop-limit unless the stop will be placed after the fill."""
    order = {
        "product_id": leg["product_id"],
        "product_symbol": leg["symbol"],
        "size": size,
        "side": "sell",
        "order_type": "limit_order",
        "limit_price": _text(limit_price),
        "time_in_force": "gtc",
        "client_order_id": client_order_id,
    }
    if not separate_stop:
        stop, stop_limit = stop_levels(limit_price, Decimal(str(leg["tick_size"])))
        order.update(
            bracket_stop_loss_price=_text(stop),
            bracket_stop_loss_limit_price=_text(stop_limit),
            bracket_stop_trigger_method=TRIGGER_METHOD,
        )
    return order


def plan_orders(result, size, tag):
    """Return ([(name, leg, order, price label, separate stop)], notes), or raise SkipTrade before anything is placed.

    An adjusted strike drops only that side, so the other side can still trade on its own.
    """
    legs = (("CALL", result["call"]), ("PUT", result["put"]))
    tradable = [(name, leg) for name, leg in legs if not leg["adjusted"]]
    if not tradable:
        raise SkipTrade("calculated call and put strikes were both outside the listed range.")
    notes = [
        f"⚠️ {name} NOT TRADED: calculated {name.lower()} strike was outside the listed range."
        for name, leg in legs
        if leg["adjusted"]
    ]
    held_short = {
        position["product_symbol"]
        for position in delta_client.get_positions()
        if float(position.get("size") or 0) < 0
    }
    plan = []
    for name, leg in tradable:
        if not leg.get("tick_size") or not leg.get("product_id"):
            raise SkipTrade(f"Delta returned no contract details for {leg['symbol']}.")
        best_bid, best_ask = delta_client.get_best_quotes(leg["symbol"])
        if best_bid is None or best_ask is None:
            side = "bids" if best_bid is None else "asks"
            raise SkipTrade(f"{leg['symbol']} has no {side} in the order book, so its spread is unknown.")
        price, label = choose_limit(best_bid, best_ask, Decimal(str(leg["tick_size"])))
        separate = leg["symbol"] in held_short
        order = build_order(leg, price, size, f"{tag}{name[0]}", separate_stop=separate)
        plan.append((name, leg, order, label, separate))
    return plan, notes


def _describe(name, leg, order, label, separate):
    entry = f"SELL {name} {fmt(leg['strike'])}: limit {order['limit_price']} ({label}), size {order['size']}"
    if separate:
        return f"{entry}\nAdding to a held strike: the app places a separate SL after each fill."
    return (
        f"{entry}\n"
        f"SL trigger {order['bracket_stop_loss_price']} (mark), limit {order['bracket_stop_loss_limit_price']}"
    )


def _watch_item(name, leg, order, response):
    return {
        "order_id": response["id"],
        "symbol": leg["symbol"],
        "product_id": leg["product_id"],
        "tick_size": str(leg["tick_size"]),
        "label": f"{name} {fmt(leg['strike'])}",
        "limit": order["limit_price"],
        "protected": 0,
        "warned": False,
        "uncertain": None,
        "next_try": 0,
    }


def place_trade(result, size, tag):
    """Place both sides and return (message section, orders to watch). A failed POST is never retried."""
    try:
        plan, notes = plan_orders(result, size, tag)
    except (SkipTrade, DeltaError) as reason:
        return f"⚠️ TRADE SKIPPED: {reason} No orders were placed.", []

    sections = []
    watch = []
    placed = 0
    for name, leg, order, label, separate in plan:
        try:
            response = delta_client.place_order(order)
        except DeltaRejected as error:
            sections.append(f"⚠️ SELL {name} {fmt(leg['strike'])} NOT PLACED: {error}")
            continue
        except DeltaError as error:
            sections.append(
                f"⚠️ SELL {name} {fmt(leg['strike'])} UNKNOWN: {error} "
                "It may have gone through, so check your Delta orders."
                + (" If it did, it has NO STOPLOSS, so set one by hand." if separate else "")
            )
            continue
        placed += 1
        text = f"{_describe(name, leg, order, label, separate)}\nOrder {response.get('id')}, {response.get('state')}"
        if separate and response.get("id"):
            watch.append(_watch_item(name, leg, order, response))
        elif separate:
            text += "\n⚠️ NO STOPLOSS: Delta gave no order id to watch, so set one by hand now."
        elif not response.get("bracket_stop_loss_price"):
            text += "\n⚠️ NO STOPLOSS CONFIRMED BY DELTA: check this order now."
        sections.append(text)

    sections += notes
    if placed and notes:
        sections.append(f"This trade holds only the {plan[0][0].lower()} side.")
    if placed == len(plan):
        return "ORDERS PLACED\n\n" + "\n\n".join(sections), watch
    return "⚠️ NOT ALL ORDERS WERE PLACED: check Delta now.\n\n" + "\n\n".join(sections), watch


def _warn_once(item, now, text):
    """Retry later, and only report the problem the first time."""
    item["next_try"] = now + RETRY_SECONDS
    if item["warned"]:
        return []
    item["warned"] = True
    return [text]


def _stop_order(item, fill_price, size, client_order_id):
    stop, stop_limit = stop_levels(fill_price, Decimal(item["tick_size"]))
    return {
        "product_id": item["product_id"],
        "product_symbol": item["symbol"],
        "size": size,
        "side": "buy",
        "order_type": "limit_order",
        "limit_price": _text(stop_limit),
        "stop_order_type": "stop_loss_order",
        "stop_price": _text(stop),
        "stop_trigger_method": TRIGGER_METHOD,
        "reduce_only": True,
        "client_order_id": client_order_id,
    }


def _split_bracket(item, filled, other_order_ids, now):
    """Replace the position-wide bracket with a stop for the older lots. Returns messages.

    The new stop goes in first and the bracket is cancelled last, so the older lots are never
    left without a stop. Looking the stop up by client id means a retry can't place it twice.
    """
    bracket = next((o for o in delta_client.get_pending_orders(item["product_id"]) if o.get("bracket_order")), None)
    if bracket is None:
        item["split_done"] = True
        return []
    others_filled = 0
    for order_id in other_order_ids:
        other = delta_client.get_order(order_id)
        others_filled += int(other["size"]) - int(other["unfilled_size"])
    older_size = int(bracket["size"]) - filled - others_filled
    if older_size <= 0:
        # Delta may not have resized the bracket yet. It still protects its own lots meanwhile.
        item["split_tries"] = item.get("split_tries", 0) + 1
        if item["split_tries"] < BRACKET_RESIZE_TRIES:
            item["next_try"] = now + BRACKET_RESIZE_WAIT_SECONDS
            return []
        return _warn_once(
            item, now,
            f"⚠️ CHECK STOPLOSSES: {item['symbol']} bracket covers {bracket['size']} lots, "
            f"so the older lots couldn't be worked out. The bracket stays in place.",
        )

    client_order_id = f"SB{bracket['id']}"
    if not delta_client.find_order_by_client_id(client_order_id):
        delta_client.place_order({
            "product_id": item["product_id"],
            "product_symbol": item["symbol"],
            "size": older_size,
            "side": "buy",
            "order_type": "limit_order",
            "limit_price": bracket["limit_price"],
            "stop_order_type": "stop_loss_order",
            "stop_price": bracket["stop_price"],
            "stop_trigger_method": bracket.get("stop_trigger_method") or TRIGGER_METHOD,
            "reduce_only": True,
            "client_order_id": client_order_id,
        })
    delta_client.cancel_order(bracket["id"], item["product_id"])
    item["split_done"] = True
    item["warned"] = False
    return [
        f"🛡️ SEPARATE STOPLOSSES: {item['label']} ({item['symbol']})\n"
        f"Older {older_size} lots now have their own SL: trigger {bracket['stop_price']} (mark), "
        f"limit {bracket['limit_price']}\nThe position-wide bracket was removed."
    ]


def watch_order(item, now, other_order_ids=()):
    """Place a stoploss for any new fill. Returns (finished, messages) and updates item in place.

    now is in seconds. A stop that got no clear reply is looked up by client id before any retry,
    so the same fill never gets two stops.
    """
    if now < item["next_try"]:
        return False, []
    no_stop = f"⚠️ NO STOPLOSS YET: SELL {item['label']} ({item['symbol']}) may be unprotected."
    try:
        order = delta_client.get_order(item["order_id"])
        filled = int(order["size"]) - int(order["unfilled_size"])
        fill_price = Decimal(str(order.get("average_fill_price") or item["limit"]))

        if item["uncertain"]:
            if delta_client.find_order_by_client_id(item["uncertain"]["client_order_id"]):
                item["protected"] = item["uncertain"]["filled"]
            item["uncertain"] = None
    except DeltaError as error:
        return False, _warn_once(item, now, f"{no_stop} {error} Retrying, but set one by hand if it stays open.")
    except (ArithmeticError, KeyError, TypeError, ValueError) as error:
        return False, _warn_once(item, now, f"{no_stop} Unexpected order data: {error!r}. Set one by hand.")

    messages = []
    new_size = filled - item["protected"]
    if new_size > 0:
        client_order_id = f"SL{item['order_id']}F{filled}"
        stop_order = _stop_order(item, fill_price, new_size, client_order_id)
        try:
            response = delta_client.place_order(stop_order)
        except DeltaRejected as error:
            return False, _warn_once(item, now, f"{no_stop} {error} Retrying, but set one by hand now.")
        except DeltaError as error:
            item["uncertain"] = {"client_order_id": client_order_id, "filled": filled}
            return False, _warn_once(item, now, f"{no_stop} {error} Checking again, but look at your Delta orders.")
        item["protected"] = filled
        item["warned"] = False
        messages.append(
            f"🛡️ STOPLOSS PLACED: {item['label']} ({item['symbol']})\n"
            f"Filled {new_size} at {_text(fill_price)}\n"
            f"SL trigger {stop_order['stop_price']} (mark), limit {stop_order['limit_price']}, "
            f"size {new_size}\nOrder {response.get('id')}"
        )

    if filled and not item.get("split_done"):
        try:
            messages += _split_bracket(item, filled, other_order_ids, now)
        except DeltaError as error:
            messages += _warn_once(
                item, now, f"⚠️ CHECK STOPLOSSES: {item['symbol']} could not be split yet: {error} "
                "Retrying. Look at your Delta stop orders."
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as error:
            messages += _warn_once(
                item, now, f"⚠️ CHECK STOPLOSSES: unexpected {item['symbol']} order data: {error!r}."
            )
    settled = item["protected"] >= filled and (item.get("split_done") or not filled)
    return order.get("state") in ("closed", "cancelled") and settled, messages
