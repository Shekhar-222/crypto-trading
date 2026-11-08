"""Formats open account positions from Delta into a PnL message."""
from delta_client import DeltaError
from strikes import fmt

# Telegram can't center text and strips leading spaces, so pad with blank braille characters.
TITLE_PADDING = "⠀" * 4


def _number(value):
    return None if value in (None, "") else float(value)


def usd_inr_rate(wallets):
    """Delta's own rate, implied by the USD wallet's balance and its INR value."""
    for wallet in wallets:
        if wallet.get("asset_symbol") == "USD":
            balance = _number(wallet.get("balance"))
            balance_inr = _number(wallet.get("balance_inr"))
            if balance and balance_inr:
                return balance_inr / balance
    raise DeltaError("Can't show PnL in rupees: Delta returned no INR value for a USD balance.")


def rupees(value):
    """Format with Indian digit grouping, such as -₹1,27,500.50."""
    sign = "-" if round(value, 2) < 0 else ""
    whole, decimals = f"{abs(value):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return f"{sign}₹{','.join([*groups, tail])}.{decimals}"


def dollars(value):
    sign = "-" if round(value, 2) < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def unrealized_usd(position):
    """Profit or loss as Delta's positions screen shows it: premium received plus the cost to close.

    Delta's own unrealized_pnl field is not used: on a short option it holds the option's value.
    """
    return float(position["realized_cashflow"]) + float(position["unrealized_cashflow"])


def is_open(position):
    return float(position.get("size") or 0) != 0


def summarize_funds(wallets):
    """The wallets that hold a balance, for the page."""
    return [_summarize_wallet(wallet) for wallet in wallets if float(wallet.get("balance") or 0) != 0]


def _summarize_wallet(wallet):
    balance = float(wallet["balance"])
    blocked = _number(wallet.get("blocked_margin"))
    balance_inr = _number(wallet.get("balance_inr"))
    # Delta has no INR field for blocked margin, so use the rate implied by its balance figures.
    blocked_inr = None if blocked is None or balance_inr is None else blocked * balance_inr / balance
    return {
        "asset": wallet["asset_symbol"],
        "balance": balance,
        "available": _number(wallet.get("available_balance")),
        "blocked": blocked,
        "balance_inr": balance_inr,
        "available_inr": _number(wallet.get("available_balance_inr")),
        "blocked_inr": blocked_inr,
    }


def summarize_positions(positions, inr_rate=None):
    """Open positions and their total unrealized PnL, for the page. INR is None without a rate."""
    rows = [
        {
            "symbol": position["product_symbol"],
            "size": position["size"],
            "entry": _number(position.get("entry_price")),
            "mark": _number(position.get("mark_price")),
            "pnl": unrealized_usd(position),
            "pnl_inr": unrealized_usd(position) * inr_rate if inr_rate else None,
        }
        for position in positions
        if is_open(position)
    ]
    total = sum(row["pnl"] for row in rows)
    return {"rows": rows, "total": total, "total_inr": total * inr_rate if inr_rate else None}


def format_stops(stops):
    """The stoploss orders on a position, or a clear warning if there are none."""
    if stops is None:
        return "unknown"
    if not stops:
        return "⚠️ NONE"
    levels = sorted(stops, key=lambda stop: float(stop["stop_price"]))
    if len(levels) == 1:
        return fmt(float(levels[0]["stop_price"]))
    return ", ".join(f"{fmt(float(stop['stop_price']))} x{stop['size']}" for stop in levels)


def _with_hearts(line, value):
    """Green hearts around a profit, red around a loss, none when it rounds to zero."""
    amount = round(value, 2)
    if amount == 0:
        return line
    heart = "💚" if amount > 0 else "❤️"
    return f"{heart} {line} {heart}"


def _leg_dot(value):
    """Green dot in front of a profitable position, red for a loss, none when it rounds to zero."""
    amount = round(value, 2)
    if amount == 0:
        return ""
    return "🟢 " if amount > 0 else "🔴 "


def account_summary(positions, inr_rate, ltps, stops):
    """(total USD, total INR, position lines) for one account.
    ltps and stops are looked up by symbol, with None where Delta couldn't supply them."""
    total_usd = 0.0
    details = []
    for position in positions:
        total_usd += unrealized_usd(position)
        unrealized = unrealized_usd(position) * inr_rate
        symbol = position["product_symbol"]
        details += [
            "",
            f"{_leg_dot(unrealized)}{symbol}",
            f"Size {position['size']} | Avg Entry {fmt(_number(position.get('entry_price')))}"
            f" | LTP {fmt(_number(ltps.get(symbol)))} | SL {format_stops(stops.get(symbol))}"
            f" | PnL {rupees(unrealized)}",
        ]
    return total_usd, total_usd * inr_rate, details


def _total_line(total_usd, total_inr):
    return _with_hearts(f"Total PnL: {rupees(total_inr)} ({dollars(total_usd)})", total_inr)


def format_positions(positions, inr_rate, ltps, stops):
    total_usd, total_inr, details = account_summary(positions, inr_rate, ltps, stops)
    return "\n".join([f"{TITLE_PADDING}📊 PNL UPDATE📊", "", _total_line(total_usd, total_inr), *details])


# Telegram refuses messages over 4,096 characters, so long updates are split between accounts.
MESSAGE_LIMIT = 3800
DIVIDER = "=" * 25
NAME_PADDING = "⠀" * 6


def format_combined(reports):
    """The Telegram update: one PnL line per account, then each account's positions.
    reports are (name, total USD, total INR, position lines). Returns the messages to send."""
    lines = [f"{_leg_dot(inr)}{name} : {rupees(inr)}  ({dollars(usd)})" for name, usd, inr, _ in reports]
    sections = ["\n".join([f"{NAME_PADDING}👤 {name}", *details]) for name, _, _, details in reports]
    messages = ["\n\n".join([f"{TITLE_PADDING}📊 PNL UPDATE📊", *lines])]
    for section in sections:
        block = f"\n\n{DIVIDER}\n\n{section}"
        if len(messages[-1]) + len(block) + len(DIVIDER) + 2 > MESSAGE_LIMIT:
            messages[-1] += f"\n\n{DIVIDER}"
            messages.append(section)
        else:
            messages[-1] += block
    messages[-1] += f"\n\n{DIVIDER}"
    return messages
