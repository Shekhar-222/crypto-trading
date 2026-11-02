"""Strike selection: call = ATM + straddle * multiplier, put = ATM - straddle * multiplier."""

STOPLOSS_MULTIPLE = 2


class SignalError(Exception):
    """A signal cannot be produced safely, so no entry strikes should be given."""


def parse_ltp(ticker):
    """Return the last traded price (Delta's `close` field), or None if there isn't one."""
    try:
        price = float(ticker.get("close"))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def select_strikes(tickers, reference_price, multiplier):
    """Pick the strikes to sell from the option tickers of one symbol and expiry."""
    calls = {}
    puts = {}
    for ticker in tickers:
        book = {"call_options": calls, "put_options": puts}.get(ticker.get("contract_type"))
        if book is not None:
            book[float(ticker["strike_price"])] = ticker

    both_sides = calls.keys() & puts.keys()
    if not both_sides:
        raise SignalError("No strike is listed with both a call and a put for this expiry.")

    atm = min(sorted(both_sides), key=lambda strike: abs(strike - reference_price))
    atm_call_ltp = parse_ltp(calls[atm])
    atm_put_ltp = parse_ltp(puts[atm])
    missing = [
        ticker["symbol"]
        for ticker, ltp in ((calls[atm], atm_call_ltp), (puts[atm], atm_put_ltp))
        if ltp is None
    ]
    if missing:
        raise SignalError(f"No LTP for ATM option(s): {', '.join(missing)}.")

    straddle = atm_call_ltp + atm_put_ltp
    offset = straddle * multiplier
    calculated_call = atm + offset
    calculated_put = atm - offset

    # On an exact tie between two strikes, take the one further from ATM (further OTM).
    call_strike = min(calls, key=lambda strike: (abs(strike - calculated_call), -strike))
    put_strike = min(puts, key=lambda strike: (abs(strike - calculated_put), strike))

    return {
        "reference_price": reference_price,
        "multiplier": multiplier,
        "atm_strike": atm,
        "atm_call_ltp": atm_call_ltp,
        "atm_put_ltp": atm_put_ltp,
        "straddle": straddle,
        "offset": offset,
        "call": {
            "calculated": calculated_call,
            "strike": call_strike,
            "symbol": calls[call_strike]["symbol"],
            "ltp": parse_ltp(calls[call_strike]),
            "product_id": calls[call_strike].get("product_id"),
            "tick_size": calls[call_strike].get("tick_size"),
            "adjusted": calculated_call > max(calls),
        },
        "put": {
            "calculated": calculated_put,
            "strike": put_strike,
            "symbol": puts[put_strike]["symbol"],
            "ltp": parse_ltp(puts[put_strike]),
            "product_id": puts[put_strike].get("product_id"),
            "tick_size": puts[put_strike].get("tick_size"),
            "adjusted": calculated_put < min(puts),
        },
    }


def fmt(value):
    if value is None:
        return "n/a"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def format_signal(result, settings, expiry_label, time_label):
    reference_name = "Spot" if settings["atm_reference"] == "spot" else "Futures"
    lines = [
        f"🔔 SELL ENTRY: {settings['symbol']} | Expiry {expiry_label}",
        f"Time: {time_label} IST",
        "",
        f"{reference_name}: {fmt(result['reference_price'])} | ATM: {fmt(result['atm_strike'])}",
        f"Straddle (LTP): {fmt(result['atm_call_ltp'])} + {fmt(result['atm_put_ltp'])}"
        f" = {fmt(result['straddle'])}",
        f"Offset: {fmt(result['straddle'])} x {fmt(result['multiplier'])} = {fmt(result['offset'])}",
    ]
    for name, leg in (("CALL", result["call"]), ("PUT", result["put"])):
        lines += ["", f"SELL {name} {fmt(leg['strike'])} ({leg['symbol']}), LTP {fmt(leg['ltp'])}"]
        if leg["adjusted"]:
            lines.append(f"⚠️ Adjusted: calculated {name.lower()} strike was outside the listed range")
        elif leg["ltp"] is None:
            lines.append("Stoploss: n/a (no LTP)")
        else:
            lines.append(f"Stoploss: {fmt(leg['ltp'] * STOPLOSS_MULTIPLE)}")
    return "\n".join(lines)
