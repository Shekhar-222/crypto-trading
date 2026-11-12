import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import target
from delta_client import DeltaError, DeltaRejected

NOW = datetime(2026, 11, 12, 22, 31, tzinfo=target.IST)
CALL = "C-BTC-81200-141126"
PUT = "P-BTC-74800-141126"


class FakeDelta:
    """Delta's order book and orders, as far as the target watcher sees them."""

    def __init__(self):
        self.orders = {}
        self.asks = {CALL: Decimal("100"), PUT: Decimal("150")}
        self.next_id = 500
        self.placed = []
        self.cancelled = []
        self.place_error = None

    def add(self, order_id, size, filled=0, price=None, state="open", **extra):
        self.orders[order_id] = {"size": size, "filled": filled, "avg": price, "state": state, **extra}

    def fill(self, order_id, filled, price):
        order = self.orders[order_id]
        order.update(filled=filled, avg=price)
        if filled == order["size"]:
            order["state"] = "closed"

    def get_order(self, order_id):
        order = self.orders[order_id]
        return {"id": order_id, "size": order["size"], "unfilled_size": order["size"] - order["filled"],
                "average_fill_price": order["avg"], "state": order["state"]}

    def cancel_order(self, order_id, product_id):
        self.cancelled.append(order_id)
        order = self.orders[order_id]
        if order["state"] != "open":
            raise DeltaRejected("Delta rejected the request: HTTP 400, order_not_open.")
        order["state"] = "cancelled"

    def place_order(self, order):
        self.placed.append(order)
        if self.place_error:
            error, self.place_error = self.place_error, None
            if isinstance(error, DeltaRejected) or "lost" not in str(error):
                raise error
            self.add(self.next_id, order["size"], limit=order["limit_price"], client=order["client_order_id"])
            self.next_id += 1
            raise error  # Delta took it, but the reply was lost.
        order_id = self.next_id
        self.next_id += 1
        self.add(order_id, order["size"], limit=order["limit_price"], client=order["client_order_id"])
        return {"id": order_id, "state": "open"}

    def find_order_by_client_id(self, client_order_id):
        for order_id, order in self.orders.items():
            if order.get("client") == client_order_id:
                return {"id": order_id, "limit_price": order["limit"]}
        return None

    def get_best_quotes(self, symbol):
        return Decimal("1"), self.asks.get(symbol)


def watch_item(name, symbol, order_id, product_id):
    return {"order_id": order_id, "symbol": symbol, "product_id": product_id, "tick_size": "0.1",
            "label": f"{name} {symbol.split('-')[2]}", "limit": "100", "protected": 0, "warned": False,
            "uncertain": None, "next_try": 0, "no_stop": True, "name": name, "strike": 0}


class TargetTest(unittest.TestCase):
    def setUp(self):
        self.delta = FakeDelta()
        for name in ("get_order", "cancel_order", "place_order", "find_order_by_client_id", "get_best_quotes"):
            patcher = mock.patch.object(target.delta_client, name, side_effect=getattr(self.delta, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = NOW
        # Sold 3 calls at 135 and 3 puts at 201: premium 336, target 30% = 100.8 points.
        self.delta.add(11, 3, 3, "135", "closed")
        self.delta.add(12, 3, 3, "201", "closed")
        self.trade = target.new_trade(
            [watch_item("CALL", CALL, 11, 1), watch_item("PUT", PUT, 12, 2)], "2611122230abcd", "141126", 30.0)

    def step(self, seconds=6, rate=None):
        self.clock += timedelta(seconds=seconds)
        return target.step(self.trade, self.clock, rate)

    def buys(self):
        return [o for o in self.delta.placed if o["side"] == "buy"]

    def hit_target(self):
        self.delta.asks = {CALL: Decimal("60"), PUT: Decimal("170")}  # Profit 75 + 31 = 106 points.
        return self.step()

    def test_announces_the_target_once_fills_are_known(self):
        _, messages = self.step()
        self.assertEqual(messages, [
            "🎯 TARGET WATCH: sold CALL 81200 at 135, PUT 74800 at 201 (size 3)\n"
            "Premium 336 | Target 30% = 100.8 points\nExits when buying back costs 235.2 or less (best ask)."
        ])
        self.assertEqual(self.step()[1], [])

    def test_below_target_nothing_is_bought(self):
        done, _ = self.step()  # Profit 35 + 51 = 86 points.
        self.assertFalse(done)
        self.assertEqual(self.trade["profit"], "86")
        self.assertEqual(self.trade["target"], "100.8")
        self.assertEqual(self.buys(), [])
        self.assertEqual(self.trade["state"], "open")

    def test_exactly_at_target_exits(self):
        self.delta.asks = {CALL: Decimal("100"), PUT: Decimal("135.2")}  # 35 + 65.8 = 100.8.
        _, messages = self.step()
        self.assertEqual(self.trade["state"], "exiting")
        self.assertIn("🎯 TARGET HIT: profit 100.8 points ≥ target 100.8", messages[-1])

    def test_target_hit_buys_back_both_legs_at_best_ask_reduce_only(self):
        done, messages = self.hit_target()
        self.assertFalse(done)
        self.assertIn("🎯 TARGET HIT: profit 106 points ≥ target 100.8\nBuying back: CALL 81200 at 60, PUT 74800 at 170",
                      messages[-1])
        buys = self.buys()
        self.assertEqual([(o["product_symbol"], o["size"], o["limit_price"], o["reduce_only"]) for o in buys],
                         [(CALL, 3, "60", True), (PUT, 3, "170", True)])
        self.assertEqual(len({o["client_order_id"] for o in buys}), 2)

    def test_filled_exits_book_the_profit(self):
        self.hit_target()
        call_exit, put_exit = (o for o in self.delta.orders if o >= 500)
        self.delta.fill(call_exit, 3, "60")
        self.delta.fill(put_exit, 3, "170")
        done, messages = self.step(rate=lambda: 85.0)
        self.assertTrue(done)
        self.assertEqual(messages[-1], (
            "✅ TARGET BOOKED: all legs bought back\n"
            "CALL 81200: sold 135, bought back 60\nPUT 74800: sold 201, bought back 170\n"
            "Profit: 106 points (size 3)\nBooked: ₹27.03 ($0.32)"
        ))
        self.assertEqual(len(self.buys()), 2)

    def test_booking_without_a_rupee_rate_shows_dollars(self):
        self.hit_target()
        for order_id in [o for o in self.delta.orders if o >= 500]:
            self.delta.fill(order_id, 3, "60" if order_id == 500 else "170")

        def no_rate():
            raise DeltaError("down")

        _, messages = self.step(rate=no_rate)
        self.assertTrue(messages[-1].endswith("Booked: $0.32"))

    def test_unfilled_exit_is_repriced_to_the_new_ask_only_after_cancelling(self):
        self.hit_target()
        call_exit = 500
        self.delta.fill(501, 3, "170")  # The put fills straight away.
        self.step(seconds=4)  # Too soon to re-price.
        self.assertEqual(self.delta.cancelled, [])
        self.delta.asks[CALL] = Decimal("62")
        self.step(seconds=6)
        self.assertEqual(self.delta.cancelled, [call_exit])
        self.assertEqual(len(self.buys()), 2)  # Nothing new until the cancel is confirmed.
        self.delta.orders[call_exit]["filled"] = 1  # One lot filled before the cancel.
        self.delta.orders[call_exit]["avg"] = "60"
        self.step()
        new = self.buys()[-1]
        self.assertEqual((new["product_symbol"], new["size"], new["limit_price"]), (CALL, 2, "62"))
        self.assertNotEqual(new["client_order_id"], self.buys()[0]["client_order_id"])
        self.delta.fill(502, 2, "62")
        done, messages = self.step()
        self.assertTrue(done)
        self.assertIn("CALL 81200: sold 135, bought back 61.33", messages[-1])

    def test_same_ask_is_not_repriced(self):
        self.hit_target()
        self.step(seconds=30)
        self.assertEqual(self.delta.cancelled, [])

    def test_lost_reply_is_found_and_never_doubled(self):
        self.delta.place_error = DeltaError("No clear reply from Delta: lost.")
        self.hit_target()
        self.assertEqual(len(self.buys()), 2)
        self.step()
        self.assertEqual(len(self.buys()), 2)  # The call exit was found by its client id.
        self.assertEqual(self.trade["legs"][0]["exit_order_id"], 500)

    def test_failed_send_is_retried_with_a_new_client_id(self):
        self.delta.place_error = DeltaError("No clear reply from Delta: timeout.")
        self.hit_target()
        self.step()
        call_buys = [o for o in self.buys() if o["product_symbol"] == CALL]
        self.assertEqual(len(call_buys), 2)
        self.assertNotEqual(call_buys[0]["client_order_id"], call_buys[1]["client_order_id"])

    def test_refused_exit_warns_once_and_retries_later(self):
        self.delta.place_error = DeltaRejected("Delta rejected the request: HTTP 400, insufficient_margin.")
        _, messages = self.hit_target()
        self.assertIn("⚠️ TARGET WATCH PROBLEM", messages[-1])
        self.assertIn("insufficient_margin", messages[-1])
        self.assertEqual(self.step()[1], [])  # Waits before retrying, and doesn't warn again.
        self.step(seconds=31)
        self.assertEqual(len([o for o in self.buys() if o["product_symbol"] == CALL]), 2)

    def test_part_filled_entry_is_cancelled_at_target_and_only_fills_are_exited(self):
        self.delta.add(12, 3, 2, "201", "open")
        self.hit_target()
        self.assertIn(12, self.delta.cancelled)
        put_buy = [o for o in self.buys() if o["product_symbol"] == PUT][0]
        self.assertEqual(put_buy["size"], 2)

    def test_entry_filling_more_before_its_cancel_is_also_exited(self):
        self.delta.add(12, 3, 2, "201", "open")
        self.delta.cancel_order = mock.Mock()  # The cancel hasn't happened yet.
        with mock.patch.object(target.delta_client, "cancel_order", side_effect=lambda *a: None):
            self.hit_target()
            self.delta.fill(501, 2, "170")
            self.delta.fill(12, 3, "201")  # The last lot filled before the cancel.
            self.step()
        put_buys = [o["size"] for o in self.buys() if o["product_symbol"] == PUT]
        self.assertEqual(put_buys, [2, 1])

    def test_no_seller_on_a_leg_waits(self):
        self.delta.asks = {CALL: Decimal("1"), PUT: None}
        done, _ = self.step()
        self.assertFalse(done)
        self.assertEqual(self.trade["state"], "open")
        self.assertEqual(self.buys(), [])

    def test_expiry_stops_watching_without_buying(self):
        self.step()
        expiry = datetime(2026, 11, 14, 17, 30, tzinfo=target.IST)
        done, messages = target.step(self.trade, expiry, None)
        self.assertTrue(done)
        self.assertEqual(messages, ["⌛ EXPIRED: the target wasn't reached, so Delta settles the legs at expiry."
                                    "\nLast check: profit 86 of 100.8 points."])
        self.assertEqual(self.buys(), [])

    def test_before_expiry_time_keeps_watching(self):
        done, _ = target.step(self.trade, datetime(2026, 11, 14, 17, 29, tzinfo=target.IST), None)
        self.assertFalse(done)

    def test_nothing_filled_stops_watching(self):
        self.delta.add(11, 3, 0, None, "cancelled")
        self.delta.add(12, 3, 0, None, "cancelled")
        done, messages = self.step()
        self.assertTrue(done)
        self.assertIn("no entry order filled", messages[0])

    def test_unfilled_entries_keep_waiting(self):
        self.delta.add(11, 3, 0, None, "open")
        self.delta.add(12, 3, 0, None, "open")
        self.assertEqual(self.step(), (False, []))

    def test_one_sided_trade_targets_its_own_premium(self):
        self.trade = target.new_trade([watch_item("PUT", PUT, 12, 2)], "t", "141126", 30.0)
        _, messages = self.step()
        self.assertIn("Premium 201 | Target 30% = 60.3 points", messages[0])
        self.delta.asks[PUT] = Decimal("140")  # Profit 61.
        self.step()
        self.assertEqual(self.trade["state"], "exiting")

    def test_different_sizes_use_totals(self):
        self.delta.add(12, 3, 1, "201", "closed")
        _, messages = self.step()
        self.assertIn("different sizes", messages[0])
        self.assertIn("Premium 606 | Target 30% = 181.8 points", messages[0])

    def test_delta_down_warns_once_then_recovers(self):
        with mock.patch.object(target.delta_client, "get_order", side_effect=DeltaError("down")):
            _, first = self.step()
            _, second = self.step(seconds=31)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.step(seconds=31)
        self.assertFalse(self.trade["warned"])

    def test_checks_wait_between_runs(self):
        self.step()
        with mock.patch.object(target.delta_client, "get_best_quotes") as quotes:
            target.step(self.trade, self.clock + timedelta(seconds=2), None)
        quotes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
