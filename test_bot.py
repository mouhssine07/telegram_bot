"""Offline checks: isolated files and fake Telegram API; never runs the live bot."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bot = types.ModuleType("isolated_bot")
        self.bot.__file__ = str(root / "bot.py")
        source = Path(__file__).with_name("bot.py").read_text(encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            exec(compile(source, self.bot.__file__, "exec"), self.bot.__dict__)
        self.bot.ADMIN_CHAT_ID = "999"
        self.products = {"p0001": {"sku": "p0001", "name": "Test watch", "price_dh": 120}}
        self.bot.save_products(self.products)
        self.db = self.bot.connect()
        self.addCleanup(lambda: self.db.close())
        self.calls = []
        self.next_id = 100
        self.bot.api = self.api

    def api(self, method, data=None, timeout=15):
        self.calls.append((method, dict(data or {})))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def text(self, chat_id, text, message_id=10):
        self.bot.handle_text({"chat": {"id": chat_id, "type": "private"},
                              "message_id": message_id, "text": text},
                             self.products, self.db, "TestBot")

    def callback(self, chat_id, data, message_id):
        self.bot.handle_callback({"id": "callback", "data": data,
                                  "message": {"message_id": message_id,
                                              "chat": {"id": chat_id, "type": "private"}}},
                                 self.products, self.db)

    def fill_form(self, chat_id=101):
        self.text(chat_id, "/start p_p0001_channel", 1)
        self.callback(chat_id, "order:p0001:channel", self.next_id)
        for message_id, value in enumerate(["10", "Customer X", "0612345678", "Rabat", "Test street"], 2):
            self.text(chat_id, value, message_id)
        self.callback(chat_id, "note:done", self.next_id)
        return self.next_id

    def insert_order(self, chat_id, name):
        self.db.execute("""INSERT INTO orders
            (chat_id, sku, product_name, unit_price_dh, quantity,
             customer_name, phone, city, address, source)
            VALUES (?, 'p0001', 'Test watch', 120, 1, ?, '0612345678', 'Rabat', 'Test street', 'test')""",
                        (chat_id, name))
        self.db.commit()

    def sent_text(self):
        return "\n".join(data["text"] for method, data in self.calls if method == "sendMessage")


class OrderFlowTests(BotTestCase):
    def test_channel_link_shows_single_product_photo_before_order_card(self):
        self.db.execute("INSERT INTO published_posts VALUES (999, 5, 'p0001', '@channel', 77)")
        self.db.commit()
        self.text(101, "/start p_p0001_channel")
        self.assertEqual(self.calls[0][0], "copyMessage")
        photo = self.calls[0][1]
        self.assertEqual((photo["chat_id"], photo["from_chat_id"], photo["message_id"]), (101, "@channel", 77))
        self.assertEqual(photo["caption"], "Test watch")
        self.assertEqual(json.loads(photo["reply_markup"]), {"inline_keyboard": []})
        self.assertIn("order:p0001:channel", self.calls[-1][1]["reply_markup"])

    def test_channel_link_reuses_album_photos_in_original_order_after_restart(self):
        self.db.execute("INSERT INTO published_albums VALUES (999, 'a', 'p0001', '@channel', '[70,71]', NULL)")
        for mid in [6, 5]:
            self.db.execute("INSERT INTO album_items VALUES (999, 'a', ?, ?, '', NULL)", (mid, f"photo-{mid}"))
        self.db.commit()
        self.db.close()
        self.db = self.bot.connect()
        self.text(101, "/start p_p0001_channel")
        self.assertEqual(self.calls[0][0], "sendMediaGroup")
        media = json.loads(self.calls[0][1]["media"])
        self.assertEqual([p["media"] for p in media], ["photo-5", "photo-6"])
        self.assertEqual(media[0]["caption"], "Test watch")
        self.assertNotIn("caption", media[1])
        self.assertIn("order:p0001:channel", self.calls[-1][1]["reply_markup"])

    def test_legacy_album_uses_channel_copy_and_missing_media_does_not_block_ordering(self):
        self.db.execute("INSERT INTO published_albums VALUES (999, 'a', 'p0001', '@channel', '[70,71]', NULL)")
        self.db.commit()
        self.text(101, "/start p_p0001_channel")
        self.assertEqual(self.calls[0][0], "copyMessages")
        self.assertEqual(json.loads(self.calls[0][1]["message_ids"]), [70, 71])
        self.calls.clear()
        def fail_media(method, data=None, timeout=15):
            if method == "copyMessages":
                raise RuntimeError("Media unavailable")
            return self.api(method, data, timeout)
        self.bot.api = fail_media
        self.text(101, "/start p_p0001_channel")
        self.assertIn("order:p0001:channel", self.calls[-1][1]["reply_markup"])
        self.callback(101, "order:p0001:channel", self.next_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], "quantity")

    def test_catalog_imported_photo_and_photo_preservation_after_checkout(self):
        self.db.execute("INSERT INTO source_imports VALUES (999, 5, 'p0001')")
        self.db.commit()
        self.callback(101, "view:p0001", 80)
        photo = next(data for method, data in self.calls if method == "copyMessage")
        self.assertEqual((photo["from_chat_id"], photo["message_id"]), (999, 5))
        def media_api(method, data=None, timeout=15):
            if method == "copyMessage":
                self.calls.append((method, dict(data)))
                return {"message_id": 9000}
            return self.api(method, data, timeout)
        self.bot.api = media_api
        self.callback(101, "confirm", self.fill_form())
        deleted = [mid for method, data in self.calls if method == "deleteMessages"
                   for mid in json.loads(data["message_ids"])]
        self.assertNotIn(9000, deleted)

    def test_product_without_media_or_unknown_sku_never_copies_another_product(self):
        self.db.execute("INSERT INTO published_posts VALUES (999, 5, 'other', '@channel', 77)")
        self.db.commit()
        self.text(101, "/start p_p0001_channel")
        self.text(101, "/start p_removed_channel")
        self.assertFalse(any(method in ("copyMessage", "copyMessages", "sendMediaGroup") for method, data in self.calls))

    def start_quantity(self, chat_id=101, quantity="10"):
        self.text(chat_id, "/start")
        self.callback(chat_id, "order:p0001:catalog", self.next_id)
        self.text(chat_id, quantity)

    def test_minimum_quantity_blocks_small_orders_and_legacy_confirmation(self):
        self.start_quantity(quantity="9")
        self.assertEqual(self.bot.session(self.db, 101)[0], "quantity")
        for value in ["0", "1", "1001"]:
            self.text(101, value)
            self.assertEqual(self.bot.session(self.db, 101)[0], "quantity")
        self.text(101, "10")
        self.assertEqual(self.bot.session(self.db, 101)[0], "name")
        confirm_id = self.fill_form()
        _, data = self.bot.session(self.db, 101)
        data["quantity"] = 1
        self.bot.put_session(self.db, 101, "confirm", data)
        self.callback(101, "confirm", confirm_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(self.bot.session(self.db, 101)[0], "quantity")

    def test_stock_below_minimum_cannot_start_checkout(self):
        self.text(999, "/stock p0001 9")
        self.start_quantity()
        self.assertIsNone(self.bot.session(self.db, 101))

    def test_returning_customer_reuses_own_details_after_restart(self):
        self.insert_order(101, "Returning customer")
        self.insert_order(202, "Other customer")
        self.start_quantity()
        step, data = self.bot.session(self.db, 101)
        self.assertEqual(step, "details")
        self.assertEqual(data["name"], "Returning customer")
        self.assertNotIn("Other customer", self.sent_text())
        self.db.close()
        self.db = self.bot.connect()
        self.callback(101, "details:use", data["details_message_id"])
        self.callback(101, "note:done", self.next_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], "confirm")
        self.callback(101, "confirm", self.next_id)
        order = self.db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((order["customer_name"], order["quantity"], order["chat_id"]), ("Returning customer", 10, 101))

    def test_returning_customer_edits_only_one_field_and_old_buttons_are_ignored(self):
        self.insert_order(101, "Returning customer")
        self.start_quantity()
        old_id = self.next_id
        self.callback(101, "details:phone", old_id)
        self.text(101, "invalid")
        self.assertEqual(self.bot.session(self.db, 101)[0], "phone")
        self.text(101, "0699999999")
        new_id = self.next_id
        self.callback(101, "details:use", old_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], "details")
        self.callback(101, "details:use", new_id)
        self.callback(101, "note:done", self.next_id)
        summary_id = self.next_id
        self.callback(101, "details:edit", summary_id)
        self.callback(101, "details:city", self.next_id)
        self.text(101, "Casablanca")
        self.callback(101, "confirm", summary_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], "details")
        self.callback(101, "details:use", self.bot.session(self.db, 101)[1]["details_message_id"])
        self.callback(101, "confirm", self.next_id)
        old, new = self.db.execute("SELECT * FROM orders ORDER BY id").fetchall()
        self.assertEqual((old["phone"], old["city"]), ("0612345678", "Rabat"))
        self.assertEqual((new["phone"], new["city"], new["customer_name"]), ("0699999999", "Casablanca", "Returning customer"))

    def test_missing_saved_field_is_requested_and_cancel_does_not_change_saved_details(self):
        self.insert_order(101, "Returning customer")
        self.db.execute("UPDATE orders SET address='' WHERE chat_id=101")
        self.db.commit()
        self.start_quantity()
        self.callback(101, "details:use", self.next_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], "address")
        self.text(101, "New address")
        self.callback(101, "note:done", self.next_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], "confirm")
        self.text(101, "/cancel")
        self.assertEqual(self.db.execute("SELECT address FROM orders").fetchone()[0], "")
        self.start_quantity(chat_id=202)
        self.assertEqual(self.bot.session(self.db, 202)[0], "name")

    def test_modern_caption_uses_current_price_and_valid_emoji_entity_offsets(self):
        product = {"sku": "p0001", "name": "⌚ ساعة", "price_dh": 150,
                   "description": "⌚ ساعة — ذهبية ✨ ---- 120 درهم"}
        caption, entities = self.bot.product_post_caption(product, "TestBot", "channel", album=True)
        self.assertEqual(caption.count("⌚ ساعة"), 1)
        self.assertIn("ذهبية ✨", caption)
        self.assertIn("150 DH", caption)
        self.assertIn("🟨", caption)
        self.assertNotIn("Delivery fee", caption)
        self.assertNotIn("رسوم التوصيل", caption)
        self.assertIn("الطلب كيبدا من 10 قطع", caption)
        self.assertIn("للقطعة", caption)
        self.assertNotIn("120", caption)
        self.assertIn("LUXEVISTA", caption)
        encoded = caption.encode("utf-16-le")
        labels = [encoded[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le") for e in entities]
        self.assertIn("⌚ ساعة", labels)
        self.assertTrue(any(label == "150 DH" and entity["type"] == "text_link"
                            for label, entity in zip(labels, entities)))
        self.assertIn("طلب دابا", caption)
        self.assertNotIn("Order now", caption)
        self.assertNotIn("per item", caption)
        product["description"] = "⌚" * 1100
        with self.assertRaisesRegex(RuntimeError, "طويل بزاف"):
            self.bot.product_post_caption(product, "TestBot", "channel", album=True)

    def test_customers_only_see_their_own_orders_including_older_pages(self):
        for _ in range(12):
            self.insert_order(101, "Customer X")
            self.insert_order(202, "Customer Y")
        for chat_id, own, other in [(101, "Customer X", "Customer Y"), (202, "Customer Y", "Customer X")]:
            for command, count in [("/orders", 10), ("/orders 2", 2)]:
                self.calls.clear()
                self.text(chat_id, command)
                self.assertEqual(self.sent_text().count(own), count)
                self.assertNotIn(other, self.sent_text())
                self.assertTrue(all(data["chat_id"] == chat_id for method, data in self.calls))
        self.calls.clear()
        self.text(303, "/orders")
        self.assertNotIn("Customer", self.sent_text())

    def test_admin_can_still_see_all_orders(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.text(999, "/orders")
        self.assertIn("Customer X", self.sent_text())
        self.assertIn("Customer Y", self.sent_text())

    def test_only_admin_can_delete_orders(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.text(101, "/delete 1")
        self.text(202, "/delete 1")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 2)
        self.text(999, "/delete 1")
        remaining = self.db.execute("SELECT * FROM orders").fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["customer_name"], "Customer Y")
        self.calls.clear()
        self.text(101, "/orders")
        self.assertNotIn("Customer X", self.sent_text())
        self.assertNotIn("Customer Y", self.sent_text())

    def test_invalid_or_missing_delete_id_changes_nothing(self):
        self.insert_order(101, "Customer X")
        for command in ["/delete", "/delete 0", "/delete -1", "/delete 1 2",
                        "/delete 1 OR 1=1", "/delete 9999999999999999999", "/delete 99"]:
            self.text(999, command)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM retired_order_skus").fetchone()[0], 0)

    def test_delete_middle_renumbers_and_next_order_continues_sequence(self):
        for chat_id, name in [(101, "Customer X"), (202, "Remove me"), (303, "Customer Z")]:
            self.insert_order(chat_id, name)
        original = dict(self.db.execute("SELECT * FROM orders WHERE id=3").fetchone())
        self.text(999, "/delete 2")
        remaining = self.db.execute("SELECT id FROM orders ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in remaining], [1, 2])
        renumbered = dict(self.db.execute("SELECT * FROM orders WHERE id=2").fetchone())
        original["id"] = 2
        self.assertEqual(renumbered, original)
        self.calls.clear()
        self.text(303, "/orders")
        self.assertIn("#2", self.sent_text())
        self.assertIn("Customer Z", self.sent_text())
        self.assertNotIn("Customer X", self.sent_text())
        self.db.close()
        self.db = self.bot.connect()
        self.insert_order(404, "Customer W")
        self.assertEqual(self.db.execute("SELECT id FROM orders WHERE chat_id=404").fetchone()[0], 3)

    def test_delete_last_and_only_order_resets_next_number(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.text(999, "/delete 2")
        self.insert_order(303, "Customer Z")
        self.assertEqual(self.db.execute("SELECT id FROM orders WHERE chat_id=303").fetchone()[0], 2)
        self.text(999, "/delete 2")
        self.text(999, "/delete 1")
        self.insert_order(404, "Customer W")
        self.assertEqual(self.db.execute("SELECT id FROM orders").fetchone()[0], 1)

    def test_delete_fills_existing_gaps(self):
        for number in range(1, 6):
            self.insert_order(number, f"Customer {number}")
        self.db.execute("DELETE FROM orders WHERE id IN (1, 3)")
        self.db.commit()
        self.text(999, "/delete 4")
        remaining = [tuple(row) for row in self.db.execute("SELECT id, chat_id FROM orders ORDER BY id")]
        self.assertEqual(remaining, [(1, 2), (2, 5)])
        self.insert_order(6, "Next customer")
        self.assertEqual(self.db.execute("SELECT id FROM orders WHERE chat_id=6").fetchone()[0], 3)

    def test_renumber_failure_rolls_back_entire_deletion(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.db.execute("""CREATE TRIGGER block_renumber BEFORE UPDATE OF id ON orders
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(self.bot.sqlite3.IntegrityError):
            self.text(999, "/delete 1")
        self.assertEqual([row[0] for row in self.db.execute("SELECT id FROM orders ORDER BY id")], [1, 2])
        self.assertEqual(self.db.execute("SELECT count(*) FROM retired_order_skus").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='orders'").fetchone()[0], 2)

    def test_deleted_order_keeps_its_product_sku_reserved_after_restart(self):
        self.insert_order(101, "Customer X")
        self.text(999, "/delete 1")
        self.db.close()
        self.db = self.bot.connect()
        self.assertEqual(self.bot.next_sku({}, self.db), "p0002")
        self.assertEqual(self.bot.load_products(), self.products)

    def test_delete_rolls_back_sku_reservation_if_deletion_fails(self):
        self.insert_order(101, "Customer X")
        self.db.execute("""CREATE TRIGGER block_delete BEFORE DELETE ON orders
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(self.bot.sqlite3.IntegrityError):
            self.text(999, "/delete 1")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM retired_order_skus").fetchone()[0], 0)

    def test_arguments_cannot_select_another_customer(self):
        self.insert_order(202, "Customer Y")
        for command in ["/orders 202", "/orders 1 202", "/orders -1", "/orders 0", "/orders 1 OR 1=1"]:
            self.text(101, command)
        self.assertNotIn("Customer Y", self.sent_text())

    def test_confirm_saves_once_and_deletes_only_form_messages(self):
        self.bot.remember_order_message(self.db, 202, 88)
        confirm_id = self.fill_form()
        tracked = {row[0] for row in self.db.execute(
            "SELECT message_id FROM order_messages WHERE chat_id=101")}
        self.assertTrue(set(range(1, 7)).issubset(tracked))
        self.callback(101, "confirm", confirm_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertIsNone(self.bot.session(self.db, 101))
        deleted = set()
        for method, data in self.calls:
            if method == "deleteMessages":
                self.assertEqual(data["chat_id"], 101)
                deleted.update(json.loads(data["message_ids"]))
        self.assertEqual(deleted, tracked)
        self.assertNotIn(self.next_id, deleted)  # Success message remains.
        self.assertIn("/orders", self.sent_text())
        self.assertEqual(self.db.execute("SELECT count(*) FROM order_messages WHERE chat_id=202").fetchone()[0], 1)
        self.callback(101, "confirm", confirm_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)

    def test_admin_alert_and_imported_photo_survive_admin_test_order(self):
        self.db.execute("INSERT INTO source_imports VALUES (999, 77, 'p0001')")
        self.db.commit()
        confirm_id = self.fill_form(999)
        self.callback(999, "confirm", confirm_id)
        deleted = {mid for method, data in self.calls if method == "deleteMessages"
                   for mid in json.loads(data["message_ids"])}
        self.assertNotIn(77, deleted)
        self.assertNotIn(self.next_id - 1, deleted)  # Admin alert.
        self.assertNotIn(self.next_id, deleted)  # Success message.

    def test_cleanup_failure_does_not_lose_order_or_confirmation(self):
        confirm_id = self.fill_form()
        def failing_api(method, data=None, timeout=15):
            result = self.api(method, data, timeout)
            if method in ("deleteMessages", "deleteMessage"):
                raise RuntimeError("Message cannot be deleted")
            return result
        self.bot.api = failing_api
        self.callback(101, "confirm", confirm_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertIn("/orders", self.sent_text())

    def test_tracking_survives_restart_and_batches_at_100(self):
        for message_id in range(1, 206):
            self.bot.remember_order_message(self.db, 101, message_id)
        self.db.close()
        self.db = self.bot.connect()
        self.bot.clean_order_chat(self.db, 101)
        batches = [json.loads(data["message_ids"]) for method, data in self.calls if method == "deleteMessages"]
        self.assertEqual(list(map(len, batches)), [100, 100, 5])

    def test_cancel_does_not_delete_messages_or_create_order(self):
        self.fill_form()
        self.text(101, "/cancel")
        self.assertFalse(any(method.startswith("delete") for method, _ in self.calls))
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    def test_failed_admin_alert_still_confirms_and_cleans(self):
        confirm_id = self.fill_form()
        def failing_api(method, data=None, timeout=15):
            if method == "sendMessage" and data["chat_id"] == "999":
                raise RuntimeError("Notification unavailable")
            return self.api(method, data, timeout)
        self.bot.api = failing_api
        with contextlib.redirect_stderr(io.StringIO()):
            self.callback(101, "confirm", confirm_id)
        self.assertIn("/orders", self.sent_text())
        self.assertTrue(any(method == "deleteMessages" for method, _ in self.calls))


class NotificationTests(BotTestCase):
    def deliver(self, ticks=10, start=100):
        for now in range(start, start + ticks):
            self.bot.shop.process_notifications(self.db, self.products, "TestBot", self.bot.api, now=now)

    def recipients(self):
        return [data["chat_id"] for method, data in self.calls if method == "sendMessage"]

    def restock_request(self, chat_id=101):
        self.text(999, "/stock p0001 0")
        self.callback(chat_id, "restock:p0001", 99)

    def test_stock_is_manual_after_confirmation_and_order_deletion(self):
        self.text(999, "/stock p0001 10")
        confirm_id = self.fill_form()
        self.callback(101, "confirm", confirm_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(self.bot.shop.stock(self.db, "p0001"), 10)
        self.text(999, "/delete 1")
        self.assertEqual(self.bot.shop.stock(self.db, "p0001"), 10)

    def test_unavailable_product_shows_notify_button_and_blocks_stale_order_button(self):
        self.text(999, "/stock p0001 0")
        self.text(101, "/start p_p0001_channel")
        markup = json.loads(self.calls[-1][1]["reply_markup"])
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "restock:p0001")
        self.callback(101, "order:p0001:channel", 55)
        self.assertIsNone(self.bot.session(self.db, 101))

    def test_stock_is_rechecked_at_quantity_and_confirmation(self):
        self.text(999, "/stock p0001 10")
        self.text(101, "/start p_p0001_channel")
        self.callback(101, "order:p0001:channel", self.next_id)
        self.text(101, "11")
        self.assertEqual(self.bot.session(self.db, 101)[0], "quantity")
        confirm_id = self.fill_form()
        self.text(999, "/stock p0001 0")
        self.callback(101, "confirm", confirm_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(self.bot.session(self.db, 101)[0], "quantity")

    def test_restock_is_opt_in_once_and_survives_restart(self):
        self.restock_request()
        self.callback(101, "restock:p0001", 99)
        self.assertEqual(self.db.execute("SELECT count(*) FROM restock_requests").fetchone()[0], 1)
        self.text(999, "/stock p0001 3")
        self.text(999, "/stock p0001 4")
        self.db.close()
        self.db = self.bot.connect()
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [101])
        markup = json.loads(self.calls[0][1]["reply_markup"])
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://t.me/TestBot?start=p_p0001_restock")
        self.assertIn("Test watch", self.sent_text())
        self.assertEqual(self.db.execute("SELECT count(*) FROM restock_requests").fetchone()[0], 0)
        self.text(999, "/stock p0001 0")
        self.text(999, "/stock p0001 3")
        self.calls.clear()
        self.deliver(start=200)
        self.assertEqual(self.recipients(), [])

    def test_restock_can_be_requested_again_for_next_outage(self):
        self.restock_request()
        self.text(999, "/stock p0001 3")
        self.deliver()
        self.restock_request()
        self.text(999, "/stock p0001 1")
        self.calls.clear()
        self.deliver(start=200)
        self.assertEqual(self.recipients(), [101])

    def test_unsubscribe_cancels_pending_and_is_scoped_to_customer(self):
        self.restock_request(101)
        self.callback(202, "restock:p0001", 99)
        self.text(999, "/stock p0001 3")
        self.callback(101, "unrestock:p0001", 99)
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [202])

    def test_arrivals_match_categories_and_all_without_duplicates_or_late_opt_ins(self):
        self.text(999, "/category p0001 women collections")
        for chat_id, category in [(101, "women"), (101, "all"), (202, "men"), (303, "collections"), (404, "all")]:
            self.callback(chat_id, f"arrival:on:{category}", 99)
        self.text(999, "/announce p0001")
        self.callback(505, "arrival:on:all", 99)
        self.text(999, "/announce p0001")
        self.calls.clear()
        self.deliver()
        self.assertCountEqual(self.recipients(), [101, 303, 404])
        self.assertEqual(len(self.recipients()), 3)

    def test_category_opt_out_cancels_old_jobs_even_if_resubscribed(self):
        self.text(999, "/category p0001 women")
        self.callback(101, "arrival:on:women", 99)
        self.text(999, "/announce p0001")
        self.callback(101, "arrival:off:women", 99)
        self.callback(101, "arrival:on:women", 99)
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [])

    def test_all_off_cancels_restock_and_arrival_jobs(self):
        self.restock_request()
        self.callback(101, "arrival:on:all", 99)
        self.text(999, "/announce p0001")
        self.text(999, "/stock p0001 3")
        self.text(101, "/notifications off")
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM arrival_subscriptions").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM restock_requests").fetchone()[0], 0)

    def test_no_implicit_subscriptions_from_orders_or_notification_menu(self):
        self.text(101, "/notifications")
        self.text(101, "/arrivals")
        self.callback(101, "confirm", self.fill_form())
        self.text(999, "/announce p0001")
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [])

    def test_deleted_catalog_product_cancels_delivery_and_sku_stays_reserved(self):
        self.restock_request()
        self.text(999, "/stock p0001 3")
        self.bot.save_products({})
        self.bot.sync_products(self.products, self.db)
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [])
        self.assertEqual(self.bot.next_sku({}, self.db), "p0002")
        self.assertEqual(self.bot.load_products(), {})

    def test_admin_validation_and_private_chat_restriction(self):
        original = self.bot.PRODUCTS_PATH.read_bytes()
        for command in ["/stock p0001 -1", "/stock p0001 1.5", "/stock p0001 1000000",
                        "/stock missing 1", "/category p0001 bad:topic", "/category p0001 all"]:
            self.text(999, command)
        for command in ["/stock p0001 0", "/category p0001 women", "/announce p0001"]:
            self.text(101, command)
            self.bot.handle_text({"chat": {"id": 999, "type": "group"}, "text": command}, self.products, self.db, "TestBot")
        self.assertIsNone(self.bot.shop.stock(self.db, "p0001"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM product_topics").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM arrival_announcements").fetchone()[0], 0)
        self.assertEqual(self.bot.PRODUCTS_PATH.read_bytes(), original)

    def test_custom_categories_and_removing_categories(self):
        self.text(999, "/category p0001 summer_2026 women")
        self.callback(101, "arrival:on:summer_2026", 99)
        self.text(999, "/announce p0001")
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [101])
        self.text(999, "/category p0001 none")
        self.assertEqual(self.db.execute("SELECT count(*) FROM product_topics").fetchone()[0], 0)

    def test_temporary_failure_retries_after_restart_without_repeating_success(self):
        self.restock_request()
        self.text(999, "/stock p0001 3")
        def fail(method, data, timeout=15):
            raise RuntimeError("Offline")
        self.bot.api = fail
        with contextlib.redirect_stdout(io.StringIO()):
            self.deliver(ticks=1)
        self.assertEqual(self.db.execute("SELECT status FROM notification_jobs").fetchone()[0], "pending")
        self.db.close()
        self.db = self.bot.connect()
        self.bot.api = self.api
        self.calls.clear()
        self.deliver(start=110)
        self.assertEqual(self.recipients(), [])
        self.deliver(start=131)
        self.assertEqual(self.recipients(), [101])

    def test_rate_limit_defers_all_notifications_and_blocked_user_does_not_block_others(self):
        self.restock_request(101)
        self.callback(202, "restock:p0001", 99)
        self.text(999, "/stock p0001 3")
        def limited(method, data, timeout=15):
            raise self.bot.shop.TelegramError("Too many requests", 429, 120)
        self.bot.api = limited
        with contextlib.redirect_stdout(io.StringIO()):
            self.deliver(ticks=1)
        self.bot.api = self.api
        self.calls.clear()
        self.deliver(start=200)
        self.assertEqual(self.recipients(), [])
        def blocked(method, data, timeout=15):
            if data["chat_id"] == 101:
                raise self.bot.shop.TelegramError("Forbidden", 403)
            return self.api(method, data, timeout)
        self.bot.api = blocked
        self.deliver(start=221)
        self.assertEqual(self.recipients(), [202])
        self.assertEqual(self.db.execute("SELECT count(*) FROM restock_requests WHERE chat_id=101").fetchone()[0], 0)

    def test_stock_sold_out_again_defers_alert_until_available(self):
        self.restock_request()
        self.text(999, "/stock p0001 3")
        self.text(999, "/stock p0001 0")
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [])
        self.text(999, "/stock p0001 2")
        self.calls.clear()
        self.deliver(start=161)
        self.assertEqual(self.recipients(), [101])

    def test_consent_is_rechecked_just_before_sending(self):
        self.callback(101, "arrival:on:all", 99)
        self.text(999, "/announce p0001")
        self.db.execute("DELETE FROM arrival_subscriptions")
        self.db.commit()
        self.calls.clear()
        self.deliver()
        self.assertEqual(self.recipients(), [])

    def test_first_single_publish_queues_arrival_but_preview_and_repeat_do_not(self):
        self.bot.CHANNEL_ID = "@testchannel"
        self.callback(101, "arrival:on:all", 99)
        def publishing_api(method, data, timeout=15):
            if method == "copyMessage":
                self.api(method, data, timeout)
                return {"message_id": 9999}
            return self.api(method, data, timeout)
        self.bot.api = publishing_api
        message = {"chat": {"id": 999, "type": "private"}, "message_id": 10,
                   "reply_to_message": {"message_id": 5}, "text": "/preview p0001"}
        self.bot.handle_text(message, self.products, self.db, "TestBot")
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs").fetchone()[0], 0)
        message["text"] = "/publish p0001"
        self.bot.handle_text(message, self.products, self.db, "TestBot")
        self.bot.handle_text(message, self.products, self.db, "TestBot")
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs").fetchone()[0], 1)
        copies = [data for method, data in self.calls if method == "copyMessage"]
        self.assertEqual(copies[-1]["message_id"], 5)
        self.assertIn("LUXEVISTA", copies[-1]["caption"])
        self.assertEqual(copies[0]["caption"], copies[-1]["caption"])
        self.assertTrue(json.loads(copies[-1]["caption_entities"]))
        self.assertEqual(json.loads(copies[-1]["reply_markup"])["inline_keyboard"], [])
        self.assertIn("p_p0001_channel", copies[-1]["caption_entities"])

    def test_album_publish_keeps_album_flow_and_queues_arrival(self):
        self.bot.CHANNEL_ID = "@testchannel"
        self.callback(101, "arrival:on:all", 99)
        for mid in [5, 6]:
            self.db.execute("INSERT INTO album_items VALUES (999, 'album', ?, ?, ?, NULL)",
                            (mid, f"photo-{mid}", "Test watch\n120 DH" if mid == 5 else ""))
        self.db.commit()
        def album_api(method, data, timeout=15):
            if method == "sendMediaGroup":
                self.api(method, data, timeout)
                return [{"message_id": 50}, {"message_id": 51}]
            return self.api(method, data, timeout)
        self.bot.api = album_api
        self.bot.publish_album(999, "album", "p0001", False, self.products, self.db, "TestBot")
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs").fetchone()[0], 0)
        self.bot.publish_album(999, "album", "p0001", True, self.products, self.db, "TestBot")
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs").fetchone()[0], 1)
        media = json.loads(next(data["media"] for method, data in self.calls if method == "sendMediaGroup"))
        self.assertEqual(len(media), 2)
        self.assertEqual([item["media"] for item in media], ["photo-5", "photo-6"])
        self.assertTrue(all(item["type"] == "photo" for item in media))
        self.assertNotIn("caption", media[1])
        self.assertIn("LUXEVISTA", media[0]["caption"])
        self.assertEqual(media[0]["caption_entities"][-1]["type"], "text_link")
        ctas = [data for method, data in self.calls if method == "sendMessage" and data["chat_id"] == "@testchannel"]
        self.assertEqual(ctas, [])
        self.assertIn("طلب دابا", media[0]["caption"])
        self.assertIsNone(self.db.execute("SELECT cta_message_id FROM published_albums").fetchone()[0])
        self.bot.publish_album(999, "album", "p0001", True, self.products, self.db, "TestBot")
        self.assertEqual(sum(method == "sendMediaGroup" for method, data in self.calls), 2)
        self.assertEqual(sum(method == "sendMessage" and data["chat_id"] == "@testchannel" for method, data in self.calls), 0)

    def test_album_needs_no_separate_button_and_retry_does_not_repost(self):
        self.bot.CHANNEL_ID = "@testchannel"
        for mid in [5, 6]:
            self.db.execute("INSERT INTO album_items VALUES (999, 'album', ?, ?, ?, NULL)",
                            (mid, f"photo-{mid}", "Test watch\n120 DH" if mid == 5 else ""))
        self.db.commit()
        fail_button = True
        def album_api(method, data, timeout=15):
            if method == "sendMediaGroup":
                self.api(method, data, timeout)
                return [{"message_id": 50}, {"message_id": 51}]
            if method == "sendMessage" and data["chat_id"] == "@testchannel" and fail_button:
                raise RuntimeError("Temporary failure")
            return self.api(method, data, timeout)
        self.bot.api = album_api
        self.bot.publish_album(999, "album", "p0001", True, self.products, self.db, "TestBot")
        self.assertIsNone(self.db.execute("SELECT cta_message_id FROM published_albums").fetchone()[0])
        fail_button = False
        self.bot.publish_album(999, "album", "p0001", True, self.products, self.db, "TestBot")
        self.assertEqual(sum(method == "sendMediaGroup" for method, data in self.calls), 1)
        self.assertIsNone(self.db.execute("SELECT cta_message_id FROM published_albums").fetchone()[0])

    def test_existing_published_products_are_not_reannounced_after_upgrade(self):
        self.db.execute("INSERT INTO published_posts VALUES (999, 1, 'p0001', '@test', 5)")
        self.db.commit()
        self.db.close()
        self.db = self.bot.connect()
        self.callback(101, "arrival:on:all", 99)
        self.text(999, "/announce p0001")
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs").fetchone()[0], 0)

    def test_failed_publication_does_not_queue_an_arrival(self):
        self.bot.CHANNEL_ID = "@testchannel"
        self.callback(101, "arrival:on:all", 99)
        def fail_publish(method, data, timeout=15):
            if method == "copyMessage":
                raise RuntimeError("No permission")
            return self.api(method, data, timeout)
        self.bot.api = fail_publish
        self.bot.handle_text({"chat": {"id": 999, "type": "private"}, "message_id": 10,
                              "reply_to_message": {"message_id": 5}, "text": "/publish p0001"},
                             self.products, self.db, "TestBot")
        self.assertEqual(self.db.execute("SELECT count(*) FROM arrival_announcements").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs").fetchone()[0], 0)

    def test_notification_rate_limit_is_persistent_and_not_paid(self):
        for chat_id in [101, 202]:
            self.callback(chat_id, "arrival:on:all", 99)
        self.text(999, "/announce p0001")
        self.calls.clear()
        self.deliver(ticks=1)
        self.db.close()
        self.db = self.bot.connect()
        self.deliver(ticks=1)
        self.assertEqual(len(self.recipients()), 1)
        self.deliver(ticks=1, start=101)
        self.assertEqual(len(self.recipients()), 2)
        self.assertTrue(all("allow_paid_broadcast" not in data for method, data in self.calls))

    def test_http_rate_limit_preserves_retry_after(self):
        error = self.bot.urllib.error.HTTPError("https://example.invalid", 429, "rate limit", {},
            io.BytesIO(b'{"description":"Too Many Requests","parameters":{"retry_after":123}}'))
        with patch.object(self.bot.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(self.bot.shop.TelegramError) as caught:
                self.bot.telegram_request(self.bot.urllib.request.Request("https://example.invalid"), 15)
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(caught.exception.retry_after, 123)


if __name__ == "__main__":
    unittest.main()
