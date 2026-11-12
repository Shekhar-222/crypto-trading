import hashlib
import hmac
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import app
import pnl
import trading
from delta_client import DeltaError, DeltaRejected
from strikes import SignalError, select_strikes

ORIGINAL_PLACE_ORDER = app.delta_client.place_order


def option(kind, strike, ltp, expiry="021126"):
    prefix = "C" if kind == "call_options" else "P"
    return {
        "contract_type": kind,
        "strike_price": str(strike),
        "close": ltp,
        "symbol": f"{prefix}-BTC-{strike}-{expiry}",
        "spot_price": "17351",
    }


def chain(strike_list, atm=17350, atm_call=90.0, atm_put=90.95):
    tickers = []
    for strike in strike_list:
        tickers.append(option("call_options", strike, atm_call if strike == atm else 5))
        tickers.append(option("put_options", strike, atm_put if strike == atm else 5))
    return tickers


STRIKES = [17200, 17250, 17300, 17350, 17400, 17450, 17500]
CONFIG = {
    **app.DEFAULTS, "id": "acc1", "name": "Main", "api_key": "main-key-1234", "api_secret": "main-secret",
    "symbol": "BTC", "expiry": "021126", "multiplier": 0.5, "atm_reference": "spot",
    "entry_time": "09:30", "enabled": True, "last_fired": "", "pending_stops": [],
}
NOW = datetime(2026, 11, 1, 9, 30, 5, tzinfo=app.IST)


def accounts_in_settings(test, *jobs):
    """Put each job (an account with its strategy settings, like CONFIG) in the app's settings
    for one test, as an account plus a Straddle + Stoploss deployment sharing its id.
    Returns the live deployments; the live accounts are app.settings["accounts"]."""
    accounts, deployments = [], []
    for job in jobs:
        account, deployment = app.split_account(json.loads(json.dumps(job)))
        deployment["id"] = job["id"]
        deployment["strategy"] = job.get("strategy", "straddle_stoploss")
        deployment["expiry"] = job["expiry"]
        accounts.append(account)
        deployments.append(deployment)
    patcher = mock.patch.dict(app.settings, {"accounts": accounts, "deployments": deployments})
    patcher.start()
    test.addCleanup(patcher.stop)
    return deployments


def run_inline(target, *args):
    target(*args)


class SelectStrikes(unittest.TestCase):
    def test_worked_example_from_formula(self):
        result = select_strikes(chain(STRIKES), 17351, 0.5)
        self.assertEqual(result["atm_strike"], 17350)
        self.assertAlmostEqual(result["straddle"], 180.95)
        self.assertAlmostEqual(result["call"]["calculated"], 17440.475)
        self.assertAlmostEqual(result["put"]["calculated"], 17259.525)
        self.assertEqual(result["call"]["strike"], 17450)
        self.assertEqual(result["put"]["strike"], 17250)
        self.assertFalse(result["call"]["adjusted"] or result["put"]["adjusted"])

    def test_missing_or_zero_atm_ltp_gives_no_strikes(self):
        for bad in (None, 0, "", "0"):
            with self.subTest(bad=bad), self.assertRaisesRegex(SignalError, "C-BTC-17350"):
                select_strikes(chain(STRIKES, atm_call=bad), 17351, 0.5)

    def test_out_of_range_uses_last_strike_and_flags_adjusted(self):
        result = select_strikes(chain(STRIKES), 17351, 5)
        self.assertEqual(result["call"]["strike"], 17500)
        self.assertEqual(result["put"]["strike"], 17200)
        self.assertTrue(result["call"]["adjusted"] and result["put"]["adjusted"])

    def test_exact_tie_goes_further_out_of_the_money(self):
        result = select_strikes(chain(STRIKES, atm_call=50, atm_put=50), 17350, 0.75)
        self.assertEqual(result["call"]["strike"], 17450)
        self.assertEqual(result["put"]["strike"], 17250)

    def test_atm_needs_both_call_and_put(self):
        tickers = chain(STRIKES) + [option("call_options", 17360, 5)]
        self.assertEqual(select_strikes(tickers, 17361, 0.5)["atm_strike"], 17350)


class Alerts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(app, "LOG_FILE", Path(folder.name) / "logs.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(app.logs.clear)

    def build(self, tickers):
        with mock.patch.object(app.delta_client, "get_option_tickers", return_value=tickers):
            return app.build_alert(CONFIG, NOW)

    def test_signal_gives_both_sell_strikes(self):
        kind, message = self.build(chain(STRIKES))
        self.assertEqual(kind, "signal")
        self.assertIn("SELL CALL 17,450", message)
        self.assertIn("SELL PUT 17,250", message)
        self.assertNotIn("Adjusted", message)
        self.assertNotIn("→", message)

    def test_stoploss_is_twice_ltp_below_each_sell_line(self):
        _, message = self.build(chain(STRIKES))
        lines = message.split("\n")
        for side in ("CALL", "PUT"):
            index = next(i for i, line in enumerate(lines) if line.startswith(f"SELL {side}"))
            self.assertEqual(lines[index + 1], "Stoploss: 10")

    def test_adjusted_strike_has_warning_and_no_stoploss(self):
        with mock.patch.object(app.delta_client, "get_option_tickers", return_value=chain(STRIKES)):
            _, message = app.build_alert({**CONFIG, "multiplier": 5}, NOW)
        self.assertEqual(message.count("⚠️ Adjusted"), 2)
        self.assertNotIn("Stoploss", message)

    def test_only_the_adjusted_side_loses_its_stoploss(self):
        tickers = [t for t in chain(STRIKES)
                   if not (t["contract_type"] == "call_options" and float(t["strike_price"]) > 17400)]
        _, message = self.build(tickers)
        self.assertIn("calculated call strike", message)
        self.assertEqual(message.count("Stoploss"), 1)
        self.assertIn("SELL PUT 17,250 (P-BTC-17250-021126), LTP 5\nStoploss: 10", message)

    def test_selected_strike_without_ltp_shows_no_stoploss_value(self):
        tickers = chain(STRIKES)
        for ticker in tickers:
            if ticker["symbol"] == "C-BTC-17450-021126":
                ticker["close"] = None
        _, message = self.build(tickers)
        self.assertIn("LTP n/a\nStoploss: n/a (no LTP)", message)

    def test_missing_ltp_warning_is_sent_to_telegram(self):
        tickers = chain(STRIKES, atm_put=None)
        with mock.patch.object(app.delta_client, "get_option_tickers", return_value=tickers), \
                mock.patch.object(app.telegram_client, "is_enabled", return_value=True), \
                mock.patch.object(app.telegram_client, "send", return_value=("sent", 7)) as send, \
                mock.patch.object(app.telegram_client, "pin") as pin:
            entry = app.send_alert(CONFIG, NOW, "test")
        pin.assert_not_called()
        self.assertEqual(entry["telegram"], "sent")
        self.assertEqual(entry["kind"], "warning")
        self.assertIn("No LTP", send.call_args.args[0])
        self.assertNotIn("SELL", send.call_args.args[0])

    def test_telegram_off_by_default_and_alert_still_logged(self):
        with mock.patch.dict(app.os.environ), \
                mock.patch.object(app.delta_client, "get_option_tickers", return_value=chain(STRIKES)), \
                mock.patch.object(app.telegram_client, "send") as send:
            app.os.environ.pop("TELEGRAM_ENABLED", None)
            entry = app.send_alert(CONFIG, NOW, "test")
        send.assert_not_called()
        self.assertEqual(entry["telegram"], "off")
        self.assertIs(app.logs[0], entry)

    def test_run_alert_now_only_logs_and_skips_telegram(self):
        with mock.patch.object(app.delta_client, "get_option_tickers", return_value=chain(STRIKES)), \
                mock.patch.object(app.telegram_client, "is_enabled", return_value=True), \
                mock.patch.object(app.telegram_client, "send") as send, \
                mock.patch.object(app.telegram_client, "pin") as pin:
            entry = app.send_alert(CONFIG, NOW, "manual")
        send.assert_not_called()
        pin.assert_not_called()
        self.assertEqual(entry["telegram"], "not sent")
        self.assertIs(app.logs[0], entry)

    def test_logs_survive_a_restart_newest_first(self):
        app.add_log({"message": "first"})
        app.add_log({"message": "second"})
        app.logs.clear()
        app.load_logs()
        self.assertEqual([entry["message"] for entry in app.logs], ["second", "first"])

    def test_other_expiries_are_ignored(self):
        kind, message = self.build([option("call_options", 17350, 5, expiry="031126")])
        self.assertEqual(kind, "warning")
        self.assertIn("not listed", message)


class TelegramFormat(unittest.TestCase):
    def test_bolds_the_stoploss(self):
        result = app.telegram_client.to_html("SELL PUT 75,200 (x), LTP 104\nStoploss: 1,208.5\nStoploss: n/a (no LTP)")
        self.assertIn("\n<b>Stoploss: 1,208.5</b>\n", result)
        self.assertIn("<b>Stoploss: n/a</b> (no LTP)", result)

    def test_bolds_only_the_sell_strikes(self):
        text = "Header\n\nSELL CALL 80,000 (C-BTC-80000-031126), LTP 134\nCall: calculated 80,710.8\n\nSELL PUT 75,200.5 (x), LTP 1"
        result = app.telegram_client.to_html(text)
        self.assertIn("<b>SELL CALL 80,000</b> (C-BTC-80000-031126), LTP 134", result)
        self.assertIn("<b>SELL PUT 75,200.5</b> (x), LTP 1", result)
        self.assertEqual(result.count("<b>"), 2)

    def test_all_bold_wraps_the_whole_message_once(self):
        self.assertEqual(app.telegram_client.to_html("📊 a < b\nPnL -₹0.51", all_bold=True),
                         "<b>📊 a &lt; b\nPnL -₹0.51</b>")

    def test_escapes_other_markup(self):
        self.assertEqual(app.telegram_client.to_html("a <b> & c"), "a &lt;b&gt; &amp; c")

    def test_send_uses_html_mode(self):
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with mock.patch.dict(app.os.environ, env), \
                mock.patch.object(app.telegram_client, "_call") as call:
            call.return_value = {"message_id": 7}
            self.assertEqual(app.telegram_client.send("SELL PUT 1"), ("sent", 7))
        params = call.call_args.args[1]
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertEqual(params["text"], "<b>SELL PUT 1</b>")


class Pinning(unittest.TestCase):
    def run_notify(self, send=("sent", 8), pin=None, unpin=None, previous=5, kind="signal"):
        other = {**CONFIG, "id": "acc2", "name": "Second", "pinned_message_id": 99}
        live, live_other = accounts_in_settings(self, {**CONFIG, "pinned_message_id": previous}, other)
        with mock.patch.object(app, "save_settings") as save, \
                mock.patch.object(app.telegram_client, "is_enabled", return_value=True), \
                mock.patch.object(app.telegram_client, "send", return_value=send), \
                mock.patch.object(app.telegram_client, "pin", return_value=pin) as pin_call, \
                mock.patch.object(app.telegram_client, "unpin", return_value=unpin) as unpin_call:
            status = app.notify_telegram(kind, "message", CONFIG)
        self.assertEqual(live_other["pinned_message_id"], 99)  # Each account keeps its own pin.
        return status, pin_call, unpin_call, live["pinned_message_id"], save

    def test_message_is_labelled_with_the_account(self):
        accounts_in_settings(self, CONFIG)
        with mock.patch.object(app.telegram_client, "is_enabled", return_value=True), \
                mock.patch.object(app.telegram_client, "send", return_value=("sent", 8)) as send:
            app.notify_telegram("pnl", "update", CONFIG)
        self.assertEqual(send.call_args.args[0], "👤 Main\nupdate")

    def test_pins_new_signal_then_unpins_previous(self):
        status, pin, unpin, stored, save = self.run_notify()
        self.assertEqual(status, "sent, pinned")
        pin.assert_called_once_with(8)
        unpin.assert_called_once_with(5)
        self.assertEqual(stored, 8)
        save.assert_called_once()

    def test_first_pin_has_nothing_to_unpin(self):
        status, _, unpin, stored, _ = self.run_notify(previous=None)
        self.assertEqual(status, "sent, pinned")
        unpin.assert_not_called()
        self.assertEqual(stored, 8)

    def test_warning_is_not_pinned(self):
        status, pin, unpin, stored, _ = self.run_notify(kind="warning")
        self.assertEqual(status, "sent")
        pin.assert_not_called()
        unpin.assert_not_called()
        self.assertEqual(stored, 5)

    def test_failed_send_changes_nothing(self):
        status, pin, _, stored, _ = self.run_notify(send=("failed: Telegram returned HTTP 400", None))
        self.assertEqual(status, "failed: Telegram returned HTTP 400")
        pin.assert_not_called()
        self.assertEqual(stored, 5)

    def test_failed_pin_keeps_the_old_pin(self):
        status, _, unpin, stored, _ = self.run_notify(pin="HTTP 400")
        self.assertEqual(status, "sent, pin failed: HTTP 400")
        unpin.assert_not_called()
        self.assertEqual(stored, 5)

    def test_failed_unpin_is_reported_and_new_pin_is_kept(self):
        status, _, _, stored, _ = self.run_notify(unpin="HTTP 400")
        self.assertEqual(status, "sent, pinned, old pin not removed: HTTP 400")
        self.assertEqual(stored, 8)


POSITION = {
    "product_symbol": "P-BTC-75200-031126", "size": -100, "entry_price": "98",
    "mark_price": "90.5", "unrealized_pnl": "999", "realized_cashflow": "9.8", "unrealized_cashflow": "-9.05",
    "product_id": 5,
}
CLOSED = {**POSITION, "product_symbol": "C-BTC-80000-031126", "size": 0}
LOSING = {**POSITION, "product_symbol": "C-BTC-86000-131126", "mark_price": "120.5", "unrealized_cashflow": "-12.05"}
# Real data from the first live trade: Delta's unrealized_pnl of 0.143 is not the profit.
FIRST_TRADE = {
    "product_symbol": "P-BTC-75600-031126", "size": -1, "entry_price": "135",
    "mark_price": "140.93539815", "unrealized_pnl": "0.14300000",
    "realized_cashflow": "0.135", "unrealized_cashflow": "-0.143",
}
# Real data from the Delta screen: cashflows of ₹1,192.97 at the wallet's rate of ₹85 per USD.
ADDED_TRADE = {
    "product_symbol": "P-BTC-75600-031126", "size": -101, "entry_price": "138.96", "mark_price": "153.7",
    "unrealized_pnl": "15.756", "realized_cashflow": "14.035", "unrealized_cashflow": "-15.756",
}


USD_WALLET = [
    {"asset_symbol": "BTC", "balance": "0", "balance_inr": "0"},
    {"asset_symbol": "USD", "balance": "100", "balance_inr": "8500"},
]


class PnlMessages(unittest.TestCase):
    def setUp(self):
        for name, value in (("get_ltp", {"return_value": 91.5}),
                            ("get_pending_orders", {"return_value": [{
                                "stop_order_type": "stop_loss_order", "stop_price": "196", "size": 100,
                            }]})):
            patcher = mock.patch.object(app.delta_client, name, **value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def check(self, positions, wallets=USD_WALLET):
        with mock.patch.object(app.delta_client, "get_positions", return_value=positions), \
                mock.patch.object(app.delta_client, "get_wallets", return_value=wallets):
            return app.check_pnl(CONFIG, NOW)

    def test_shows_average_entry_ltp_and_stoploss_without_mark(self):
        _, message = self.check([POSITION])
        self.assertIn("Size -100 | Avg Entry 98 | LTP 91.5 | SL 196 | PnL ₹63.75", message)
        self.assertNotIn("Mark", message)

    def test_two_stoplosses_show_with_their_sizes_lowest_first(self):
        self.get_pending_orders.return_value = [
            {"stop_order_type": "stop_loss_order", "stop_price": "278", "size": 100},
            {"stop_order_type": "stop_loss_order", "stop_price": "270", "size": 1},
        ]
        _, message = self.check([POSITION])
        self.assertIn("SL 270 x1, 278 x100", message)

    def test_no_stoploss_is_called_out(self):
        self.get_pending_orders.return_value = []
        _, message = self.check([POSITION])
        self.assertIn("SL ⚠️ NONE", message)

    def test_take_profit_is_not_shown_as_a_stoploss(self):
        self.get_pending_orders.return_value = [{"stop_order_type": "take_profit_order", "stop_price": "50", "size": 1}]
        _, message = self.check([POSITION])
        self.assertIn("SL ⚠️ NONE", message)

    def test_failed_stop_lookup_says_unknown_not_none(self):
        self.get_pending_orders.side_effect = DeltaError("down")
        kind, message = self.check([POSITION])
        self.assertEqual(kind, "pnl")
        self.assertIn("SL unknown", message)
        self.assertNotIn("NONE", message)

    def test_missing_ltp_still_sends_the_update(self):
        for failure in ({"return_value": None}, {"side_effect": DeltaError("down")}):
            with self.subTest(failure=failure), mock.patch.object(app.delta_client, "get_ltp", **failure):
                kind, message = self.check([POSITION])
                self.assertEqual(kind, "pnl")
                self.assertIn("LTP n/a", message)

    def test_totals_open_positions_in_rupees_and_ignores_closed(self):
        kind, message = self.check([POSITION, LOSING, CLOSED])
        self.assertEqual(kind, "pnl")
        self.assertTrue(message.startswith(
            pnl.TITLE_PADDING + "📊 PNL UPDATE📊\n\n❤️ Total PnL: -₹127.50 (-$1.50) ❤️\n\n🟢 P-BTC-75200-031126\n"
        ))
        self.assertIn("PnL ₹63.75", message)
        self.assertIn("PnL -₹191.25", message)
        self.assertNotIn("C-BTC-80000", message)
        self.assertEqual(message.count("$"), 1)

    def test_hearts_show_on_the_total_line_only(self):
        _, message = self.check([POSITION])
        self.assertIn("💚 Total PnL: ₹63.75 ($0.75) 💚\n", message)
        self.assertEqual(message.count("💚"), 2)
        _, message = self.check([LOSING])
        self.assertIn("❤️ Total PnL: -₹191.25 (-$2.25) ❤️\n", message)
        self.assertEqual(message.count("❤️"), 2)

    def test_no_hearts_or_dots_when_pnl_is_zero(self):
        flat = {**POSITION, "realized_cashflow": "9", "unrealized_cashflow": "-9"}
        _, message = self.check([flat])
        self.assertIn("\nTotal PnL: ₹0.00 ($0.00)\n", message)
        for mark in ("💚", "❤️", "🟢", "🔴"):
            self.assertNotIn(mark, message)

    def test_each_position_gets_one_dot_in_front(self):
        _, message = self.check([POSITION, LOSING])
        self.assertEqual(message.count("🟢"), 1)
        self.assertEqual(message.count("🔴"), 1)
        self.assertIn(f"\n🟢 {POSITION['product_symbol']}\n", message)
        self.assertIn(f"\n🔴 {LOSING['product_symbol']}\n", message)

    def test_telegram_update_shows_each_accounts_pnl_then_its_positions(self):
        shekhar = ["", "🟢 P-BTC-75200-031126", "Size -100 | PnL ₹63.75"]
        suraj = ["", "🔴 C-BTC-86000-131126", "Size -100 | PnL -₹191.25", "", "P-BTC-70000-131126", "Size -1 | PnL ₹0.00"]
        message, = pnl.format_combined([("Shekhar", 0.75, 63.75, shekhar), ("Suraj", -2.25, -191.25, suraj)])
        self.assertEqual(message, (
            pnl.TITLE_PADDING + "📊 PNL UPDATE📊\n\n"
            "🟢 Shekhar : ₹63.75  ($0.75)\n\n"
            "🔴 Suraj : -₹191.25  (-$2.25)\n\n"
            "=========================\n\n"
            + pnl.NAME_PADDING + "👤 Shekhar\n\n🟢 P-BTC-75200-031126\nSize -100 | PnL ₹63.75\n\n"
            "=========================\n\n"
            + pnl.NAME_PADDING + "👤 Suraj\n\n🔴 C-BTC-86000-131126\nSize -100 | PnL -₹191.25\n\nP-BTC-70000-131126\nSize -1 | PnL ₹0.00\n\n"
            "========================="
        ))

    def test_zero_pnl_has_no_dot(self):
        message, = pnl.format_combined([("Suresh", 0, 0, ["", "x", "y"])])
        self.assertIn("\n\nSuresh : ₹0.00  ($0.00)\n", message)

    def test_long_update_splits_with_the_summary_first(self):
        details = ["", *[f"🟢 POSITION-{n}\n" + "x" * 300 for n in range(5)]]
        reports = [(f"Account {n}", 1.0, 85.0, details) for n in range(6)]
        messages = pnl.format_combined(reports)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= pnl.MESSAGE_LIMIT for message in messages))
        self.assertTrue(messages[0].startswith(pnl.TITLE_PADDING + "📊 PNL UPDATE📊\n\n"))
        for n in range(6):
            self.assertIn(f"Account {n} : ₹85.00  ($1.00)", messages[0])
        self.assertEqual(sum(message.count("👤 Account") for message in messages), 6)
        for message in messages:
            self.assertTrue(message.endswith("\n\n" + pnl.DIVIDER))
        for message in messages[1:]:
            self.assertTrue(message.startswith(pnl.NAME_PADDING + "👤 Account"))

    def test_dollar_format(self):
        for value, expected in ((0, "$0.00"), (-0.001, "$0.00"), (5.753, "$5.75"), (-1234.5, "-$1,234.50")):
            with self.subTest(value=value):
                self.assertEqual(pnl.dollars(value), expected)

    def test_first_live_trade_shows_its_real_small_loss(self):
        _, message = self.check([FIRST_TRADE])
        self.assertIn("Total PnL: -₹0.68", message)
        self.assertNotIn("₹12", message)

    def test_matches_the_upnl_on_the_delta_screen(self):
        _, message = self.check([ADDED_TRADE])
        self.assertIn("Total PnL: -₹146.28", message)

    def test_long_position_uses_the_same_cashflows(self):
        long_position = {**POSITION, "size": 100, "realized_cashflow": "-9.05", "unrealized_cashflow": "9.8"}
        self.assertAlmostEqual(pnl.unrealized_usd(long_position), 0.75)

    def test_rupee_format_uses_indian_grouping(self):
        for value, expected in (
            (0, "₹0.00"), (0.001, "₹0.00"), (-0.001, "₹0.00"), (999.5, "₹999.50"), (1000, "₹1,000.00"),
            (127500.5, "₹1,27,500.50"), (-12345678.9, "-₹1,23,45,678.90"),
        ):
            with self.subTest(value=value):
                self.assertEqual(pnl.rupees(value), expected)

    def test_no_rupee_rate_warns_instead_of_guessing(self):
        for wallets in ([], [{"asset_symbol": "USD", "balance": "0", "balance_inr": "0"}],
                        [{"asset_symbol": "USD", "balance": "100"}]):
            with self.subTest(wallets=wallets):
                kind, message = self.check([POSITION], wallets)
                self.assertEqual(kind, "warning")
                self.assertIn("rupees", message)

    def test_no_open_positions(self):
        self.assertEqual(self.check([CLOSED])[0], "none")

    def test_missing_pnl_field_is_a_warning_not_a_crash(self):
        kind, message = self.check([{"product_symbol": "x", "size": 1}])
        self.assertEqual(kind, "warning")
        self.assertIn("unexpected position data", message)


class DailyLogClearing(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "logs.jsonl"
        for patcher in (mock.patch.object(app, "LOG_FILE", self.file), mock.patch.object(app, "save_settings"),
                        mock.patch.dict(app.settings, {"logs_cleared_on": None})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(app.logs.clear)
        app.logs.clear()
        app.add_log({"message": "old"})

    def test_first_run_keeps_todays_logs(self):
        app.logs_tick(NOW)
        self.assertEqual(len(app.logs), 1)
        self.assertTrue(self.file.exists())
        self.assertEqual(app.settings["logs_cleared_on"], "2026-11-01")

    def test_same_day_keeps_logs(self):
        app.logs_tick(NOW)
        app.logs_tick(NOW.replace(hour=23, minute=59))
        self.assertEqual(len(app.logs), 1)

    def test_new_day_erases_memory_and_file(self):
        app.logs_tick(NOW)
        app.logs_tick(NOW.replace(day=2, hour=0, minute=0, second=0))
        self.assertEqual(len(app.logs), 0)
        self.assertFalse(self.file.exists())
        self.assertEqual(app.settings["logs_cleared_on"], "2026-11-02")

    def test_restart_after_missing_midnight_still_erases(self):
        app.settings["logs_cleared_on"] = "2026-10-30"
        app.load_logs()
        app.logs_tick(NOW)
        self.assertEqual(len(app.logs), 0)
        self.assertFalse(self.file.exists())

    def test_new_logs_after_clearing_are_kept(self):
        app.logs_tick(NOW)
        next_day = NOW.replace(day=2)
        app.logs_tick(next_day)
        app.add_log({"message": "new"})
        app.logs_tick(next_day.replace(hour=23))
        self.assertEqual([entry["message"] for entry in app.logs], ["new"])
        self.assertTrue(self.file.exists())

    def test_missing_file_is_fine(self):
        app.logs_tick(NOW)
        self.file.unlink()
        app.logs_tick(NOW.replace(day=2))
        self.assertEqual(len(app.logs), 0)


class AccountPanel(unittest.TestCase):
    WALLETS = [
        {"asset_symbol": "USD", "balance": "100.5", "available_balance": "80", "blocked_margin": "20.5"},
        {"asset_symbol": "BTC", "balance": "0", "available_balance": "0", "blocked_margin": "0"},
    ]

    def snapshot(self, wallets=None, positions=None):
        wallet_mock = {"side_effect": wallets} if isinstance(wallets, Exception) else {"return_value": wallets or self.WALLETS}
        position_mock = {"side_effect": positions} if isinstance(positions, Exception) else {"return_value": positions or []}
        with mock.patch.object(app.delta_client, "get_wallets", **wallet_mock), \
                mock.patch.object(app.delta_client, "get_positions", **position_mock):
            return app.account_snapshot(CONFIG, NOW)

    def test_shows_only_funded_wallets(self):
        self.assertEqual(self.snapshot()["funds"], [{
            "asset": "USD", "balance": 100.5, "available": 80.0, "blocked": 20.5,
            "balance_inr": None, "available_inr": None, "blocked_inr": None,
        }])

    def test_rupee_figures_use_delta_values_and_implied_rate(self):
        wallets = [{**self.WALLETS[0], "balance_inr": "8040", "available_balance_inr": "6400"}]
        funds = self.snapshot(wallets=wallets)["funds"][0]
        self.assertEqual(funds["balance_inr"], 8040.0)
        self.assertEqual(funds["available_inr"], 6400.0)
        self.assertAlmostEqual(funds["blocked_inr"], 1640.0)

    def test_totals_open_positions(self):
        positions = self.snapshot(positions=[POSITION, LOSING, CLOSED])["positions"]
        self.assertEqual(positions["total"], -1.5)
        self.assertEqual(len(positions["rows"]), 2)
        self.assertEqual(positions["rows"][0]["mark"], 90.5)

    def test_positions_show_rupees_at_the_wallet_rate(self):
        wallets = [{**self.WALLETS[0], "balance": "100", "balance_inr": "8500"}]
        positions = self.snapshot(wallets=wallets, positions=[POSITION])["positions"]
        self.assertAlmostEqual(positions["total"], 0.75)
        self.assertAlmostEqual(positions["total_inr"], 63.75)
        self.assertAlmostEqual(positions["rows"][0]["pnl_inr"], 63.75)

    def test_without_a_rate_positions_stay_in_usd(self):
        positions = self.snapshot(positions=[POSITION])["positions"]
        self.assertAlmostEqual(positions["total"], 0.75)
        self.assertIsNone(positions["total_inr"])
        self.assertIsNone(positions["rows"][0]["pnl_inr"])

    def test_funds_failure_still_shows_positions(self):
        result = self.snapshot(wallets=DeltaError("wallet down"), positions=[POSITION])
        self.assertIsNone(result["funds"])
        self.assertEqual(result["funds_error"], "wallet down")
        self.assertEqual(result["positions"]["total"], 0.75)

    def test_positions_failure_still_shows_funds(self):
        result = self.snapshot(positions=DeltaError("positions down"))
        self.assertIsNone(result["positions"])
        self.assertEqual(result["positions_error"], "positions down")
        self.assertEqual(len(result["funds"]), 1)

    def test_unexpected_data_is_reported_not_raised(self):
        result = self.snapshot(positions=[{"size": 1}])
        self.assertIn("unexpected account data", result["positions_error"])


SUMMARY = (0.75, 63.75, ["", "🟢 P-BTC-75200-031126", "Size -100 | PnL ₹63.75"])
SINGLE_UPDATE = (pnl.TITLE_PADDING + "📊 PNL UPDATE📊\n\n🟢 Main : ₹63.75  ($0.75)\n\n=========================\n\n"
                 + pnl.NAME_PADDING + "👤 Main\n\n🟢 P-BTC-75200-031126\nSize -100 | PnL ₹63.75\n\n=========================")


class PnlSchedule(unittest.TestCase):
    def tick(self, now, check, **config):
        self.live[0].update({"pnl_enabled": True, "pnl_interval_minutes": 5, **config})
        report = (*check, SUMMARY if check[0] == "pnl" else None)
        with mock.patch.object(app, "pnl_report", return_value=report) as check_call, \
                mock.patch.object(app, "add_log") as log, \
                mock.patch.object(app, "notify_telegram", return_value="sent") as notify:
            app.pnl_tick(now)
        return check_call, notify, log

    def setUp(self):
        patcher = mock.patch.dict(app.pnl_state, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        accounts_in_settings(self, CONFIG)
        self.live = app.settings["accounts"]

    def test_runs_on_the_clock_once_per_minute(self):
        signal = ("pnl", "update")
        check, notify, _ = self.tick(NOW, signal)  # 09:30 is a 5-minute mark.
        check.assert_called_once()
        notify.assert_called_once_with("pnl", SINGLE_UPDATE)
        check, _, _ = self.tick(NOW + timedelta(seconds=40), signal)
        check.assert_not_called()
        for minutes in (1, 2, 3, 4):
            check, _, _ = self.tick(NOW + timedelta(minutes=minutes), signal)
            check.assert_not_called()
        check, notify, _ = self.tick(NOW + timedelta(minutes=5), signal)
        notify.assert_called_once()

    def test_off_the_mark_waits_for_the_next_one(self):
        check, _, _ = self.tick(NOW.replace(minute=32), ("pnl", "update"))
        check.assert_not_called()
        check, _, _ = self.tick(NOW.replace(minute=35, second=59), ("pnl", "update"))
        check.assert_called_once()

    def test_accounts_due_together_share_one_message(self):
        for index, name in ((2, "Suraj"), (3, "Suresh")):
            self.live.append({**json.loads(json.dumps(CONFIG)), "id": f"acc{index}", "name": name,
                              "pnl_enabled": True, "pnl_interval_minutes": 5})
        _, notify, _ = self.tick(NOW, ("pnl", "update"))
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "pnl")
        self.assertEqual(len(notify.call_args.args), 2)  # Names are in the lines, so no label on top.
        message = notify.call_args.args[1]
        self.assertTrue(message.startswith(pnl.TITLE_PADDING + "📊 PNL UPDATE📊\n\n🟢 "))
        self.assertNotIn("Total", message)
        for name in ("Main", "Suraj", "Suresh"):
            self.assertIn(f"🟢 {name} : ₹63.75  ($0.75)", message)
        self.assertEqual(message.count("🟢 P-BTC-75200-031126"), 3)
        self.assertLess(message.index("Suresh : "), message.index("👤 Main"))
        self.assertEqual({s["latest"]["telegram"] for s in app.pnl_state.values()}, {"sent"})

    def test_account_without_positions_is_left_out_of_the_combined_message(self):
        self.live[0].update(pnl_enabled=True, pnl_interval_minutes=5)
        self.live.append({**json.loads(json.dumps(CONFIG)), "id": "acc2", "name": "Suraj",
                          "pnl_enabled": True, "pnl_interval_minutes": 5})
        reports = {"acc1": ("pnl", "update", SUMMARY), "acc2": ("none", "No open positions", None)}
        with mock.patch.object(app, "pnl_report", side_effect=lambda account, now: reports[account["id"]]), \
                mock.patch.object(app, "notify_telegram", return_value="sent") as notify:
            app.pnl_tick(NOW)
        notify.assert_called_once_with("pnl", SINGLE_UPDATE)

    def test_no_message_without_open_positions(self):
        _, notify, log = self.tick(NOW, ("none", "No open positions"))
        notify.assert_not_called()
        log.assert_not_called()

    def test_off_does_nothing(self):
        check, _, _ = self.tick(NOW, ("pnl", "update"), pnl_enabled=False)
        check.assert_not_called()

    def test_warns_once_per_outage_and_again_after_recovery(self):
        failure = ("warning", "failed")
        later = lambda minutes: NOW + timedelta(minutes=minutes)
        _, notify, log = self.tick(later(0), failure)
        notify.assert_called_once()
        log.assert_called_once()
        _, notify, log = self.tick(later(5), failure)
        notify.assert_not_called()
        log.assert_not_called()
        self.tick(later(10), ("pnl", "update"))
        _, notify, _ = self.tick(later(15), failure)
        notify.assert_called_once()

    def test_each_account_keeps_its_own_interval(self):
        second = {**CONFIG, "id": "acc2", "name": "Second", "pnl_enabled": True, "pnl_interval_minutes": 1}
        self.live.append(json.loads(json.dumps(second)))
        _, notify, _ = self.tick(NOW, ("pnl", "update"))
        notify.assert_called_once()
        self.assertIn("🟢 Second : ", notify.call_args.args[1])
        self.assertIn("🟢 Main : ", notify.call_args.args[1])
        _, notify, _ = self.tick(NOW + timedelta(minutes=1), ("pnl", "update"))
        notify.assert_called_once()
        self.assertIn("🟢 Second : ", notify.call_args.args[1])
        self.assertNotIn("Main", notify.call_args.args[1])
        self.assertEqual(set(app.pnl_state), {"acc1", "acc2"})

    def test_removed_account_state_is_dropped(self):
        self.tick(NOW, ("pnl", "update"))
        self.live.clear()
        app.pnl_tick(NOW + timedelta(minutes=5))
        self.assertEqual(app.pnl_state, {})

    def test_pnl_message_is_silent(self):
        with mock.patch.object(app.telegram_client, "is_enabled", return_value=True), \
                mock.patch.object(app.telegram_client, "send", return_value=("sent", 9)) as send, \
                mock.patch.object(app.telegram_client, "pin") as pin:
            self.assertEqual(app.notify_telegram("pnl", "update"), "sent")
        send.assert_called_once_with("update", silent=True, all_bold=True)
        pin.assert_not_called()


LIVE = {**CONFIG, "trading_enabled": True, "trade_size": 3}


def tradable_chain():
    tickers = chain(STRIKES)
    for index, ticker in enumerate(tickers):
        ticker.update(product_id=index + 1, tick_size="0.1")
    return tickers


class Trading(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            mock.patch.object(app, "LOG_FILE", Path(folder.name) / "logs.jsonl"),
            mock.patch.object(app.delta_client, "get_option_tickers", return_value=tradable_chain()),
            mock.patch.object(app.delta_client, "get_best_quotes", return_value=(Decimal("103.0"), Decimal("104.0"))),
            mock.patch.object(app.telegram_client, "is_enabled", return_value=False),
            mock.patch.object(app.delta_client, "get_positions", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(app.logs.clear)
        patcher = mock.patch.object(app.delta_client, "place_order", return_value={
            "id": 11, "state": "open", "bracket_stop_loss_price": "206",
        })
        self.place = patcher.start()
        self.addCleanup(patcher.stop)

    def test_order_has_best_bid_and_stop_limit_at_2x_plus_10_percent(self):
        leg = {"symbol": "P", "product_id": 5, "tick_size": "0.1"}
        order = trading.build_order(leg, Decimal("103.0"), 3, "tag")
        self.assertEqual(order["limit_price"], "103")
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["order_type"], "limit_order")
        self.assertEqual(order["size"], 3)
        self.assertEqual(order["bracket_stop_loss_price"], "206")
        self.assertEqual(order["bracket_stop_loss_limit_price"], "226.6")
        self.assertEqual(order["bracket_stop_trigger_method"], "mark_price")

    def test_prices_round_up_to_the_tick(self):
        leg = {"symbol": "P", "product_id": 5, "tick_size": "0.5"}
        order = trading.build_order(leg, Decimal("10.3"), 1, "tag")
        self.assertEqual(order["bracket_stop_loss_price"], "21")
        self.assertEqual(order["bracket_stop_loss_limit_price"], "23.5")

    def test_scheduled_entry_places_both_sides_once(self):
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertEqual(self.place.call_count, 2)
        self.assertEqual(
            sorted(call.args[0]["product_symbol"] for call in self.place.call_args_list),
            ["C-BTC-17450-021126", "P-BTC-17250-021126"],
        )
        self.assertIn("ORDERS PLACED", entry["message"])
        self.assertNotIn(app.ALERT_ONLY, entry["message"])

    def test_run_alert_now_button_never_places_orders(self):
        entry = app.send_alert(LIVE, NOW, "manual")
        self.assertIn(app.ALERT_ONLY, entry["message"])
        self.place.assert_not_called()

    def test_trading_off_places_nothing_on_schedule(self):
        entry = app.send_alert({**LIVE, "trading_enabled": False}, NOW, "scheduled", trade=True)
        self.place.assert_not_called()
        self.assertIn(app.ALERT_ONLY, entry["message"])

    def test_both_sides_adjusted_skips_the_trade(self):
        entry = app.send_alert({**LIVE, "multiplier": 5}, NOW, "scheduled", trade=True)
        self.place.assert_not_called()
        self.assertIn("TRADE SKIPPED", entry["message"])

    def one_side_adjusted(self, adjusted_type):
        limit = (lambda s: s > 17400) if adjusted_type == "call_options" else (lambda s: s < 17300)
        return [t for t in tradable_chain()
                if not (t["contract_type"] == adjusted_type and limit(float(t["strike_price"])))]

    def test_adjusted_call_still_sells_the_put(self):
        with mock.patch.object(app.delta_client, "get_option_tickers",
                               return_value=self.one_side_adjusted("call_options")):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.place.assert_called_once()
        order = self.place.call_args.args[0]
        self.assertEqual(order["product_symbol"], "P-BTC-17250-021126")
        self.assertEqual(order["bracket_stop_loss_price"], "206")
        self.assertIn("ORDERS PLACED", entry["message"])
        self.assertIn("CALL NOT TRADED", entry["message"])
        self.assertIn("holds only the put side", entry["message"])

    def test_adjusted_put_still_sells_the_call(self):
        with mock.patch.object(app.delta_client, "get_option_tickers",
                               return_value=self.one_side_adjusted("put_options")):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.args[0]["product_symbol"], "C-BTC-17450-021126")
        self.assertIn("PUT NOT TRADED", entry["message"])


    def test_missing_atm_ltp_places_nothing(self):
        with mock.patch.object(app.delta_client, "get_option_tickers", return_value=chain(STRIKES, atm_put=None)):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.place.assert_not_called()
        self.assertEqual(entry["kind"], "warning")

    def test_no_bids_places_neither_side(self):
        with mock.patch.object(app.delta_client, "get_best_quotes",
                               side_effect=[(Decimal("103"), Decimal("104")), (None, Decimal("104"))]):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.place.assert_not_called()
        self.assertIn("no bids", entry["message"])

    def test_no_asks_places_neither_side(self):
        with mock.patch.object(app.delta_client, "get_best_quotes", return_value=(Decimal("103"), None)):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.place.assert_not_called()
        self.assertIn("no asks", entry["message"])

    def test_limit_price_follows_the_spread(self):
        tick = Decimal("0.1")
        for bid, ask, price, kind in (
            ("100", "102", "100", "best bid"),
            ("100", "103", "100", "best bid"),
            ("100", "103.5", "101.8", "midpoint"),
            ("100", "104", "102", "midpoint"),
            ("100", "104.1", "104.1", "best ask"),
            ("100", "110", "110", "best ask"),
        ):
            with self.subTest(bid=bid, ask=ask):
                limit, label = trading.choose_limit(Decimal(bid), Decimal(ask), tick)
                self.assertEqual(limit, Decimal(price))
                self.assertIn(kind, label)

    def test_order_and_stoploss_use_the_chosen_limit(self):
        with mock.patch.object(app.delta_client, "get_best_quotes", return_value=(Decimal("100"), Decimal("110"))):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        order = self.place.call_args_list[0].args[0]
        self.assertEqual(order["limit_price"], "110")
        self.assertEqual(order["bracket_stop_loss_price"], "220")
        self.assertEqual(order["bracket_stop_loss_limit_price"], "242")
        self.assertIn("limit 110 (best ask, spread 10)", entry["message"])

    def test_rejected_side_is_reported_and_not_retried(self):
        self.place.side_effect = [{"id": 1, "state": "open", "bracket_stop_loss_price": "206"},
                                  DeltaRejected("Delta rejected the request: HTTP 400, insufficient_margin.")]
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertEqual(self.place.call_count, 2)
        self.assertIn("NOT ALL ORDERS WERE PLACED", entry["message"])
        self.assertIn("insufficient_margin", entry["message"])

    def test_no_clear_reply_warns_order_may_be_live(self):
        self.place.side_effect = DeltaError("No clear reply from Delta: TimeoutError.")
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertEqual(self.place.call_count, 2)
        self.assertIn("may have gone through", entry["message"])

    def test_missing_stoploss_in_response_is_flagged(self):
        self.place.return_value = {"id": 11, "state": "open"}
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertEqual(entry["message"].count("NO STOPLOSS CONFIRMED"), 2)

    def test_unexpected_error_still_sends_the_alert(self):
        self.place.side_effect = RuntimeError("boom")
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertIn("TRADING ERROR", entry["message"])

    def test_scheduler_trades_but_only_once(self):
        accounts_in_settings(self, LIVE)
        with mock.patch.object(app, "save_settings"), mock.patch.object(app, "run_in_background", run_inline):
            app.scheduler_tick(NOW)
            app.scheduler_tick(NOW)
        self.assertEqual(self.place.call_count, 2)

    def test_each_account_orders_with_its_own_keys_and_size(self):
        second = {**LIVE, "id": "acc2", "name": "Second", "api_key": "second-key-99",
                  "api_secret": "second-secret", "trade_size": 7}
        accounts_in_settings(self, LIVE, second)
        used = []

        def place(order):
            used.append((app.delta_client._credentials.get()[0], order["size"]))
            return {"id": 11, "state": "open", "bracket_stop_loss_price": "206"}

        self.place.side_effect = place
        with mock.patch.object(app, "save_settings"), mock.patch.object(app, "run_in_background", run_inline):
            app.scheduler_tick(NOW)
        self.assertEqual(sorted(used), [("main-key-1234", 3), ("main-key-1234", 3),
                                        ("second-key-99", 7), ("second-key-99", 7)])
        self.assertEqual(sorted(entry["account"] for entry in app.logs), ["Main", "Second"])
        self.assertIsNone(app.delta_client._credentials.get())

    def test_accounts_fire_at_their_own_times(self):
        later = {**LIVE, "id": "acc2", "name": "Second", "entry_time": "10:15"}
        accounts_in_settings(self, LIVE, later)
        with mock.patch.object(app, "save_settings"), \
                mock.patch.object(app, "run_in_background") as background:
            app.scheduler_tick(NOW)
            app.scheduler_tick(NOW.replace(hour=10, minute=15))
            app.scheduler_tick(NOW.replace(hour=10, minute=15, second=30))
        self.assertEqual([call.args[1]["name"] for call in background.call_args_list], ["Main", "Second"])

    def test_signed_post_signs_the_exact_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"result": {"id": 1}}')
        order = {"size": 1, "side": "sell"}
        with app.delta_client.using("k", "s"), \
                mock.patch.object(app.delta_client.time, "time", return_value=1700000000), \
                mock.patch.object(app.delta_client.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(ORIGINAL_PLACE_ORDER(order), {"id": 1})
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        expected = hmac.new(b"s", f"POST1700000000/v2/orders{request.data.decode()}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Signature"), expected)
        self.assertEqual(json.loads(request.data), order)


HELD_PUT = {**POSITION, "product_symbol": "P-BTC-17250-021126", "size": -1}
WATCHED = {
    "order_id": 77, "symbol": "P-BTC-17250-021126", "product_id": 5, "tick_size": "0.1", "label": "PUT 17,250",
    "limit": "100", "protected": 0, "warned": False, "uncertain": None, "next_try": 0,
}


def live_order(size=5, unfilled=0, state="closed", average="100"):
    return {"size": size, "unfilled_size": unfilled, "state": state, "average_fill_price": average}


class AddingToHeldStrike(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.account = accounts_in_settings(self, LIVE)[0]
        for patcher in (
            mock.patch.object(app, "LOG_FILE", Path(folder.name) / "logs.jsonl"),
            mock.patch.object(app, "save_settings"),
            mock.patch.object(app.delta_client, "get_option_tickers", return_value=tradable_chain()),
            mock.patch.object(app.delta_client, "get_best_quotes", return_value=(Decimal("103"), Decimal("104"))),
            mock.patch.object(app.delta_client, "get_positions", return_value=[HELD_PUT]),
            mock.patch.object(app.telegram_client, "is_enabled", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(app.logs.clear)
        patcher = mock.patch.object(app.delta_client, "place_order", return_value={
            "id": 77, "state": "open", "bracket_stop_loss_price": "206",
        })
        self.place = patcher.start()
        self.addCleanup(patcher.stop)

    def orders_by_symbol(self):
        return {call.args[0]["product_symbol"]: call.args[0] for call in self.place.call_args_list}

    def test_held_strike_sells_without_a_bracket_and_is_watched(self):
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        orders = self.orders_by_symbol()
        self.assertFalse([key for key in orders["P-BTC-17250-021126"] if key.startswith("bracket")])
        self.assertEqual(orders["C-BTC-17450-021126"]["bracket_stop_loss_price"], "206")
        watched = self.account["pending_stops"]
        self.assertEqual([item["symbol"] for item in watched], ["P-BTC-17250-021126"])
        self.assertIn("separate SL after each fill", entry["message"])
        self.assertNotIn("NO STOPLOSS CONFIRMED", entry["message"])

    def test_long_position_is_not_treated_as_held(self):
        long_position = {**HELD_PUT, "size": 1}
        with mock.patch.object(app.delta_client, "get_positions", return_value=[long_position]):
            app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertEqual(self.account["pending_stops"], [])
        self.assertIn("bracket_stop_loss_price", self.orders_by_symbol()["P-BTC-17250-021126"])

    def test_unknown_positions_skip_the_trade(self):
        with mock.patch.object(app.delta_client, "get_positions", side_effect=DeltaError("down")):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.place.assert_not_called()
        self.assertIn("TRADE SKIPPED", entry["message"])

    def test_no_clear_reply_on_held_strike_warns_of_no_stoploss(self):
        self.place.side_effect = [{"id": 1, "state": "open", "bracket_stop_loss_price": "2"}, DeltaError("No clear reply.")]
        entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertIn("NO STOPLOSS", entry["message"])
        self.assertEqual(self.account["pending_stops"], [])

    def test_rejected_entry_is_not_watched_and_has_no_holding_note(self):
        self.place.side_effect = DeltaRejected("Delta rejected the request.")
        tickers = [t for t in tradable_chain()
                   if not (t["contract_type"] == "call_options" and float(t["strike_price"]) > 17400)]
        with mock.patch.object(app.delta_client, "get_option_tickers", return_value=tickers):
            entry = app.send_alert(LIVE, NOW, "scheduled", trade=True)
        self.assertEqual(self.account["pending_stops"], [])
        self.assertIn("CALL NOT TRADED", entry["message"])
        self.assertNotIn("holds only", entry["message"])


class WatchOrder(unittest.TestCase):
    def setUp(self):
        for name, value in (("get_order", {"return_value": live_order()}),
                            ("find_order_by_client_id", {"return_value": None}),
                            ("get_pending_orders", {"return_value": []}),
                            ("cancel_order", {"return_value": {}}),
                            ("place_order", {"return_value": {"id": 88, "state": "pending"}})):
            patcher = mock.patch.object(app.delta_client, name, **value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.item = dict(WATCHED)

    def test_full_fill_places_one_reduce_only_stop_and_finishes(self):
        done, messages = trading.watch_order(self.item, 1000)
        self.assertTrue(done)
        self.assertIn("STOPLOSS PLACED", messages[0])
        order = self.place_order.call_args.args[0]
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["size"], 5)
        self.assertEqual(order["order_type"], "limit_order")
        self.assertEqual(order["stop_order_type"], "stop_loss_order")
        self.assertEqual(order["stop_price"], "200")
        self.assertEqual(order["limit_price"], "220")
        self.assertEqual(order["stop_trigger_method"], "mark_price")
        self.assertIs(order["reduce_only"], True)

    def test_stop_uses_the_actual_fill_price(self):
        self.get_order.return_value = live_order(average="105")
        trading.watch_order(self.item, 1000)
        self.assertEqual(self.place_order.call_args.args[0]["stop_price"], "210")

    def test_no_fill_waits_without_placing(self):
        self.get_order.return_value = live_order(unfilled=5, state="open", average=None)
        self.assertEqual(trading.watch_order(self.item, 1000), (False, []))
        self.place_order.assert_not_called()

    def test_partial_fills_each_get_their_own_size(self):
        self.get_order.return_value = live_order(size=5, unfilled=3, state="open")
        done, _ = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertEqual(self.place_order.call_args.args[0]["size"], 2)
        self.assertEqual(trading.watch_order(self.item, 1001), (False, []))
        self.place_order.assert_called_once()
        self.get_order.return_value = live_order(size=5, unfilled=0, state="closed")
        done, _ = trading.watch_order(self.item, 1002)
        self.assertTrue(done)
        self.assertEqual(self.place_order.call_args.args[0]["size"], 3)

    def test_cancelled_unfilled_order_stops_watching(self):
        self.get_order.return_value = live_order(unfilled=5, state="cancelled", average=None)
        self.assertEqual(trading.watch_order(self.item, 1000), (True, []))
        self.place_order.assert_not_called()

    def test_cancelled_part_filled_order_protects_the_fill_first(self):
        self.get_order.return_value = live_order(size=5, unfilled=3, state="cancelled")
        done, _ = trading.watch_order(self.item, 1000)
        self.assertTrue(done)
        self.assertEqual(self.place_order.call_args.args[0]["size"], 2)

    def test_rejected_stop_warns_once_and_retries_later(self):
        self.place_order.side_effect = DeltaRejected("Delta rejected the request.")
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertIn("NO STOPLOSS YET", messages[0])
        self.assertEqual(trading.watch_order(self.item, 1001), (False, []))
        self.place_order.assert_called_once()
        self.assertEqual(trading.watch_order(self.item, 1000 + trading.RETRY_SECONDS), (False, []))
        self.assertEqual(self.place_order.call_count, 2)
        self.place_order.side_effect = None
        done, messages = trading.watch_order(self.item, 1000 + 2 * trading.RETRY_SECONDS)
        self.assertTrue(done)
        self.assertIn("STOPLOSS PLACED", messages[0])

    def test_no_clear_reply_finds_the_stop_and_never_doubles_it(self):
        self.place_order.side_effect = DeltaError("No clear reply.")
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertIn("NO STOPLOSS YET", messages[0])
        self.find_order_by_client_id.return_value = {"id": 88}
        done, messages = trading.watch_order(self.item, 1000 + trading.RETRY_SECONDS)
        self.assertTrue(done)
        self.find_order_by_client_id.assert_called_once_with("SL77F5")
        self.place_order.assert_called_once()

    def test_no_clear_reply_retries_when_the_stop_was_not_placed(self):
        self.place_order.side_effect = [DeltaError("No clear reply."), {"id": 88}]
        trading.watch_order(self.item, 1000)
        done, messages = trading.watch_order(self.item, 1000 + trading.RETRY_SECONDS)
        self.assertTrue(done)
        self.assertEqual(self.place_order.call_count, 2)
        self.assertIn("STOPLOSS PLACED", messages[0])

    def test_failed_order_read_warns_and_keeps_watching(self):
        self.get_order.side_effect = DeltaError("down")
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertIn("NO STOPLOSS YET", messages[0])
        self.place_order.assert_not_called()

    def test_unexpected_order_data_warns(self):
        self.get_order.return_value = {"state": "closed"}
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertIn("Unexpected order data", messages[0])


class SplittingTheBracket(WatchOrder):
    BRACKET = {"id": 55, "size": 6, "bracket_order": True, "stop_price": "270", "limit_price": "297",
               "stop_trigger_method": "mark_price"}

    def setUp(self):
        super().setUp()
        self.get_pending_orders.return_value = [{"id": 90, "size": 5, "bracket_order": None}, dict(self.BRACKET)]

    def calls(self):
        return self.place_order.call_args_list

    def test_each_trade_gets_its_own_stop_and_the_bracket_goes(self):
        done, messages = trading.watch_order(self.item, 1000)
        self.assertTrue(done)
        new, older = (call.args[0] for call in self.calls())
        self.assertEqual((new["size"], new["stop_price"], new["limit_price"]), (5, "200", "220"))
        self.assertEqual((older["size"], older["stop_price"], older["limit_price"]), (1, "270", "297"))
        self.assertIs(older["reduce_only"], True)
        self.assertEqual(older["side"], "buy")
        self.assertEqual(older["stop_order_type"], "stop_loss_order")
        self.cancel_order.assert_called_once_with(55, 5)
        self.assertIn("SEPARATE STOPLOSSES", messages[1])

    def test_bracket_is_cancelled_only_after_the_older_stop_is_placed(self):
        events = []
        self.place_order.side_effect = lambda order: events.append(("place", order["size"])) or {"id": 1}
        self.cancel_order.side_effect = lambda *args: events.append(("cancel", args[0]))
        trading.watch_order(self.item, 1000)
        self.assertEqual(events, [("place", 5), ("place", 1), ("cancel", 55)])

    def test_rejected_older_stop_keeps_the_bracket(self):
        self.place_order.side_effect = [{"id": 1}, DeltaRejected("Delta rejected the request.")]
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.cancel_order.assert_not_called()
        self.assertIn("CHECK STOPLOSSES", messages[-1])

    def test_failed_cancel_retries_without_a_second_stop(self):
        self.cancel_order.side_effect = [DeltaError("No clear reply."), {}]
        done, _ = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertEqual(self.place_order.call_count, 2)
        self.find_order_by_client_id.return_value = {"id": 91}
        done, _ = trading.watch_order(self.item, 1000 + trading.RETRY_SECONDS)
        self.assertTrue(done)
        self.find_order_by_client_id.assert_called_with("SB55")
        self.assertEqual(self.place_order.call_count, 2)
        self.assertEqual(self.cancel_order.call_count, 2)

    def test_waits_while_the_bracket_has_not_resized(self):
        self.get_pending_orders.return_value = [{**self.BRACKET, "size": 1}]
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.place_order.assert_called_once()
        self.cancel_order.assert_not_called()
        self.assertEqual(len(messages), 1)
        self.get_pending_orders.return_value = [dict(self.BRACKET)]
        done, _ = trading.watch_order(self.item, 1000 + trading.BRACKET_RESIZE_WAIT_SECONDS)
        self.assertTrue(done)
        self.assertEqual(self.place_order.call_args.args[0]["size"], 1)

    def test_warns_if_the_bracket_never_resizes_and_keeps_it(self):
        self.get_pending_orders.return_value = [{**self.BRACKET, "size": 1}]
        self.item["split_tries"] = trading.BRACKET_RESIZE_TRIES - 1
        self.item["protected"] = 5
        done, messages = trading.watch_order(self.item, 1000)
        self.assertFalse(done)
        self.assertIn("CHECK STOPLOSSES", messages[0])
        self.cancel_order.assert_not_called()

    def test_other_watched_fills_are_not_counted_as_older_lots(self):
        self.get_pending_orders.return_value = [{**self.BRACKET, "size": 8}]
        self.get_order.side_effect = lambda order_id: live_order() if order_id == 77 else live_order(size=2)
        trading.watch_order(self.item, 1000, [78])
        self.assertEqual(self.place_order.call_args.args[0]["size"], 1)

    def test_no_bracket_means_only_the_new_stop(self):
        self.get_pending_orders.return_value = [{"id": 90, "size": 5, "bracket_order": None}]
        done, _ = trading.watch_order(self.item, 1000)
        self.assertTrue(done)
        self.place_order.assert_called_once()
        self.cancel_order.assert_not_called()

    def test_unfilled_order_leaves_the_bracket_alone(self):
        self.get_order.return_value = live_order(unfilled=5, state="cancelled", average=None)
        self.assertEqual(trading.watch_order(self.item, 1000), (True, []))
        self.get_pending_orders.assert_not_called()

    def test_later_partial_fills_do_not_split_again(self):
        self.get_order.return_value = live_order(size=5, unfilled=3, state="open")
        self.get_pending_orders.return_value = [{**self.BRACKET, "size": 3}]
        trading.watch_order(self.item, 1000)
        self.assertEqual(self.cancel_order.call_count, 1)
        self.get_order.return_value = live_order(size=5, unfilled=0, state="closed")
        done, _ = trading.watch_order(self.item, 1001)
        self.assertTrue(done)
        self.assertEqual(self.cancel_order.call_count, 1)
        self.assertEqual(self.place_order.call_args.args[0]["size"], 3)

    # The scenarios below belong to the parent class and assume no bracket on the contract.
    test_full_fill_places_one_reduce_only_stop_and_finishes = None
    test_stop_uses_the_actual_fill_price = None
    test_partial_fills_each_get_their_own_size = None
    test_cancelled_part_filled_order_protects_the_fill_first = None
    test_rejected_stop_warns_once_and_retries_later = None
    test_no_clear_reply_finds_the_stop_and_never_doubles_it = None
    test_no_clear_reply_retries_when_the_stop_was_not_placed = None


class StopsTick(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            mock.patch.object(app, "LOG_FILE", Path(folder.name) / "logs.jsonl"),
            mock.patch.object(app, "save_settings"),
            mock.patch.object(app.telegram_client, "is_enabled", return_value=False),
            mock.patch.object(app.delta_client, "place_order", return_value={"id": 88}),
            mock.patch.object(app.delta_client, "get_pending_orders", return_value=[]),
        ):
            setattr(self, "last", patcher.start())
            self.addCleanup(patcher.stop)
        self.addCleanup(app.logs.clear)

    def test_finished_order_is_removed_and_logged_once(self):
        account, = accounts_in_settings(self, {**CONFIG, "pending_stops": [dict(WATCHED)]})
        with mock.patch.object(app.delta_client, "get_order", return_value=live_order()):
            app.stops_tick(NOW)
            self.assertEqual(account["pending_stops"], [])
            app.stops_tick(NOW + timedelta(seconds=1))
        self.assertEqual(len(app.logs), 1)
        self.assertEqual(app.logs[0]["source"], "stoploss")
        self.assertEqual(app.logs[0]["account"], "Main")

    def test_open_order_keeps_being_watched(self):
        account, = accounts_in_settings(self, {**CONFIG, "pending_stops": [dict(WATCHED)]})
        with mock.patch.object(app.delta_client, "get_order",
                               return_value=live_order(unfilled=5, state="open", average=None)):
            app.stops_tick(NOW)
            self.assertEqual(len(account["pending_stops"]), 1)
        self.assertEqual(len(app.logs), 0)

    def test_watcher_sees_other_deployments_orders_on_the_same_account(self):
        first, = accounts_in_settings(self, {**CONFIG, "pending_stops": [dict(WATCHED)]})
        second = {**json.loads(json.dumps(first)), "id": "dep2", "pending_stops": [{**WATCHED, "order_id": 78}]}
        app.settings["deployments"].append(second)
        seen = {}

        def watch(item, now, others):
            seen[item["order_id"]] = list(others)
            return False, []

        with mock.patch.object(app.trading, "watch_order", side_effect=watch):
            app.stops_tick(NOW)
        self.assertEqual(seen, {77: [78], 78: [77]})

    def test_each_account_watches_with_its_own_keys(self):
        second = {**CONFIG, "id": "acc2", "name": "Second", "api_key": "second-key-99",
                  "pending_stops": [{**WATCHED, "order_id": 78}]}
        accounts_in_settings(self, {**CONFIG, "pending_stops": [dict(WATCHED)]}, second)
        keys = {}

        def get_order(order_id):
            keys[order_id] = app.delta_client._credentials.get()[0]
            return live_order(unfilled=5, state="open", average=None)

        with mock.patch.object(app.delta_client, "get_order", side_effect=get_order):
            app.stops_tick(NOW)
        self.assertEqual(keys, {77: "main-key-1234", 78: "second-key-99"})


TARGET_LIVE = {**LIVE, "id": "tgt1", "strategy": "straddle_target", "target_percent": 30.0, "open_trades": []}


class TargetStrategy(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.deployment, = accounts_in_settings(self, TARGET_LIVE)
        for patcher in (
            mock.patch.object(app, "LOG_FILE", Path(folder.name) / "logs.jsonl"),
            mock.patch.object(app, "save_settings"),
            mock.patch.object(app.delta_client, "get_option_tickers", return_value=tradable_chain()),
            mock.patch.object(app.delta_client, "get_best_quotes", return_value=(Decimal("103"), Decimal("104"))),
            mock.patch.object(app.delta_client, "get_positions", return_value=[]),
            mock.patch.object(app.telegram_client, "is_enabled", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(app.logs.clear)
        patcher = mock.patch.object(app.delta_client, "place_order", side_effect=self.place)
        self.place_order = patcher.start()
        self.addCleanup(patcher.stop)
        self.next_id = 70

    def place(self, order):
        self.next_id += 1
        return {"id": self.next_id, "state": "open"}

    def job(self):
        return app.job_copy(self.deployment["id"])

    def test_alert_shows_the_target_instead_of_stoplosses(self):
        kind, message = app.build_alert(self.job(), NOW)
        self.assertEqual(kind, "signal")
        self.assertNotIn("Stoploss", message)
        self.assertIn("🎯 Target: 30% of premium, no stoploss. Held to expiry if the target isn't reached.", message)
        _, stoploss_message = app.build_alert(CONFIG, NOW)
        self.assertIn("Stoploss: 10", stoploss_message)
        self.assertNotIn("🎯 Target", stoploss_message)

    def test_entry_has_no_stoploss_and_starts_a_target_watch(self):
        entry = app.send_alert(self.job(), NOW, "scheduled", trade=True)
        orders = [call.args[0] for call in self.place_order.call_args_list]
        self.assertEqual(len(orders), 2)
        for order in orders:
            self.assertFalse([key for key in order if key.startswith("bracket")])
            self.assertTrue(order["client_order_id"].startswith("2611010930tgt1"))
        self.assertIn("No stoploss: this strategy exits at its overall target.", entry["message"])
        self.assertIn("🎯 Watching for the target: 30% of premium", entry["message"])
        self.assertNotIn("NO STOPLOSS", entry["message"])
        watched = self.deployment["pending_stops"]
        self.assertEqual([(w["order_id"], w["no_stop"]) for w in watched], [(71, True), (72, True)])
        trade, = self.deployment["open_trades"]
        self.assertEqual((trade["expiry"], trade["target_percent"], trade["state"]), ("021126", 30.0, "open"))
        self.assertEqual([leg["entry_order_id"] for leg in trade["legs"]], [71, 72])
        self.assertEqual(entry["strategy"], "Straddle + Target %")

    def test_stoploss_strategy_still_uses_brackets(self):
        accounts_in_settings(self, LIVE)
        app.send_alert(app.job_copy(LIVE["id"]), NOW, "scheduled", trade=True)
        for call in self.place_order.call_args_list:
            self.assertIn("bracket_stop_loss_price", call.args[0])

    def test_watched_entry_gets_no_stop_of_its_own_but_splits_a_bracket(self):
        item = {**WATCHED, "no_stop": True}
        bracket = {"id": 9, "bracket_order": True, "size": 8, "stop_price": "200", "limit_price": "220"}
        with mock.patch.object(app.delta_client, "get_order", return_value=live_order(size=5)), \
                mock.patch.object(app.delta_client, "get_pending_orders", return_value=[bracket]), \
                mock.patch.object(app.delta_client, "find_order_by_client_id", return_value=None), \
                mock.patch.object(app.delta_client, "cancel_order") as cancel:
            done, messages = trading.watch_order(item, 100)
        self.assertTrue(done)
        placed = [call.args[0] for call in self.place_order.call_args_list]
        self.assertEqual([(o["size"], o["stop_price"]) for o in placed], [(3, "200")])  # Only the older 3 lots.
        cancel.assert_called_once_with(9, 5)
        self.assertNotIn("STOPLOSS PLACED", "".join(messages))

    def test_watch_progress_is_saved_and_finished_trades_removed(self):
        self.deployment["open_trades"] = [{"id": "a"}, {"id": "b"}]

        def step(trade, now, rate):
            if trade["id"] == "a":
                trade["state"] = "exiting"
                return False, ["🎯 TARGET HIT: profit 106 points ≥ target 100.8"]
            return True, ["✅ TARGET BOOKED: all legs bought back"]

        with mock.patch.object(app.target, "step", side_effect=step), \
                mock.patch.object(app, "save_settings") as save:
            app.target_tick(NOW)
        self.assertEqual(self.deployment["open_trades"], [{"id": "a", "state": "exiting"}])
        save.assert_called()
        self.assertEqual([(e["source"], e["kind"], e["account"]) for e in app.logs],
                         [("target", "target", "Main"), ("target", "target", "Main")])

    def test_unchanged_watch_is_not_saved_every_check(self):
        self.deployment["open_trades"] = [{"id": "a", "profit": "1", "next_try": 0}]

        def step(trade, now, rate):
            trade.update(profit="2", next_try=5)
            return False, []

        with mock.patch.object(app.target, "step", side_effect=step), \
                mock.patch.object(app, "save_settings") as save:
            app.target_tick(NOW)
        save.assert_not_called()
        self.assertEqual(self.deployment["open_trades"][0]["profit"], "2")

    def test_watch_uses_the_accounts_keys(self):
        self.deployment["open_trades"] = [{"id": "a"}]
        seen = []
        with mock.patch.object(app.target, "step",
                               side_effect=lambda *a: (seen.append(app.delta_client._credentials.get()), (False, []))[1]):
            app.target_tick(NOW)
        self.assertEqual(seen, [("main-key-1234", "main-secret")])

    def test_cannot_remove_while_a_trade_is_watched(self):
        self.deployment["open_trades"] = [{"id": "a"}]
        with self.assertRaisesRegex(ValueError, "watched for their target"):
            app.remove_deployment({"id": self.deployment["id"]})
        with self.assertRaisesRegex(ValueError, "watched for their target"):
            app.remove_account({"id": self.deployment["account_id"]})

    def test_target_percent_is_checked(self):
        body = {**TradeValidation.BODY}
        self.assertEqual(app.validate({**body, "target_percent": 25})["target_percent"], 25.0)
        for bad in (0, -5, 101, True, "30", float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.validate({**body, "target_percent": bad})


LISTED = [{"value": "121126", "label": "12 Nov 2026"}, {"value": "131126", "label": "13 Nov 2026"},
          {"value": "141126", "label": "14 Nov 2026"}]


class AutoExpiry(unittest.TestCase):
    def resolve(self, expiry, now):
        with mock.patch.object(app.delta_client, "list_expiries", return_value=LISTED) as listed:
            return app.resolve_expiry({**CONFIG, "expiry": expiry}, now), listed

    def test_days_ahead_counts_from_the_entry_date(self):
        evening = datetime(2026, 11, 12, 22, 0, tzinfo=app.IST)
        self.assertEqual(self.resolve("auto:2", evening)[0], "141126")  # The user's example.
        self.assertEqual(self.resolve("auto:1", evening)[0], "131126")

    def test_time_of_day_does_not_change_the_date(self):
        for hour in (0, 9, 17, 23):
            with self.subTest(hour=hour):
                self.assertEqual(self.resolve("auto:2", datetime(2026, 11, 12, hour, 5, tzinfo=app.IST))[0], "141126")

    def test_same_day_only_before_its_1730_expiry(self):
        self.assertEqual(self.resolve("auto:0", datetime(2026, 11, 12, 9, 30, tzinfo=app.IST))[0], "121126")
        with self.assertRaisesRegex(SignalError, "already expired"):
            self.resolve("auto:0", datetime(2026, 11, 12, 17, 30, tzinfo=app.IST))

    def test_ist_date_is_used_even_when_utc_is_still_the_day_before(self):
        just_after_midnight_ist = datetime(2026, 11, 11, 18, 40, tzinfo=timezone.utc)  # 12 Nov 00:10 IST.
        self.assertEqual(self.resolve("auto:2", just_after_midnight_ist)[0], "141126")

    def test_fixed_expiry_is_used_as_it_is(self):
        expiry, listed = self.resolve("141126", NOW)
        self.assertEqual(expiry, "141126")
        listed.assert_not_called()

    def test_unlisted_date_is_a_warning_never_another_expiry(self):
        evening = datetime(2026, 11, 12, 22, 0, tzinfo=app.IST)
        with self.assertRaisesRegex(SignalError, "no BTC expiry for 15 Nov 2026"):
            self.resolve("auto:3", evening)
        with mock.patch.object(app.delta_client, "list_expiries", return_value=LISTED[:2]):
            kind, message, _ = app.evaluate({**CONFIG, "expiry": "auto:2"}, evening)
        self.assertEqual(kind, "warning")
        self.assertIn("Expiry 2 days ahead (auto)", message)
        self.assertIn("nothing was traded", message)

    def test_entry_trades_the_resolved_expiry(self):
        tickers = [dict(t, symbol=t["symbol"].replace("021126", "131126")) for t in chain(STRIKES)]
        evening = datetime(2026, 11, 12, 22, 30, tzinfo=app.IST)
        with mock.patch.object(app.delta_client, "list_expiries", return_value=LISTED), \
                mock.patch.object(app.delta_client, "get_option_tickers", return_value=tickers):
            kind, message, result = app.evaluate({**CONFIG, "expiry": "auto:1"}, evening)
        self.assertEqual(kind, "signal")
        self.assertEqual(result["expiry"], "131126")
        self.assertIn("Expiry 13 Nov 2026", message)
        self.assertIn("C-BTC-17450-131126", message)

    def test_expiry_is_automatic_unless_a_date_is_chosen(self):
        for value, expected in (("auto:2", "auto:2"), ("", "auto:2"), (None, "auto:2"), ("141126", "141126")):
            with self.subTest(value=value):
                self.assertEqual(app.validate({**TradeValidation.BODY, "expiry": value})["expiry"], expected)
        for bad in ("auto:1", "auto:3", "../x", "14112", 141126):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.validate({**TradeValidation.BODY, "expiry": bad})

    def test_manual_expiry_is_kept_across_restarts(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        file = Path(folder.name) / "settings.json"
        account = app.new_account("Shekhar", "k" * 10, "s" * 10)
        fixed = app.new_deployment(account["id"], "straddle_stoploss", expiry="141126", last_fired="2026-11-12 22:30")
        file.write_text(json.dumps({"accounts": [account], "deployments": [fixed], "logs_cleared_on": None}),
                        encoding="utf-8")
        with mock.patch.object(app, "SETTINGS_FILE", file), \
                mock.patch.dict(app.settings, {"accounts": [], "deployments": [], "logs_cleared_on": None}):
            app.load_settings()
            deployment, = app.settings["deployments"]
            self.assertEqual(deployment["expiry"], "141126")
            self.assertEqual(deployment["last_fired"], "2026-11-12 22:30")

    def test_target_trade_keeps_the_resolved_expiry(self):
        accounts_in_settings(self, {**TARGET_LIVE, "expiry": "auto:1"})
        tickers = [dict(t, symbol=t["symbol"].replace("021126", "131126")) for t in tradable_chain()]
        evening = datetime(2026, 11, 12, 22, 30, tzinfo=app.IST)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with mock.patch.object(app, "LOG_FILE", Path(folder.name) / "logs.jsonl"), \
                mock.patch.object(app, "save_settings"), \
                mock.patch.object(app.delta_client, "list_expiries", return_value=LISTED), \
                mock.patch.object(app.delta_client, "get_option_tickers", return_value=tickers), \
                mock.patch.object(app.delta_client, "get_best_quotes", return_value=(Decimal("103"), Decimal("104"))), \
                mock.patch.object(app.delta_client, "get_positions", return_value=[]), \
                mock.patch.object(app.delta_client, "place_order", return_value={"id": 7, "state": "open"}), \
                mock.patch.object(app.telegram_client, "is_enabled", return_value=False):
            app.send_alert(app.job_copy(TARGET_LIVE["id"]), evening, "scheduled", trade=True)
        self.addCleanup(app.logs.clear)
        trade, = app.find_deployment(TARGET_LIVE["id"])["open_trades"]
        self.assertEqual(trade["expiry"], "131126")
        self.assertEqual(app.find_deployment(TARGET_LIVE["id"])["expiry"], "auto:1")  # The choice stays auto.


class TradeValidation(unittest.TestCase):
    BODY = {"symbol": "BTC", "expiry": "021126", "multiplier": 1.8,
            "atm_reference": "spot", "entry_time": "09:30", "enabled": True}

    def test_trading_is_off_unless_explicitly_true(self):
        self.assertFalse(app.DEFAULTS["trading_enabled"])
        for value in (None, "true", 1):
            self.assertFalse(app.validate({**self.BODY, "trading_enabled": value})["trading_enabled"])

    def test_rejects_bad_sizes(self):
        for size in (0, -1, 1.5, 1001, True, "1", None):
            with self.subTest(size=size), self.assertRaises(ValueError):
                app.validate({**self.BODY, "trade_size": size})


class SignedRequests(unittest.TestCase):
    def test_signature_follows_delta_format(self):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"result": []}')
        with app.delta_client.using("my-key", "my-secret"), \
                mock.patch.object(app.delta_client.time, "time", return_value=1700000000.9), \
                mock.patch.object(app.delta_client.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(app.delta_client.get_positions(), [])
        request = urlopen.call_args.args[0]
        expected = hmac.new(b"my-secret", b"GET1700000000/v2/positions/margined", hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("Signature"), expected)
        self.assertEqual(request.get_header("Timestamp"), "1700000000")
        self.assertEqual(request.get_header("Api-key"), "my-key")
        self.assertTrue(request.get_header("User-agent"))

    def test_missing_keys_raise_a_clear_error(self):
        env = {"DELTA_API_KEY": "env-key", "DELTA_API_SECRET": "env-secret"}
        with mock.patch.dict(app.os.environ, env), self.assertRaisesRegex(DeltaError, "no Delta API key"):
            app.delta_client.get_positions()  # .env keys are never used without an account.
        with app.delta_client.using("", ""), self.assertRaisesRegex(DeltaError, "no Delta API key"):
            app.delta_client.get_positions()

    def test_keys_apply_only_inside_the_block(self):
        with app.delta_client.using("a", "b"):
            with app.delta_client.using("c", "d"):
                self.assertEqual(app.delta_client._credentials.get(), ("c", "d"))
            self.assertEqual(app.delta_client._credentials.get(), ("a", "b"))
        self.assertIsNone(app.delta_client._credentials.get())

    def test_rejection_reports_code_without_secrets(self):
        body = io.BytesIO(b'{"error": {"code": "UnauthorizedApiAccess"}}')
        rejected = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, body)
        with app.delta_client.using("my-key", "my-secret"), \
                mock.patch.object(app.delta_client.urllib.request, "urlopen", side_effect=rejected), \
                self.assertRaises(DeltaError) as caught:
            app.delta_client.get_positions()
        self.assertIn("UnauthorizedApiAccess", str(caught.exception))
        self.assertNotIn("my-secret", str(caught.exception))
        self.assertNotIn("my-key", str(caught.exception))


class Schedule(unittest.TestCase):
    def test_due_only_at_entry_time(self):
        self.assertTrue(app.is_due(CONFIG, NOW))
        self.assertFalse(app.is_due(CONFIG, NOW.replace(minute=31)))
        self.assertFalse(app.is_due({**CONFIG, "enabled": False}, NOW))
        self.assertFalse(app.is_due({**CONFIG, "expiry": ""}, NOW))

    def test_fires_once_per_day_and_again_next_day(self):
        fired = {**CONFIG, "last_fired": app.fire_key(CONFIG, NOW)}
        self.assertFalse(app.is_due(fired, NOW))
        self.assertTrue(app.is_due(fired, NOW.replace(day=2)))

    def test_tick_sends_a_single_alert(self):
        account, = accounts_in_settings(self, CONFIG)
        with mock.patch.object(app, "save_settings"), \
                mock.patch.object(app, "run_in_background") as background:
            app.scheduler_tick(NOW)
            app.scheduler_tick(NOW)
        background.assert_called_once()
        self.assertEqual(background.call_args.args[0], app.send_alert)
        self.assertEqual(account["last_fired"], "2026-11-01 09:30")

    def test_deployment_without_its_account_never_runs(self):
        deployment, = accounts_in_settings(self, CONFIG)
        app.settings["accounts"].clear()
        with mock.patch.object(app, "save_settings"), \
                mock.patch.object(app, "run_in_background") as background:
            app.scheduler_tick(NOW)
        background.assert_not_called()
        self.assertEqual(deployment["last_fired"], "")

    def test_failed_entry_thread_is_logged(self):
        with mock.patch.object(app, "report_error") as report:
            app._guarded(mock.Mock(side_effect=RuntimeError("boom")), ())
        self.assertEqual(report.call_args.args[0], "entry")


class Accounts(unittest.TestCase):
    OLD_SETTINGS = {
        "symbol": "BTC", "expiry": "041126", "multiplier": 1.8, "atm_reference": "spot", "entry_time": "22:30",
        "enabled": True, "last_fired": "2026-11-02 22:30", "pinned_message_id": 202, "pnl_enabled": True,
        "pnl_interval_minutes": 1, "trading_enabled": True, "trade_size": 300, "logs_cleared_on": "2026-11-03",
        "pending_stops": [{"order_id": 5}],
    }

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "settings.json"
        patcher = mock.patch.object(app, "SETTINGS_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(app.settings, {"accounts": [], "deployments": [], "logs_cleared_on": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(app.os.environ, {"DELTA_API_KEY": "env-key-1234", "DELTA_API_SECRET": "env-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def check_split(self, old_account, account, deployment):
        """The account keeps its keys and PnL settings, and the deployment everything else."""
        self.assertEqual(deployment["account_id"], account["id"])
        self.assertEqual(deployment["strategy"], "straddle_stoploss")
        for key in app.ACCOUNT_DEFAULTS:
            self.assertEqual(account[key], old_account[key], key)
            self.assertNotIn(key, deployment)
        for key in app.DEPLOYMENT_DEFAULTS:
            expected = "auto:2" if key == "expiry" else old_account.get(key, app.DEPLOYMENT_DEFAULTS[key])
            self.assertEqual(deployment[key], expected, key)
            self.assertNotIn(key, account)

    def test_single_account_settings_become_main_account_and_deployment(self):
        self.file.write_text(json.dumps(self.OLD_SETTINGS), encoding="utf-8")
        app.load_settings()
        account, = app.settings["accounts"]
        deployment, = app.settings["deployments"]
        self.assertEqual((account["name"], account["api_key"], account["api_secret"]),
                         ("Main", "env-key-1234", "env-secret"))
        self.check_split(self.OLD_SETTINGS, account, deployment)
        self.assertEqual(app.settings["logs_cleared_on"], "2026-11-03")
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"][0]["id"], account["id"])
        self.assertEqual(saved["deployments"][0]["id"], deployment["id"])
        self.assertNotIn("symbol", saved)

    def test_accounts_become_accounts_with_one_deployment_each(self):
        shekhar = {**self.OLD_SETTINGS, "id": "7de062b4", "name": "Shekhar", "api_key": "k" * 10, "api_secret": "s" * 10}
        suraj = {**shekhar, "id": "50892d37", "name": "Suraj", "entry_time": "09:30", "last_fired": "",
                 "pinned_message_id": None, "pending_stops": [], "pnl_interval_minutes": 5}
        del shekhar["logs_cleared_on"], suraj["logs_cleared_on"]
        self.file.write_text(json.dumps({"accounts": [shekhar, suraj], "logs_cleared_on": "2026-11-12"}), encoding="utf-8")
        app.load_settings()
        self.assertEqual([a["id"] for a in app.settings["accounts"]], ["7de062b4", "50892d37"])
        for old, account, deployment in zip((shekhar, suraj), app.settings["accounts"], app.settings["deployments"]):
            self.assertEqual((account["name"], account["api_key"], account["api_secret"]),
                             (old["name"], old["api_key"], old["api_secret"]))
            self.check_split(old, account, deployment)
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["deployments"]), 2)

    def test_migrated_deployment_does_not_fire_again_the_same_day(self):
        self.file.write_text(json.dumps(self.OLD_SETTINGS), encoding="utf-8")
        app.load_settings()
        self.assertFalse(app.is_due(app.settings["deployments"][0], datetime(2026, 11, 2, 22, 30, 30, tzinfo=app.IST)))

    def test_new_settings_load_as_they_are(self):
        account = app.new_account("Second", "k" * 10, "s" * 10)
        deployment = app.new_deployment(account["id"], "straddle_stoploss", entry_time="10:00")
        data = {"accounts": [account], "deployments": [deployment], "logs_cleared_on": None}
        self.file.write_text(json.dumps(data), encoding="utf-8")
        app.load_settings()
        self.assertEqual(app.settings["accounts"], [account])
        self.assertEqual(app.settings["deployments"], [deployment])
        self.assertEqual(self.file.read_text(encoding="utf-8"), json.dumps(data))  # Not rewritten.

    def test_no_keys_in_env_means_no_account_is_made_up(self):
        self.file.write_text(json.dumps(self.OLD_SETTINGS), encoding="utf-8")
        with mock.patch.dict(app.os.environ, {"DELTA_API_KEY": ""}):
            app.load_settings()
        self.assertEqual(app.settings["accounts"], [])
        self.assertEqual(app.settings["deployments"], [])

    @unittest.skipIf(sys.platform == "win32", "Windows has no Unix file modes")
    def test_settings_file_is_private(self):
        app.save_settings()
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def add(self, **body):
        body = {"name": "Second", "api_key": "second-key-99", "api_secret": "second-secret", **body}
        with mock.patch.object(app.delta_client, "get_wallets", return_value=[]) as wallets:
            return app.add_account(body), wallets

    def test_add_checks_keys_and_starts_with_trading_off(self):
        seen = []
        with mock.patch.object(app.delta_client, "get_wallets",
                               side_effect=lambda: seen.append(app.delta_client._credentials.get())):
            shown = app.add_account({"name": " Second ", "api_key": "second-key-99", "api_secret": "second-secret"})
        self.assertEqual(seen, [("second-key-99", "second-secret")])
        account, = app.settings["accounts"]
        self.assertEqual((account["name"], account["api_key"], account["api_secret"]),
                         ("Second", "second-key-99", "second-secret"))
        self.assertFalse(account["pnl_enabled"])
        self.assertEqual(app.settings["deployments"], [])  # Strategies are deployed separately.
        self.assertNotIn("api_secret", shown)
        self.assertNotIn("api_key", shown)
        self.assertEqual(shown["api_key_hint"], "…y-99")
        self.assertTrue(self.file.exists())

    def test_rejected_keys_are_not_saved(self):
        with mock.patch.object(app.delta_client, "get_wallets",
                               side_effect=DeltaRejected("Delta rejected the request: HTTP 401, ip_not_whitelisted_for_api_key.")), \
                self.assertRaisesRegex(ValueError, "ip_not_whitelisted"):
            app.add_account({"name": "Second", "api_key": "second-key-99", "api_secret": "second-secret"})
        self.assertEqual(app.settings["accounts"], [])

    def test_bad_input_is_refused_before_calling_delta(self):
        for body in ({"name": ""}, {"name": "x" * 31}, {"name": None}, {"api_key": "short"},
                     {"api_key": "has space in it"}, {"api_secret": None}, {"api_secret": "é" * 10}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                _, wallets = self.add(**body)
                wallets.assert_not_called()

    def test_duplicate_name_or_key_is_refused(self):
        self.add()
        for body in ({"name": "second", "api_key": "other-key-1"}, {"name": "Third"}):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "already"):
                self.add(**body)
        self.assertEqual(len(app.settings["accounts"]), 1)

    def test_remove_keeps_other_accounts(self):
        first, _ = self.add()
        self.add(name="Third", api_key="third-key-123")
        app.pnl_state[first["id"]] = {"latest": None}
        self.addCleanup(app.pnl_state.pop, first["id"], None)
        app.remove_account({"id": first["id"]})
        self.assertEqual([a["name"] for a in app.settings["accounts"]], ["Third"])
        self.assertNotIn(first["id"], app.pnl_state)

    def test_remove_is_refused_while_stops_are_pending(self):
        shown, _ = self.add()
        deployment = app.add_deployment({"account_id": shown["id"], "strategy": "straddle_stoploss"})
        app.find_deployment(deployment["id"])["pending_stops"] = [{"order_id": 1}]
        with self.assertRaisesRegex(ValueError, "waiting for their stoplosses"):
            app.remove_account({"id": shown["id"]})
        self.assertEqual(len(app.settings["accounts"]), 1)

    def test_removing_an_account_removes_only_its_deployments(self):
        first, _ = self.add()
        third, _ = self.add(name="Third", api_key="third-key-123")
        for account in (first, third):
            app.add_deployment({"account_id": account["id"], "strategy": "straddle_stoploss"})
        app.remove_account({"id": first["id"]})
        self.assertEqual([d["account_id"] for d in app.settings["deployments"]], [third["id"]])

    def test_deploy_starts_with_everything_off(self):
        shown, _ = self.add()
        deployment = app.add_deployment({"account_id": shown["id"], "strategy": "straddle_stoploss"})
        live, = app.settings["deployments"]
        self.assertEqual(live, deployment)
        self.assertEqual((live["account_id"], live["strategy"]), (shown["id"], "straddle_stoploss"))
        self.assertFalse(live["enabled"] or live["trading_enabled"])
        self.assertEqual(live["expiry"], "auto:2")

    def test_same_strategy_can_be_deployed_twice_on_one_account(self):
        shown, _ = self.add()
        for _ in range(2):
            app.add_deployment({"account_id": shown["id"], "strategy": "straddle_stoploss"})
        self.assertEqual(len({d["id"] for d in app.settings["deployments"]}), 2)

    def test_deploy_refuses_unknown_strategy_or_account(self):
        shown, _ = self.add()
        for body in ({"account_id": shown["id"], "strategy": "nope"}, {"account_id": "nope", "strategy": "straddle_stoploss"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                app.add_deployment(body)
        self.assertEqual(app.settings["deployments"], [])

    def test_remove_deployment(self):
        shown, _ = self.add()
        keep = app.add_deployment({"account_id": shown["id"], "strategy": "straddle_stoploss"})
        drop = app.add_deployment({"account_id": shown["id"], "strategy": "straddle_stoploss"})
        app.find_deployment(drop["id"])["pending_stops"] = [{"order_id": 1}]
        with self.assertRaisesRegex(ValueError, "waiting for their stoplosses"):
            app.remove_deployment({"id": drop["id"]})
        app.find_deployment(drop["id"])["pending_stops"] = []
        app.remove_deployment({"id": drop["id"]})
        self.assertEqual([d["id"] for d in app.settings["deployments"]], [keep["id"]])
        self.assertEqual(len(app.settings["accounts"]), 1)
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            app.remove_deployment({"id": drop["id"]})

    def test_job_has_account_name_and_keys_with_the_deployment_id(self):
        shown, _ = self.add()
        deployment = app.add_deployment({"account_id": shown["id"], "strategy": "straddle_stoploss"})
        job = app.job_copy(deployment["id"])
        self.assertEqual((job["id"], job["account_id"], job["name"], job["api_key"], job["api_secret"]),
                         (deployment["id"], shown["id"], "Second", "second-key-99", "second-secret"))
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            app.job_copy("nope")

    def test_account_pnl_settings_are_saved_on_the_account(self):
        shown, _ = self.add()
        app.save_account_pnl({"id": shown["id"], "pnl_enabled": True, "pnl_interval_minutes": 1})
        account, = app.settings["accounts"]
        self.assertEqual((account["pnl_enabled"], account["pnl_interval_minutes"]), (True, 1))
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            app.save_account_pnl({"id": "nope", "pnl_interval_minutes": 1})

    def test_unknown_account_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            app.remove_account({"id": "nope"})
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            app.account_copy("nope")

    def test_page_never_sees_secrets(self):
        shown = app.public_account(CONFIG)
        self.assertNotIn("api_secret", shown)
        self.assertNotIn("main-secret", json.dumps(shown))
        self.assertNotIn("main-key-1234", json.dumps(shown))


class Validation(unittest.TestCase):
    BODY = {"symbol": "BTC", "expiry": "021126", "multiplier": 1.8,
            "atm_reference": "spot", "entry_time": "09:30", "enabled": True}

    def test_accepts_good_settings(self):
        self.assertEqual(app.validate(self.BODY)["multiplier"], 1.8)

    def test_rejects_bad_settings(self):
        bad = [{"symbol": "DOGE"}, {"multiplier": 0}, {"multiplier": True}, {"multiplier": "1.8"},
               {"multiplier": float("nan")}, {"atm_reference": "mark"}, {"entry_time": "25:00"}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                app.validate({**self.BODY, **change})

    def test_strategy_settings_leave_pnl_settings_alone(self):
        self.assertNotIn("pnl_enabled", app.validate({**self.BODY, "pnl_enabled": True}))

    def test_pnl_settings(self):
        self.assertEqual(app.validate_pnl({"pnl_enabled": True, "pnl_interval_minutes": 1}),
                         {"pnl_enabled": True, "pnl_interval_minutes": 1})
        self.assertFalse(app.validate_pnl({"pnl_enabled": "true"})["pnl_enabled"])
        for interval in (0, 1441, 2.5, True, None):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                app.validate_pnl({"pnl_interval_minutes": interval})


class DashboardAccess(unittest.TestCase):
    PUBLIC = "65.2.227.93:8765"
    REMOTE = "203.0.113.9"
    GOOD_COOKIE = "dashboard_key=secret-key"

    def setUp(self):
        patcher = mock.patch.dict(app.os.environ, {"DASHBOARD_KEY": "secret-key", "PUBLIC_HOST": "65.2.227.93"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_local_address_needs_a_local_client(self):
        self.assertTrue(app.is_allowed("127.0.0.1:8765", "127.0.0.1", None))
        self.assertTrue(app.is_allowed("localhost:8765", "127.0.0.1", None))

    def test_remote_client_cannot_fake_the_local_address(self):
        self.assertFalse(app.is_allowed("127.0.0.1:8765", self.REMOTE, None))
        self.assertFalse(app.is_allowed("localhost:8765", self.REMOTE, self.GOOD_COOKIE))

    def test_public_address_needs_the_right_key(self):
        self.assertTrue(app.is_allowed(self.PUBLIC, self.REMOTE, self.GOOD_COOKIE))
        self.assertTrue(app.is_allowed(self.PUBLIC, self.REMOTE, f"other=1; {self.GOOD_COOKIE}"))
        for cookie in (None, "", "dashboard_key=wrong", "other=secret-key", "dashboard_key=", "bad;;=cookie"):
            with self.subTest(cookie=cookie):
                self.assertFalse(app.is_allowed(self.PUBLIC, self.REMOTE, cookie))

    def test_loopback_client_still_needs_the_key_on_the_public_address(self):
        self.assertFalse(app.is_allowed(self.PUBLIC, "127.0.0.1", None))

    def test_other_hosts_are_refused_even_with_the_key(self):
        for host in ("evil.example", "65.2.227.93", "65.2.227.93:80", None):
            with self.subTest(host=host):
                self.assertFalse(app.is_allowed(host, self.REMOTE, self.GOOD_COOKIE))

    def test_public_address_is_closed_without_a_key_set(self):
        for missing in ("DASHBOARD_KEY", "PUBLIC_HOST"):
            with self.subTest(missing=missing), mock.patch.dict(app.os.environ):
                del app.os.environ[missing]
                self.assertFalse(app.is_allowed(self.PUBLIC, self.REMOTE, self.GOOD_COOKIE))

    def test_empty_key_never_matches(self):
        self.assertFalse(app.key_matches(""))
        with mock.patch.dict(app.os.environ, {"DASHBOARD_KEY": ""}):
            self.assertFalse(app.key_matches(""))
            self.assertFalse(app.is_allowed(self.PUBLIC, self.REMOTE, "dashboard_key="))


if __name__ == "__main__":
    unittest.main()
