"""Logs a daily option-selling strike alert, with optional Telegram. Run: python app.py, open the URL."""
import json
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
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
ROOT = Path(__file__).parent
SETTINGS_FILE = ROOT / "settings.json"
LOG_FILE = ROOT / "logs.jsonl"
MAX_LOGS = 100
IST = timezone(timedelta(hours=5, minutes=30))
SYMBOLS = ("BTC", "ETH")
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
    "logs_cleared_on": None,
    "pending_stops": [],
}
ALERT_ONLY = "Alert only. No order was placed."

lock = threading.Lock()
logs = deque(maxlen=MAX_LOGS)
settings = dict(DEFAULTS)
# PnL updates stay out of the logs so they can't push entry alerts out of the 100 kept.
pnl_state = {"latest": None, "last_check": None, "failing": False}


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except ValueError as error:
            print(f"Ignoring unreadable {SETTINGS_FILE.name}: {error}")


def save_settings():
    temp = SETTINGS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temp, SETTINGS_FILE)


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


def notify_telegram(kind, message):
    """Send the alert and keep only the latest entry signal pinned. Returns the status to log."""
    if not telegram_client.is_enabled():
        return "off"
    status, message_id = telegram_client.send(message, silent=kind == "pnl", all_bold=kind == "pnl")
    if kind != "signal" or message_id is None:
        return status
    pin_error = telegram_client.pin(message_id)
    if pin_error:
        return f"{status}, pin failed: {pin_error}"
    with lock:
        previous = settings["pinned_message_id"]
        settings["pinned_message_id"] = message_id
        save_settings()
    unpin_error = telegram_client.unpin(previous) if previous else None
    if unpin_error:
        return f"{status}, pinned, old pin not removed: {unpin_error}"
    return f"{status}, pinned"


def trade_section(config, now, result):
    """Only the scheduled entry calls this, so test buttons can never place an order."""
    try:
        text, watch = trading.place_trade(result, config["trade_size"], f"{now:%y%m%d%H%M}")
        if watch:
            with lock:
                settings["pending_stops"] = [*settings["pending_stops"], *watch]
                save_settings()
        return text
    except Exception as error:  # The alert must still go out, as orders may already be live.
        return f"⚠️ TRADING ERROR: {error!r}\nOrders may have been placed, so check Delta now."


def send_alert(config, now, source, trade=False):
    if trade and config.get("trading_enabled"):
        kind, message, result = evaluate(config, now)
        if kind == "signal":
            message = f"{message}\n\n{trade_section(config, now, result)}"
    else:
        kind, message = build_alert(config, now)
    entry = {
        "time": now.strftime("%d %b %H:%M:%S"),
        "source": source,
        "kind": kind,
        "message": message,
        # Run alert now is a test for the logs, so it stays out of the channel.
        "telegram": "not sent" if source == "manual" else notify_telegram(kind, message),
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


def scheduler_tick(now):
    with lock:
        config = dict(settings)
        due = is_due(config, now)
        if due:
            settings["last_fired"] = fire_key(config, now)
            save_settings()
    if due:
        send_alert(config, now, "scheduled", trade=True)


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


def check_pnl(now):
    """Return (kind, message), where kind is pnl, none or warning."""
    time_label = now.strftime("%d %b %Y %H:%M")
    try:
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


def account_snapshot(now):
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
    with lock:
        enabled = settings["pnl_enabled"]
        interval = settings["pnl_interval_minutes"]
        last_check = pnl_state["last_check"]
        if not enabled:
            pnl_state["last_check"] = None
            pnl_state["failing"] = False
            return
        if last_check and now - last_check < timedelta(minutes=interval):
            return
        pnl_state["last_check"] = now
        already_failing = pnl_state["failing"]

    kind, message = check_pnl(now)
    telegram = "not sent"
    if kind == "pnl":
        telegram = notify_telegram(kind, message)
    elif kind == "warning" and not already_failing:
        # Warn once per outage, not every interval.
        telegram = notify_telegram(kind, message)
        add_log({
            "time": now.strftime("%d %b %H:%M:%S"),
            "source": "pnl",
            "kind": kind,
            "message": message,
            "telegram": telegram,
        })
    with lock:
        pnl_state["failing"] = kind == "warning"
        pnl_state["latest"] = {
            "time": now.strftime("%d %b %H:%M:%S"),
            "kind": kind,
            "message": message,
            "telegram": telegram,
        }


def stops_tick(now):
    """Give every fill of a watched order its stoploss. The list is saved, so this survives a restart."""
    with lock:
        items = list(settings["pending_stops"])
    finished = set()
    messages = []
    changed = False
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
            settings["pending_stops"] = [i for i in settings["pending_stops"] if i["order_id"] not in finished]
            save_settings()
    for message in messages:
        kind = "warning" if message.startswith("⚠️") else "stop"
        add_log({
            "time": now.strftime("%d %b %H:%M:%S"),
            "source": "stoploss",
            "kind": kind,
            "message": message,
            "telegram": notify_telegram(kind, message),
        })


def run_every_second(tick, name):
    while True:
        try:
            tick(datetime.now(IST))
        except Exception as error:  # A bad tick must not stop later runs.
            print(f"{name} error: {error!r}")
            add_log({
                "time": datetime.now(IST).strftime("%d %b %H:%M:%S"),
                "source": name,
                "kind": "warning",
                "message": f"⚠️ {name} error: {error!r}",
                "telegram": "off",
            })
        time.sleep(1)


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
        # Stops other websites in the browser from reaching this local app.
        return self.headers.get("Host") in (f"{HOST}:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.trusted():
            return self.respond(403, {"error": "Forbidden"})
        url = urlparse(self.path)
        if url.path == "/":
            page = (ROOT / "static" / "index.html").read_bytes()
            return self.respond(200, page, "text/html; charset=utf-8")
        if url.path == "/api/state":
            with lock:
                state = {
                    "settings": dict(settings),
                    "logs": list(logs),
                    "pnl": pnl_state["latest"],
                    "telegram_enabled": telegram_client.is_enabled(),
                    "telegram_configured": telegram_client.is_configured(),
                    "now": datetime.now(IST).strftime("%d %b %Y %H:%M:%S"),
                }
            return self.respond(200, state)
        if url.path == "/api/account":
            return self.respond(200, account_snapshot(datetime.now(IST)))
        if url.path == "/api/pnl":
            now = datetime.now(IST)
            kind, message = check_pnl(now)
            return self.respond(200, {
                "time": now.strftime("%d %b %H:%M:%S"),
                "kind": kind,
                "message": message,
                "telegram": "not sent",
            })
        if url.path == "/api/expiries":
            symbol = parse_qs(url.query).get("symbol", [""])[0]
            if symbol not in SYMBOLS:
                return self.respond(400, {"error": "Symbol must be BTC or ETH."})
            try:
                return self.respond(200, {"expiries": delta_client.list_expiries(symbol)})
            except DeltaError as error:
                return self.respond(502, {"error": str(error)})
        return self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.trusted() or self.headers.get("Content-Type") != "application/json":
            return self.respond(403, {"error": "Forbidden"})
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(length, 10_000)))
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object.")
            config = validate(body, path != "/api/settings" or body.get("enabled") is True)
        except ValueError as error:
            return self.respond(400, {"error": str(error)})

        now = datetime.now(IST)
        if path == "/api/settings":
            with lock:
                settings.update(config)
                save_settings()
            return self.respond(200, {"ok": True})
        if path == "/api/send-now":
            return self.respond(200, send_alert(config, now, "manual"))
        return self.respond(404, {"error": "Not found"})


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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
