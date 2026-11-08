"""Runs the daily option-selling entry for each Delta account, with optional Telegram. Run: python app.py."""
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
IST = timezone(timedelta(hours=5, minutes=30))
SYMBOLS = ("BTC", "ETH")
# Every account has its own copy of these. A new account starts with alerts and trading off.
DEFAULTS = {
    "symbol": "BTC",
    "expiry": "",
    "multiplier": 1.8,
    "atm_reference": "spot",
    "entry_time": "09:30",
    "enabled": False,
    "last_fired": "",
    "pinned_message_id": None,
    "pnl_enabled": False,
    "pnl_interval_minutes": 5,
    "trading_enabled": False,
    "trade_size": 1,
    "pending_stops": [],
}
ALERT_ONLY = "Alert only. No order was placed."

lock = threading.Lock()
logs = deque(maxlen=MAX_LOGS)
settings = {"accounts": [], "logs_cleared_on": None}
# Per account id. PnL updates stay out of the logs so they can't push entry alerts out of the 100 kept.
pnl_state = {}


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def new_account(name, api_key, api_secret, **values):
    return {
        **json.loads(json.dumps(DEFAULTS)),
        **values,
        "id": secrets.token_hex(4),
        "name": name,
        "api_key": api_key,
        "api_secret": api_secret,
    }


def migrate(data):
    """Turn the old single-account settings into the first account, keys taken from .env.
    Returns (settings, changed)."""
    if "accounts" in data:
        return data, False
    key = os.environ.get("DELTA_API_KEY", "")
    secret = os.environ.get("DELTA_API_SECRET", "")
    accounts = []
    if key and secret:
        accounts.append(new_account("Main", key, secret, **{k: data[k] for k in DEFAULTS if k in data}))
    return {"accounts": accounts, "logs_cleared_on": data.get("logs_cleared_on")}, True


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
        for key, value in DEFAULTS.items():
            account.setdefault(key, json.loads(json.dumps(value)))
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
        return json.loads(json.dumps(account))


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


def validate(body, require_expiry):
    """Return clean settings from a frontend request, or raise ValueError with a readable reason."""
    symbol = body.get("symbol")
    if symbol not in SYMBOLS:
        raise ValueError("Symbol must be BTC or ETH.")
    expiry = body.get("expiry") or ""
    if not isinstance(expiry, str) or (expiry and not re.fullmatch(r"\d{6}", expiry)):
        raise ValueError("Expiry is not valid.")
    if require_expiry and not expiry:
        raise ValueError("Select an expiry first.")
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
    interval = body.get("pnl_interval_minutes", DEFAULTS["pnl_interval_minutes"])
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440:
        raise ValueError("PnL interval must be a whole number of minutes from 1 to 1440.")
    size = body.get("trade_size", DEFAULTS["trade_size"])
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= trading.MAX_SIZE:
        raise ValueError(f"Order size must be a whole number from 1 to {trading.MAX_SIZE}.")
    return {
        "symbol": symbol,
        "expiry": expiry,
        "multiplier": float(multiplier),
        "atm_reference": body["atm_reference"],
        "entry_time": entry_time,
        "enabled": body.get("enabled") is True,
        "pnl_enabled": body.get("pnl_enabled") is True,
        "pnl_interval_minutes": interval,
        "trading_enabled": body.get("trading_enabled") is True,
        "trade_size": size,
    }


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
    """Stop managing an account. Its positions and stoplosses on Delta are left as they are."""
    with lock:
        account = find_account(body.get("id"))
        if account is None:
            raise ValueError("That account no longer exists. Refresh the page.")
        if account["pending_stops"]:
            raise ValueError(
                "This account has orders waiting for their stoplosses. Remove it once they fill or are cancelled."
            )
        settings["accounts"].remove(account)
        pnl_state.pop(account["id"], None)
        save_settings()
    return {"ok": True}


def evaluate(config, now):
    """Return (kind, message, result): a signal, or a warning explaining why there is no signal."""
    try:
        expiry = delta_client.expiry_label(config["expiry"])
    except ValueError:
        expiry = config["expiry"]
    time_label = now.strftime("%d %b %Y %H:%M")
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
    return "signal", strikes.format_signal(result, config, expiry, time_label), result


def build_alert(config, now):
    """The alert without any trading. It never places orders."""
    kind, message, _ = evaluate(config, now)
    return (kind, f"{message}\n\n{ALERT_ONLY}") if kind == "signal" else (kind, message)


def notify_telegram(kind, message, account=None):
    """Send to the channel, labelled with the account, and keep each account's latest entry pinned.
    Returns the status to log."""
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
        live = find_account(account["id"])
        previous = live["pinned_message_id"] if live else None
        if live:
            live["pinned_message_id"] = message_id
            save_settings()
    unpin_error = telegram_client.unpin(previous) if previous else None
    if unpin_error:
        return f"{status}, pinned, old pin not removed: {unpin_error}"
    return f"{status}, pinned"


def trade_section(account, now, result):
    """Only the scheduled entry calls this, so test buttons can never place an order."""
    try:
        text, watch = trading.place_trade(result, account["trade_size"], f"{now:%y%m%d%H%M}")
        if watch:
            with lock:
                live = find_account(account["id"])
                if live is None:
                    return f"{text}\n\n⚠️ This account was removed, so the app can't add separate stoplosses. Check Delta now."
                live["pending_stops"] = [*live["pending_stops"], *watch]
                save_settings()
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
        "source": source,
        "kind": kind,
        "message": message,
        # Run alert now is a test for the logs, so it stays out of the channel.
        "telegram": "not sent" if source == "manual" else notify_telegram(kind, message, account),
    }
    add_log(entry)
    return entry


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
    """Each due account's entry runs on its own thread, so a slow one can't make another miss its minute."""
    due = []
    with lock:
        for account in settings["accounts"]:
            if is_due(account, now):
                account["last_fired"] = fire_key(account, now)
                due.append(json.loads(json.dumps(account)))
        if due:
            save_settings()
    for account in due:
        run_in_background(send_alert, account, now, "scheduled", True)


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


def check_pnl(account, now):
    """Return (kind, message), where kind is pnl, none or warning."""
    time_label = now.strftime("%d %b %Y %H:%M")
    try:
        with signing(account):
            positions = [p for p in delta_client.get_positions() if pnl.is_open(p)]
            if not positions:
                return "none", f"No open positions at {time_label} IST."
            rate = pnl.usd_inr_rate(delta_client.get_wallets())
            ltps, stops = position_extras(positions)
        return "pnl", pnl.format_positions(positions, rate, ltps, stops)
    except DeltaError as error:
        reason = str(error)
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        reason = f"Delta returned unexpected position data: {error!r}"
    return "warning", f"⚠️ PNL CHECK FAILED: {time_label} IST\n\n{reason}"


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
            if state["last_check"] and now - state["last_check"] < timedelta(minutes=account["pnl_interval_minutes"]):
                continue
            state["last_check"] = now
            due.append((json.loads(json.dumps(account)), state["failing"]))

    for account, already_failing in due:
        kind, message = check_pnl(account, now)
        telegram = "not sent"
        if kind == "pnl":
            telegram = notify_telegram(kind, message, account)
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
        work = [(json.loads(json.dumps(a)), list(a["pending_stops"])) for a in settings["accounts"] if a["pending_stops"]]
    for account, items in work:
        finished = set()
        messages = []
        changed = False
        with signing(account):
            for item in items:
                before = json.dumps(item)
                others = [i["order_id"] for i in items if i["symbol"] == item["symbol"] and i is not item]
                done, item_messages = trading.watch_order(item, now.timestamp(), others)
                messages += item_messages
                changed = changed or done or json.dumps(item) != before
                if done:
                    finished.add(item["order_id"])
        if changed:
            with lock:
                live = find_account(account["id"])
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
                "source": "stoploss",
                "kind": kind,
                "message": message,
                "telegram": notify_telegram(kind, message, account),
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
                    "accounts": [public_account(a) for a in settings["accounts"]],
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
            if path not in ("/api/settings", "/api/send-now"):
                return self.respond(404, {"error": "Not found"})
            account = account_copy(body.get("id"))
            config = validate(body, path != "/api/settings" or body.get("enabled") is True)
        except ValueError as error:
            return self.respond(400, {"error": str(error)})

        if path == "/api/settings":
            with lock:
                live = find_account(account["id"])
                if live is None:
                    return self.respond(400, {"error": "That account no longer exists. Refresh the page."})
                live.update(config)
                save_settings()
            return self.respond(200, {"ok": True})
        return self.respond(200, send_alert({**account, **config}, datetime.now(IST), "manual"))


def main():
    load_env()
    load_settings()
    load_logs()
    # Separate threads, so a slow PnL request can't make the entry alert miss its minute.
    threading.Thread(target=run_every_second, args=(scheduler_tick, "scheduler"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(pnl_tick, "pnl"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(logs_tick, "logs"), daemon=True).start()
    threading.Thread(target=run_every_second, args=(stops_tick, "stoploss"), daemon=True).start()
    print(f"Open http://{HOST}:{PORT} (keep this window open for alerts to fire)")
    # Only listen beyond this machine once a secret key protects the public address.
    public = bool(os.environ.get("PUBLIC_HOST") and dashboard_key())
    ThreadingHTTPServer(("0.0.0.0" if public else HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
