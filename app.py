"""Runs daily option-selling strategies on Delta accounts, with optional Telegram. Run: python app.py.
Each deployment is one strategy on one account, with its own settings."""
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import delta_client
import pnl
import strikes
import target
import telegram_client
import trading
from delta_client import DeltaError
from strikes import SignalError

HOST = "127.0.0.1"
PORT = 8765
KEY_COOKIE = "dashboard_key"
KEY_COOKIE_RULES = "Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
ROOT = Path(__file__).parent
SETTINGS_FILE = ROOT / "settings.json"
LOG_FILE = ROOT / "logs.jsonl"
MAX_LOGS = 100
MAX_ACCOUNTS = 20
MAX_DEPLOYMENTS = 60
IST = timezone(timedelta(hours=5, minutes=30))
SYMBOLS = ("BTC", "ETH")
# The strategies the page offers. Each runs on an account through a deployment.
STRATEGIES = {
    "straddle_stoploss": {
        "name": "Straddle + Stoploss",
        "icon": "🎯",
        "description": "Sells the call and put at ATM ± straddle × multiplier, each with a 2× stoploss.",
        "stoploss": True,
    },
    "straddle_target": {
        "name": "Straddle + Target %",
        "icon": "💰",
        "description": "Same entry, no stoploss. Buys both legs back once profit reaches the target % of "
                       "premium, judged on best ask. Otherwise held to expiry.",
        "stoploss": False,
    },
}
# Each account has its own copy of these.
ACCOUNT_DEFAULTS = {
    "pnl_enabled": False,
    "pnl_interval_minutes": 5,
}
# Each deployment (one strategy on one account) has its own copy. A new one starts with alerts and trading off.
DEPLOYMENT_DEFAULTS = {
    "symbol": "BTC",
    "expiry": "auto:2",  # Automatic: the expiry 2 days after each entry date. Or a fixed date, like 141126.
    "multiplier": 1.8,
    "atm_reference": "spot",
    "entry_time": "09:30",
    "enabled": False,
    "last_fired": "",
    "pinned_message_id": None,
    "trading_enabled": False,
    "trade_size": 1,
    "pending_stops": [],
    "target_percent": 30.0,  # Straddle + Target % only.
    "open_trades": [],  # Straddle + Target % trades being watched for their target.
}
AUTO_EXPIRY_CHOICE = DEPLOYMENT_DEFAULTS["expiry"]
# Settings from before accounts and deployments were separate, for migrating them.
DEFAULTS = {**ACCOUNT_DEFAULTS, **DEPLOYMENT_DEFAULTS}
ALERT_ONLY = "Alert only. No order was placed."

lock = threading.Lock()
logs = deque(maxlen=MAX_LOGS)
settings = {"accounts": [], "deployments": [], "logs_cleared_on": None}
# Per account id. PnL updates stay out of the logs so they can't push entry alerts out of the 100 kept.
pnl_state = {}


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def _copy(value):
    return json.loads(json.dumps(value))


def new_account(name, api_key, api_secret, **values):
    return {
        **_copy(ACCOUNT_DEFAULTS),
        **values,
        "id": secrets.token_hex(4),
        "name": name,
        "api_key": api_key,
        "api_secret": api_secret,
    }


def new_deployment(account_id, strategy, **values):
    return {**_copy(DEPLOYMENT_DEFAULTS), **values, "id": secrets.token_hex(4),
            "account_id": account_id, "strategy": strategy}


def split_account(old):
    """Split an account from before deployments into the account and its Straddle + Stoploss deployment.
    The deployment keeps everything that marks what already ran, so nothing fires twice."""
    account = {k: old[k] for k in ("id", "name", "api_key", "api_secret")}
    account.update({k: old.get(k, _copy(v)) for k, v in ACCOUNT_DEFAULTS.items()})
    deployment = new_deployment(old["id"], "straddle_stoploss",
                                **{k: old[k] for k in DEPLOYMENT_DEFAULTS if k in old})
    deployment["expiry"] = AUTO_EXPIRY_CHOICE  # Fixed dates go stale, so start on the automatic expiry.
    return account, deployment


def migrate(data):
    """Bring older settings up to date. Returns (settings, changed).
    Single-account settings become the first account, keys taken from .env; then each account's
    strategy settings become its Straddle + Stoploss deployment."""
    changed = False
    if "accounts" not in data:
        key = os.environ.get("DELTA_API_KEY", "")
        secret = os.environ.get("DELTA_API_SECRET", "")
        accounts = []
        if key and secret:
            old = {k: data[k] for k in DEFAULTS if k in data}
            accounts.append({**old, "id": secrets.token_hex(4), "name": "Main", "api_key": key, "api_secret": secret})
        data = {"accounts": accounts, "logs_cleared_on": data.get("logs_cleared_on")}
        changed = True
    if "deployments" not in data:
        pairs = [split_account(account) for account in data["accounts"]]
        data = {**data, "accounts": [a for a, _ in pairs], "deployments": [d for _, d in pairs]}
        changed = True
    return data, changed


def load_settings():
    if not SETTINGS_FILE.exists():
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except ValueError as error:
        print(f"Ignoring unreadable {SETTINGS_FILE.name}: {error}")
        return
    data, changed = migrate(data)
    for account in data["accounts"]:
        for key, value in ACCOUNT_DEFAULTS.items():
            account.setdefault(key, _copy(value))
    for deployment in data["deployments"]:
        for key, value in DEPLOYMENT_DEFAULTS.items():
            deployment.setdefault(key, _copy(value))
    settings.update(data)
    if changed:
        save_settings()


def save_settings():
    """Write the file readable by this user only, as it holds API secrets."""
    temp = SETTINGS_FILE.with_suffix(".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(json.dumps(settings, indent=2))
    os.replace(temp, SETTINGS_FILE)


def find_account(account_id):
    """The live account dict, or None. Call with the lock held."""
    return next((a for a in settings["accounts"] if a["id"] == account_id), None)


def account_copy(account_id):
    with lock:
        account = find_account(account_id)
        if account is None:
            raise ValueError("That account no longer exists. Refresh the page.")
        return _copy(account)


def find_deployment(deployment_id):
    """The live deployment dict, or None. Call with the lock held."""
    return next((d for d in settings["deployments"] if d["id"] == deployment_id), None)


def make_job(deployment, account):
    """What one run of a deployment needs: its settings plus the account's name and keys.
    Its id is the deployment's, so results are saved back to the deployment."""
    return _copy({**deployment, "account_id": account["id"], "name": account["name"],
                  "api_key": account["api_key"], "api_secret": account["api_secret"]})


def job_copy(deployment_id):
    with lock:
        deployment = find_deployment(deployment_id)
        account = find_account(deployment["account_id"]) if deployment else None
        if account is None:
            raise ValueError("That deployment no longer exists. Refresh the page.")
        return make_job(deployment, account)


def public_account(account):
    """What the page may see: never the secret, and only the end of the key."""
    shown = {k: v for k, v in account.items() if k not in ("api_key", "api_secret")}
    shown["api_key_hint"] = "…" + account["api_key"][-4:] if account.get("api_key") else ""
    return shown


def signing(account):
    return delta_client.using(account.get("api_key"), account.get("api_secret"))


def load_logs():
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-MAX_LOGS:]:
            try:
                logs.appendleft(json.loads(line))
            except ValueError as error:
                print(f"Skipping unreadable log line: {error}")


def add_log(entry):
    with lock:
        logs.appendleft(entry)
        try:
            with LOG_FILE.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry) + "\n")
        except OSError as error:
            print(f"Could not write {LOG_FILE.name}: {error}")


def logs_tick(now):
    """Erase all logs when the IST date changes, including after the app was off at midnight."""
    today = f"{now:%Y-%m-%d}"
    with lock:
        last_cleared = settings["logs_cleared_on"]
        if last_cleared == today:
            return
        settings["logs_cleared_on"] = today
        save_settings()
        if last_cleared:  # The first run only records the date, so it keeps today's logs.
            logs.clear()
            LOG_FILE.unlink(missing_ok=True)


def validate(body):
    """Return clean settings from a frontend request, or raise ValueError with a readable reason.
    The expiry is automatic (2 days after each entry date) unless a fixed date is chosen."""
    symbol = body.get("symbol")
    if symbol not in SYMBOLS:
        raise ValueError("Symbol must be BTC or ETH.")
    expiry = body.get("expiry") or AUTO_EXPIRY_CHOICE
    if not isinstance(expiry, str) or not re.fullmatch(r"\d{6}", expiry) and expiry != AUTO_EXPIRY_CHOICE:
        raise ValueError("Expiry is not valid.")
    multiplier = body.get("multiplier")
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        raise ValueError("Multiplier must be a number.")
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError("Multiplier must be greater than 0.")
    if body.get("atm_reference") not in ("spot", "futures"):
        raise ValueError("ATM reference must be Spot or Futures.")
    entry_time = body.get("entry_time") or ""
    if not isinstance(entry_time, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", entry_time):
        raise ValueError("Entry time must be in HH:MM format.")
    size = body.get("trade_size", DEFAULTS["trade_size"])
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= trading.MAX_SIZE:
        raise ValueError(f"Order size must be a whole number from 1 to {trading.MAX_SIZE}.")
    target_percent = body.get("target_percent", DEFAULTS["target_percent"])
    if isinstance(target_percent, bool) or not isinstance(target_percent, (int, float)) \
            or not math.isfinite(target_percent) or not 0 < target_percent <= 100:
        raise ValueError("Target must be a percentage above 0 and up to 100.")
    return {
        "target_percent": float(target_percent),
        "symbol": symbol,
        "expiry": expiry,
        "multiplier": float(multiplier),
        "atm_reference": body["atm_reference"],
        "entry_time": entry_time,
        "enabled": body.get("enabled") is True,
        "trading_enabled": body.get("trading_enabled") is True,
        "trade_size": size,
    }


def validate_pnl(body):
    """An account's PnL update settings from the page, or ValueError."""
    interval = body.get("pnl_interval_minutes", DEFAULTS["pnl_interval_minutes"])
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440:
        raise ValueError("PnL interval must be a whole number of minutes from 1 to 1440.")
    return {"pnl_enabled": body.get("pnl_enabled") is True, "pnl_interval_minutes": interval}


def _key_text(value, label):
    value = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"[\x21-\x7e]{8,200}", value):
        raise ValueError(f"Enter the {label} exactly as Delta shows it.")
    return value


def add_account(body):
    """Check the keys with a read-only call to Delta, then save a new account with trading off."""
    name = body.get("name").strip() if isinstance(body.get("name"), str) else ""
    if not 1 <= len(name) <= 30:
        raise ValueError("Account name must be 1 to 30 characters.")
    api_key = _key_text(body.get("api_key"), "API key")
    api_secret = _key_text(body.get("api_secret"), "API secret")
    with lock:
        if len(settings["accounts"]) >= MAX_ACCOUNTS:
            raise ValueError(f"You can have up to {MAX_ACCOUNTS} accounts.")
        if any(a["name"].lower() == name.lower() for a in settings["accounts"]):
            raise ValueError("An account with that name already exists.")
        if any(a["api_key"] == api_key for a in settings["accounts"]):
            raise ValueError("That API key is already added.")
    try:
        with delta_client.using(api_key, api_secret):
            delta_client.get_wallets()
    except DeltaError as error:
        raise ValueError(
            f"Delta didn't accept these keys, so the account wasn't added. {error} "
            "Check the key and secret, and that this server's IP is on the key's allowed IPs."
        ) from error
    account = new_account(name, api_key, api_secret)
    with lock:
        if any(a["name"].lower() == name.lower() for a in settings["accounts"]):
            raise ValueError("An account with that name already exists.")
        settings["accounts"].append(account)
        save_settings()
    return public_account(account)


def remove_account(body):
    """Stop managing an account and its deployments. Its positions and stoplosses on Delta are left as they are."""
    with lock:
        account = find_account(body.get("id"))
        if account is None:
            raise ValueError("That account no longer exists. Refresh the page.")
        deployments = [d for d in settings["deployments"] if d["account_id"] == account["id"]]
        if any(d["pending_stops"] for d in deployments):
            raise ValueError(
                "This account has orders waiting for their stoplosses. Remove it once they fill or are cancelled."
            )
        if any(d["open_trades"] for d in deployments):
            raise ValueError("This account has trades being watched for their target. Remove it once they finish.")
        settings["accounts"].remove(account)
        settings["deployments"] = [d for d in settings["deployments"] if d["account_id"] != account["id"]]
        pnl_state.pop(account["id"], None)
        save_settings()
    return {"ok": True}


def save_account_pnl(body):
    config = validate_pnl(body)
    with lock:
        account = find_account(body.get("id"))
        if account is None:
            raise ValueError("That account no longer exists. Refresh the page.")
        account.update(config)
        save_settings()
    return {"ok": True}


def add_deployment(body):
    """Deploy a strategy to an account. It starts with alerts and live trading off."""
    strategy = body.get("strategy")
    if strategy not in STRATEGIES:
        raise ValueError("Unknown strategy.")
    with lock:
        account = find_account(body.get("account_id"))
        if account is None:
            raise ValueError("That account no longer exists. Refresh the page.")
        if len(settings["deployments"]) >= MAX_DEPLOYMENTS:
            raise ValueError(f"You can have up to {MAX_DEPLOYMENTS} deployments.")
        deployment = new_deployment(account["id"], strategy)
        settings["deployments"].append(deployment)
        save_settings()
        return _copy(deployment)


def remove_deployment(body):
    """Stop running a strategy on an account. Its positions and stoplosses on Delta are left as they are."""
    with lock:
        deployment = find_deployment(body.get("id"))
        if deployment is None:
            raise ValueError("That deployment no longer exists. Refresh the page.")
        if deployment["pending_stops"]:
            raise ValueError(
                "This deployment has orders waiting for their stoplosses. Remove it once they fill or are cancelled."
            )
        if deployment["open_trades"]:
            raise ValueError("This deployment has trades being watched for their target. Remove it once they finish.")
        settings["deployments"].remove(deployment)
        save_settings()
    return {"ok": True}


AUTO_EXPIRY = re.compile(r"auto:([0-3])")
AUTO_NAMES = {"0": "same day", "1": "next day", "2": "2 days ahead", "3": "3 days ahead"}


def resolve_expiry(config, now):
    """The expiry to trade now. An auto choice, like auto:2, means the expiry dated 2 days after
    the entry date, so an entry on 12 Nov trades 14 Nov. It is never swapped for another date."""
    auto = AUTO_EXPIRY.fullmatch(config["expiry"])
    if not auto:
        return config["expiry"]
    wanted = now.astimezone(IST) + timedelta(days=int(auto.group(1)))
    code = f"{wanted:%d%m%y}"
    if target.expires_at(code) <= now:
        raise SignalError(f"Today's {config['symbol']} expiry has already expired at 17:30 IST.")
    if code not in {e["value"] for e in delta_client.list_expiries(config["symbol"])}:
        raise SignalError(f"Delta lists no {config['symbol']} expiry for {wanted:%d %b %Y} "
                          f"({AUTO_NAMES[auto.group(1)]}), so nothing was traded.")
    return code


def evaluate(config, now):
    """Return (kind, message, result): a signal, or a warning explaining why there is no signal.
    The result carries the expiry actually used, which an auto choice works out here."""
    time_label = now.strftime("%d %b %Y %H:%M")
    try:
        config = {**config, "expiry": resolve_expiry(config, now)}
    except (SignalError, DeltaError) as error:
        auto = AUTO_EXPIRY.fullmatch(config["expiry"])
        return "warning", (
            f"⚠️ NO ALERT: {config['symbol']} | Expiry {AUTO_NAMES[auto.group(1)] if auto else config['expiry']} (auto)\n"
            f"Time: {time_label} IST\n\n{error}\n\nNo strikes were selected."
        ), None
    try:
        expiry = delta_client.expiry_label(config["expiry"])
    except ValueError:
        expiry = config["expiry"]
    try:
        tickers = [
            ticker
            for ticker in delta_client.get_option_tickers(config["symbol"])
            if ticker["symbol"].endswith("-" + config["expiry"])
        ]
        if not tickers:
            raise SignalError("This expiry is not listed. It may have expired, so select a new one.")
        reference = delta_client.get_reference_price(
            config["symbol"], config["atm_reference"], tickers
        )
        result = strikes.select_strikes(tickers, reference, config["multiplier"])
    except (SignalError, DeltaError) as error:
        return "warning", (
            f"⚠️ NO ALERT: {config['symbol']} | Expiry {expiry}\n"
            f"Time: {time_label} IST\n\n{error}\n\nNo strikes were selected."
        ), None
    result["expiry"] = config["expiry"]
    shown = {**config, "stoploss": uses_stoploss(config)}
    return "signal", strikes.format_signal(result, shown, expiry, time_label), result


def uses_stoploss(job):
    """Straddle + Stoploss, and settings from before strategies, place stoplosses."""
    return STRATEGIES.get(job.get("strategy"), {}).get("stoploss", True)


def build_alert(config, now):
    """The alert without any trading. It never places orders."""
    kind, message, _ = evaluate(config, now)
    return (kind, f"{message}\n\n{ALERT_ONLY}") if kind == "signal" else (kind, message)


def notify_telegram(kind, message, account=None):
    """Send to the channel, labelled with the account, and keep each deployment's latest entry pinned.
    account is an account, or a job, whose id is its deployment's. Returns the status to log."""
    if not telegram_client.is_enabled():
        return "off"
    text = f"👤 {account['name']}\n{message}" if account else message
    status, message_id = telegram_client.send(text, silent=kind == "pnl", all_bold=kind == "pnl")
    if kind != "signal" or message_id is None or account is None:
        return status
    pin_error = telegram_client.pin(message_id)
    if pin_error:
        return f"{status}, pin failed: {pin_error}"
    with lock:
        live = find_deployment(account["id"])
        previous = live["pinned_message_id"] if live else None
        if live:
            live["pinned_message_id"] = message_id
            save_settings()
    unpin_error = telegram_client.unpin(previous) if previous else None
    if unpin_error:
        return f"{status}, pinned, old pin not removed: {unpin_error}"
    return f"{status}, pinned"


def trade_section(account, now, result):
    """Only the scheduled entry calls this, so test buttons can never place an order.
    The order tag includes the deployment, so two deployments on one account never share an order id."""
    stoploss = uses_stoploss(account)
    tag = f"{now:%y%m%d%H%M}{account['id'][:4]}"
    try:
        text, watch = trading.place_trade(result, account["trade_size"], tag, stoploss)
        if watch:
            with lock:
                live = find_deployment(account["id"])
                if live is None:
                    return f"{text}\n\n⚠️ This deployment was removed, so the app isn't watching these orders. Check Delta now."
                live["pending_stops"] = [*live["pending_stops"], *watch]
                if not stoploss:
                    trade = target.new_trade(watch, tag, result["expiry"], account["target_percent"])
                    live["open_trades"] = [*live["open_trades"], trade]
                save_settings()
            if not stoploss:
                text += (f"\n\n🎯 Watching for the target: {target.fmt(account['target_percent'])}% of premium, "
                         "judged on best ask. Held to expiry if it isn't reached.")
        return text
    except Exception as error:  # The alert must still go out, as orders may already be live.
        return f"⚠️ TRADING ERROR: {error!r}\nOrders may have been placed, so check Delta now."


def send_alert(account, now, source, trade=False):
    with signing(account):
        if trade and account.get("trading_enabled"):
            kind, message, result = evaluate(account, now)
            if kind == "signal":
                message = f"{message}\n\n{trade_section(account, now, result)}"
        else:
            kind, message = build_alert(account, now)
    entry = {
        "time": now.strftime("%d %b %H:%M:%S"),
        "account": account["name"],
        "strategy": strategy_name(account),
        "source": source,
        "kind": kind,
        "message": message,
        # Run alert now is a test for the logs, so it stays out of the channel.
        "telegram": "not sent" if source == "manual" else notify_telegram(kind, message, account),
    }
    add_log(entry)
    return entry


def strategy_name(job):
    strategy = STRATEGIES.get(job.get("strategy"))
    return strategy["name"] if strategy else None


def fire_key(config, now):
    return f"{now:%Y-%m-%d} {config['entry_time']}"


def is_due(config, now):
    return (
        config["enabled"]
        and bool(config["expiry"])
        and now.strftime("%H:%M") == config["entry_time"]
        and config["last_fired"] != fire_key(config, now)
    )


def run_in_background(target, *args):
    threading.Thread(target=_guarded, args=(target, args), daemon=True).start()


def _guarded(target, args):
    try:
        target(*args)
    except Exception as error:  # Shown in the logs, as nothing else would see it.
        report_error("entry", error)


def scheduler_tick(now):
    """Each due deployment's entry runs on its own thread, so a slow one can't make another miss its minute."""
    due = []
    with lock:
        for deployment in settings["deployments"]:
            account = find_account(deployment["account_id"])
            if account is not None and is_due(deployment, now):
                deployment["last_fired"] = fire_key(deployment, now)
                due.append(make_job(deployment, account))
        if due:
            save_settings()
    for job in due:
        run_in_background(send_alert, job, now, "scheduled", True)


def position_extras(positions):
    """Live LTP and stoploss orders per position. A failed lookup shows as unknown, not as no stop."""
    ltps = {}
    stops = {}
    for position in positions:
        symbol = position["product_symbol"]
        try:
            ltps[symbol] = delta_client.get_ltp(symbol)
        except (DeltaError, AttributeError):
            ltps[symbol] = None
        try:
            stops[symbol] = [
                order for order in delta_client.get_pending_orders(position["product_id"])
                if order.get("stop_order_type") == "stop_loss_order"
            ]
        except (DeltaError, KeyError, TypeError, AttributeError):
            stops[symbol] = None
    return ltps, stops


def pnl_report(account, now):
    """Return (kind, message, summary), where kind is pnl, none or warning.
    The summary, for a pnl, is what goes into an update that combines accounts."""
    time_label = now.strftime("%d %b %Y %H:%M")
    try:
        with signing(account):
            positions = [p for p in delta_client.get_positions() if pnl.is_open(p)]
            if not positions:
                return "none", f"No open positions at {time_label} IST.", None
            rate = pnl.usd_inr_rate(delta_client.get_wallets())
            ltps, stops = position_extras(positions)
        summary = pnl.account_summary(positions, rate, ltps, stops)
        return "pnl", pnl.format_positions(positions, rate, ltps, stops), summary
    except DeltaError as error:
        reason = str(error)
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        reason = f"Delta returned unexpected position data: {error!r}"
    return "warning", f"⚠️ PNL CHECK FAILED: {time_label} IST\n\n{reason}", None


def check_pnl(account, now):
    """Return (kind, message), where kind is pnl, none or warning."""
    return pnl_report(account, now)[:2]


def account_section(fetch, summarize):
    """Return (value, error), so a failure in funds doesn't hide positions, or the reverse."""
    try:
        return summarize(fetch()), None
    except DeltaError as error:
        return None, str(error)
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        return None, f"Delta returned unexpected account data: {error!r}"


def account_snapshot(account, now):
    with signing(account):
        wallets, funds_error = account_section(delta_client.get_wallets, list)
        funds = None
        inr_rate = None
        if wallets is not None:
            funds, funds_error = account_section(lambda: wallets, pnl.summarize_funds)
            try:
                inr_rate = pnl.usd_inr_rate(wallets)
            except (DeltaError, ValueError, TypeError):
                pass  # PnL then shows in USD only.
        positions, positions_error = account_section(
            delta_client.get_positions, lambda found: pnl.summarize_positions(found, inr_rate)
        )
    return {
        "time": now.strftime("%d %b %H:%M:%S"),
        "funds": funds,
        "funds_error": funds_error,
        "positions": positions,
        "positions_error": positions_error,
    }


def pnl_due(account, state, now):
    """Updates run on the clock, every interval counted from midnight IST, so accounts with
    the same interval are checked in the same minute and share one Telegram message."""
    minute = now.hour * 60 + now.minute
    return minute % account["pnl_interval_minutes"] == 0 and state["last_check"] != f"{now:%Y-%m-%d %H:%M}"


def send_pnl_updates(reports):
    """One message for all accounts with open positions this minute. Returns the Telegram status."""
    messages = pnl.format_combined([(account["name"], *summary) for account, _, summary in reports])
    statuses = [notify_telegram("pnl", message) for message in messages]
    return statuses[0] if len(set(statuses)) == 1 else ", ".join(statuses)


def pnl_tick(now):
    due = []
    with lock:
        for stale in set(pnl_state) - {a["id"] for a in settings["accounts"]}:
            del pnl_state[stale]
        for account in settings["accounts"]:
            state = pnl_state.setdefault(account["id"], {"latest": None, "last_check": None, "failing": False})
            if not account["pnl_enabled"]:
                state["last_check"] = None
                state["failing"] = False
                continue
            if not pnl_due(account, state, now):
                continue
            state["last_check"] = f"{now:%Y-%m-%d %H:%M}"
            due.append((json.loads(json.dumps(account)), state["failing"]))

    results = [(account, already_failing, *pnl_report(account, now)) for account, already_failing in due]
    reports = [(account, message, summary) for account, _, kind, message, summary in results if kind == "pnl"]
    combined_status = send_pnl_updates(reports) if reports else "not sent"
    for account, already_failing, kind, message, _ in results:
        telegram = "not sent"
        if kind == "pnl":
            telegram = combined_status
        elif kind == "warning" and not already_failing:
            # Warn once per outage, not every interval.
            telegram = notify_telegram(kind, message, account)
            add_log({
                "time": now.strftime("%d %b %H:%M:%S"),
                "account": account["name"],
                "source": "pnl",
                "kind": kind,
                "message": message,
                "telegram": telegram,
            })
        with lock:
            state = pnl_state.get(account["id"])
            if state is not None:
                state["failing"] = kind == "warning"
                state["latest"] = {
                    "time": now.strftime("%d %b %H:%M:%S"),
                    "kind": kind,
                    "message": message,
                    "telegram": telegram,
                }


def stops_tick(now):
    """Give every fill of a watched order its stoploss. The lists are saved, so this survives a restart."""
    with lock:
        work = []
        watched = {}  # Per account, as deployments on one account share its positions on Delta.
        for deployment in settings["deployments"]:
            account = find_account(deployment["account_id"])
            if account is not None and deployment["pending_stops"]:
                work.append((make_job(deployment, account), list(deployment["pending_stops"])))
                watched.setdefault(account["id"], []).extend(deployment["pending_stops"])
    for account, items in work:
        finished = set()
        messages = []
        changed = False
        everything = watched[account["account_id"]]
        with signing(account):
            for item in items:
                before = json.dumps(item)
                others = [i["order_id"] for i in everything if i["symbol"] == item["symbol"] and i is not item]
                done, item_messages = trading.watch_order(item, now.timestamp(), others)
                messages += item_messages
                changed = changed or done or json.dumps(item) != before
                if done:
                    finished.add(item["order_id"])
        if changed:
            with lock:
                live = find_deployment(account["id"])
                if live is not None:
                    updated = {item["order_id"]: item for item in items}
                    live["pending_stops"] = [
                        updated.get(i["order_id"], i) for i in live["pending_stops"] if i["order_id"] not in finished
                    ]
                    save_settings()
        for message in messages:
            kind = "warning" if message.startswith("⚠️") else "stop"
            add_log({
                "time": now.strftime("%d %b %H:%M:%S"),
                "account": account["name"],
                "strategy": strategy_name(account),
                "source": "stoploss",
                "kind": kind,
                "message": message,
                "telegram": notify_telegram(kind, message, account),
            })


def _lasting(trade):
    """A trade without the values that change on every check, to tell when it must be saved."""
    return json.dumps({k: v for k, v in trade.items() if k not in ("profit", "next_try")}, sort_keys=True)


def inr_rate_lookup(account):
    def lookup():
        with signing(account):
            return pnl.usd_inr_rate(delta_client.get_wallets())
    return lookup


def target_tick(now):
    """Check each Straddle + Target % trade against its target. Progress is saved, so this survives a restart."""
    with lock:
        work = []
        for deployment in settings["deployments"]:
            account = find_account(deployment["account_id"])
            if account is not None and deployment["open_trades"]:
                work.append((make_job(deployment, account), _copy(deployment["open_trades"])))
    for job, trades in work:
        messages = []
        finished = set()
        changed = False
        with signing(job):
            for trade in trades:
                before = _lasting(trade)
                done, trade_messages = target.step(trade, now, inr_rate_lookup(job))
                messages += trade_messages
                changed = changed or done or _lasting(trade) != before
                if done:
                    finished.add(trade["id"])
        with lock:
            live = find_deployment(job["id"])
            if live is not None:
                updated = {trade["id"]: trade for trade in trades}
                live["open_trades"] = [updated.get(t["id"], t) for t in live["open_trades"] if t["id"] not in finished]
                if changed:
                    save_settings()  # Every order change is saved, so a restart never loses an exit order.
        for message in messages:
            kind = "warning" if message.startswith(("⚠️", "⌛")) else "target"
            add_log({
                "time": now.strftime("%d %b %H:%M:%S"),
                "account": job["name"],
                "strategy": strategy_name(job),
                "source": "target",
                "kind": kind,
                "message": message,
                "telegram": notify_telegram(kind, message, job),
            })


def report_error(name, error):
    print(f"{name} error: {error!r}")
    add_log({
        "time": datetime.now(IST).strftime("%d %b %H:%M:%S"),
        "source": name,
        "kind": "warning",
        "message": f"⚠️ {name} error: {error!r}",
        "telegram": "off",
    })


def run_every_second(tick, name):
    while True:
        try:
            tick(datetime.now(IST))
        except Exception as error:  # A bad tick must not stop later runs.
            report_error(name, error)
        time.sleep(1)


def dashboard_key():
    return os.environ.get("DASHBOARD_KEY", "")


def key_matches(value):
    key = dashboard_key()
    return bool(key) and hmac.compare_digest(value.encode(), key.encode())


def is_public_host(host):
    public_host = os.environ.get("PUBLIC_HOST", "")
    return bool(public_host and dashboard_key()) and host == f"{public_host}:{PORT}"


def is_allowed(host, client_ip, cookie_header):
    """The local address needs a local client, which stops other websites and spoofed Host headers.
    The public address needs the secret key, saved in a cookie by the secret link."""
    if host in (f"{HOST}:{PORT}", f"localhost:{PORT}"):
        return client_ip == HOST
    if not is_public_host(host):
        return False
    try:
        morsel = SimpleCookie(cookie_header or "").get(KEY_COOKIE)
    except CookieError:
        return False
    return morsel is not None and key_matches(morsel.value)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        return is_allowed(self.headers.get("Host"), self.client_address[0], self.headers.get("Cookie"))

    def open_secret_link(self):
        """Save the key from the link in a cookie, then drop it from the address bar."""
        self.send_response(302)
        self.send_header("Set-Cookie", f"{KEY_COOKIE}={dashboard_key()}; {KEY_COOKIE_RULES}")
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        link_key = query.get("key", [""])[0]
        if url.path == "/" and is_public_host(self.headers.get("Host")) and key_matches(link_key):
            return self.open_secret_link()
        if not self.trusted():
            return self.respond(404, {"error": "Not found"})
        if url.path == "/":
            page = (ROOT / "static" / "index.html").read_bytes()
            return self.respond(200, page, "text/html; charset=utf-8")
        if url.path == "/api/state":
            with lock:
                state = {
                    "strategies": STRATEGIES,
                    "accounts": [public_account(a) for a in settings["accounts"]],
                    "deployments": _copy(settings["deployments"]),
                    "logs": list(logs),
                    "pnl": {account_id: s["latest"] for account_id, s in pnl_state.items()},
                    "telegram_enabled": telegram_client.is_enabled(),
                    "telegram_configured": telegram_client.is_configured(),
                    "now": datetime.now(IST).strftime("%d %b %Y %H:%M:%S"),
                }
            return self.respond(200, state)
        if url.path in ("/api/account", "/api/pnl"):
            try:
                account = account_copy(query.get("id", [""])[0])
            except ValueError as error:
                return self.respond(404, {"error": str(error)})
            now = datetime.now(IST)
            if url.path == "/api/account":
                return self.respond(200, account_snapshot(account, now))
            kind, message = check_pnl(account, now)
            return self.respond(200, {
                "time": now.strftime("%d %b %H:%M:%S"),
                "kind": kind,
                "message": message,
                "telegram": "not sent",
            })
        if url.path == "/api/expiries":
            symbol = query.get("symbol", [""])[0]
            if symbol not in SYMBOLS:
                return self.respond(400, {"error": "Symbol must be BTC or ETH."})
            try:
                return self.respond(200, {"expiries": delta_client.list_expiries(symbol)})
            except DeltaError as error:
                return self.respond(502, {"error": str(error)})
        return self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.trusted():
            return self.respond(404, {"error": "Not found"})
        if self.headers.get("Content-Type") != "application/json":
            return self.respond(403, {"error": "Forbidden"})
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(length, 10_000)))
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object.")
            if path == "/api/accounts/add":
                return self.respond(200, add_account(body))
            if path == "/api/accounts/remove":
                return self.respond(200, remove_account(body))
            if path == "/api/accounts/pnl":
                return self.respond(200, save_account_pnl(body))
            if path == "/api/deployments/add":
                return self.respond(200, add_deployment(body))
            if path == "/api/deployments/remove":
                return self.respond(200, remove_deployment(body))
            if path not in ("/api/settings", "/api/send-now"):
                return self.respond(404, {"error": "Not found"})
            job = job_copy(body.get("id"))
            config = validate(body)
        except ValueError as error:
            return self.respond(400, {"error": str(error)})

        if path == "/api/settings":
            with lock:
                live = find_deployment(job["id"])
                if live is None:
                    return self.respond(400, {"error": "That deployment no longer exists. Refresh the page."})
                live.update(config)
                save_settings()
            return self.respond(200, {"ok": True})
        return self.respond(200, send_alert({**job, **config}, datetime.now(IST), "manual"))


def main():
    load_env()
    load_settings()
    load_logs()
    # Separate threads, so a slow PnL request can't make the entry alert miss its minute.
    threading.Thread(target=run_every_second, args=(scheduler_tick, "scheduler"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(pnl_tick, "pnl"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(logs_tick, "logs"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(stops_tick, "stoploss"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(target_tick, "target"), daemon=True).start()
    print(f"Open http://{HOST}:{PORT} (keep this window open for alerts to fire)")
    # Only listen beyond this machine once a secret key protects the public address.
    public = bool(os.environ.get("PUBLIC_HOST") and dashboard_key())
    ThreadingHTTPServer(("0.0.0.0" if public else HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
