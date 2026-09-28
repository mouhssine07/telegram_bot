"""Offline admin tests using the same isolated fixtures as the customer tests."""

import json
import sqlite3
from unittest.mock import patch

from test_bot import BotTestCase


class AdminPanelTests(BotTestCase):
    def status_button(self, order_id, status):
        order = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return next(callback for row in self.bot.admin.order_buttons(self.db, order)
                    for label, callback in row if callback.endswith(":" + status))

    def test_customer_cannot_open_panel_or_use_forwarded_admin_callbacks(self):
        self.insert_order(202, "Private customer")
        action = self.status_button(1, "confirmed")
        for command in ["/admin", "/sales", "/products", "/team add 101"]:
            self.text(101, command)
        for command in [action, "adm:home", "adm:p:p0001", "adm:e:p0001:price_dh", "adm:orders:0:all"]:
            self.callback(101, command, 50)
        self.assertNotIn("Private customer", self.sent_text())
        self.assertEqual(self.db.execute("SELECT status FROM orders").fetchone()[0], "new")
        self.assertEqual(self.db.execute("SELECT count(*) FROM admin_sessions").fetchone()[0], 0)

    def test_team_access_only_owner_and_revocation_is_immediate(self):
        self.text(999, "/team add 777")
        self.assertTrue(self.bot.admin.is_admin(self.db, "999", 777))
        self.text(777, "/team add 888")
        self.text(777, "/team remove 999")
        self.assertFalse(self.bot.admin.is_admin(self.db, "999", 888))
        self.assertTrue(self.bot.admin.is_admin(self.db, "999", 999))
        self.insert_order(101, "Customer X")
        action = self.status_button(1, "confirmed")
        self.callback(777, action, 50)
        self.assertEqual(self.db.execute("SELECT status FROM orders").fetchone()[0], "confirmed")
        cancel_action = self.status_button(1, "cancelled")
        self.callback(777, "adm:e:p0001:price_dh", 51)
        self.text(999, "/team remove 777")
        self.callback(777, cancel_action, 52)
        self.text(777, "500")
        self.assertEqual(self.db.execute("SELECT status FROM orders").fetchone()[0], "confirmed")
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 120)

    def test_team_sees_orders_but_cannot_permanently_delete_them(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.text(999, "/team add 777")
        self.calls.clear()
        self.text(777, "/orders")
        self.assertIn("Customer X", self.sent_text())
        self.assertIn("Customer Y", self.sent_text())
        self.text(777, "/delete 1")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 2)
        self.text(999, "/team remove 777")
        self.calls.clear()
        self.text(777, "/orders")
        self.assertNotIn("Customer X", self.sent_text())
        self.assertNotIn("Customer Y", self.sent_text())

    def test_main_admin_cannot_be_removed_and_bad_team_ids_are_rejected(self):
        for command in ["/team remove 999", "/team add -1001", "/team add abc", "/team add 0", "/team add 1 2"]:
            self.text(999, command)
        self.assertTrue(self.bot.admin.is_admin(self.db, "999", 999))
        self.assertEqual(self.db.execute("SELECT count(*) FROM admin_team").fetchone()[0], 0)

    def test_status_buttons_remain_attached_to_order_after_renumbering(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.insert_order(303, "Customer Z")
        action = self.status_button(3, "confirmed")
        deleted_action = self.status_button(2, "cancelled")
        self.text(999, "/delete 2")
        self.callback(999, action, 50)
        row = self.db.execute("SELECT * FROM orders WHERE id=2").fetchone()
        self.assertEqual((row["customer_name"], row["status"]), ("Customer Z", "confirmed"))
        self.callback(999, deleted_action, 51)
        self.assertEqual(self.db.execute("SELECT status FROM orders WHERE id=2").fetchone()[0], "confirmed")

    def test_reused_number_cannot_be_changed_by_old_deleted_order_button(self):
        self.insert_order(101, "Old order")
        action = self.status_button(1, "cancelled")
        self.text(999, "/delete 1")
        self.insert_order(202, "New order")
        self.callback(999, action, 50)
        self.assertEqual(self.db.execute("SELECT status FROM orders WHERE id=1").fetchone()[0], "new")

    def test_status_history_idempotency_stale_buttons_and_manual_stock(self):
        self.insert_order(101, "Customer X")
        self.text(999, "/stock p0001 5")
        old_cancel = self.status_button(1, "cancelled")
        confirmed = self.status_button(1, "confirmed")
        self.callback(999, confirmed, 50)
        self.callback(999, confirmed, 50)
        self.callback(999, old_cancel, 50)
        self.assertEqual(self.db.execute("SELECT status FROM orders").fetchone()[0], "confirmed")
        for status in ["shipped", "delivered", "cancelled", "new"]:
            self.callback(999, self.status_button(1, status), 51)
        self.assertEqual(self.db.execute("SELECT count(*) FROM order_status_history").fetchone()[0], 5)
        self.assertEqual(self.bot.shop.stock(self.db, "p0001"), 5)

    def test_order_alert_has_admin_buttons_customer_history_does_not(self):
        self.callback(101, "confirm", self.fill_form())
        alert = next(data for method, data in self.calls if method == "sendMessage" and data["chat_id"] == "999")
        markup = json.loads(alert["reply_markup"])
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertTrue(any(button["callback_data"].endswith(":confirmed") for button in buttons))
        self.assertTrue(all(len(button["callback_data"].encode()) <= 64 for button in buttons))
        self.calls.clear()
        self.text(101, "/orders")
        self.assertTrue(all("reply_markup" not in data for method, data in self.calls))

    def test_status_updates_show_in_customer_history_without_cross_customer_leak(self):
        self.insert_order(101, "Customer X")
        self.insert_order(202, "Customer Y")
        self.callback(999, self.status_button(1, "cancelled"), 50)
        self.calls.clear()
        self.text(101, "/orders")
        self.assertIn("cancelled", self.sent_text())
        self.assertNotIn("Customer Y", self.sent_text())

    def test_product_edit_preserves_extra_fields_other_products_and_order_snapshots(self):
        self.insert_order(101, "Customer X")
        self.products["p0001"]["custom"] = "keep"
        self.products["p0002"] = {"sku": "p0002", "name": "Other", "price_dh": 70}
        self.bot.save_products(self.products)
        self.callback(999, "adm:e:p0001:price_dh", 50)
        self.text(999, "155")
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 155)
        self.assertEqual(self.bot.load_products()["p0001"]["custom"], "keep")
        self.assertEqual(self.bot.load_products()["p0002"]["price_dh"], 70)
        self.assertEqual(self.db.execute("SELECT unit_price_dh FROM orders").fetchone()[0], 120)

    def test_product_removed_or_changed_during_edit_is_not_overwritten(self):
        self.callback(999, "adm:e:p0001:name", 50)
        self.bot.save_products({})
        self.text(999, "Must not revive")
        self.assertEqual(self.bot.load_products(), {})
        self.products["p0001"] = {"sku": "p0001", "name": "New name", "price_dh": 80}
        self.bot.save_products(self.products)
        self.callback(999, "adm:e:p0001:price_dh", 50)
        self.products["p0001"]["price_dh"] = 90
        self.bot.save_products(self.products)
        self.text(999, "100")
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 90)

    def test_invalid_price_and_cancel_leave_product_unchanged(self):
        self.callback(999, "adm:e:p0001:price_dh", 50)
        for raw in ["-1", "3.5", "1000000", "free"]:
            self.text(999, raw)
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 120)
        self.text(999, "/cancel")
        self.text(999, "500")
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 120)

    def test_file_write_failure_preserves_catalog_and_pending_editor(self):
        self.callback(999, "adm:e:p0001:price_dh", 50)
        with patch.object(self.bot, "save_products", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.text(999, "155")
        self.assertEqual(self.products["p0001"]["price_dh"], 120)
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 120)
        self.assertEqual(self.db.execute("SELECT count(*) FROM admin_sessions").fetchone()[0], 1)

    def test_panel_stock_edit_queues_opted_in_restock_alert(self):
        self.text(999, "/stock p0001 0")
        self.callback(101, "restock:p0001", 50)
        self.callback(999, "adm:e:p0001:stock", 51)
        self.text(999, "3")
        self.assertEqual(self.bot.shop.stock(self.db, "p0001"), 3)
        self.assertEqual(self.db.execute("SELECT count(*) FROM notification_jobs WHERE kind='restock'").fetchone()[0], 1)

    def test_team_can_import_edit_stock_and_categories(self):
        self.text(999, "/team add 777")
        self.text(777, "/addproduct")
        self.db.execute("INSERT INTO issued_links VALUES ('p0002')")
        self.db.commit()
        self.bot.handle_text({"chat": {"id": 777, "type": "private"}, "message_id": 77,
                              "photo": [{"file_id": "photo"}], "caption": "New watch\n100 DH"},
                             self.products, self.db, "TestBot")
        self.assertIn("p0003", self.bot.load_products())
        self.callback(777, "adm:e:p0003:stock", 50)
        self.text(777, "4")
        self.callback(777, "adm:e:p0003:categories", 51)
        self.text(777, "women collections")
        self.assertEqual(self.bot.shop.stock(self.db, "p0003"), 4)
        self.assertEqual({row[0] for row in self.db.execute("SELECT topic FROM product_topics WHERE sku='p0003'")}, {"women", "collections"})

    def test_product_price_change_requires_latest_customer_review(self):
        old_confirm = self.fill_form()
        self.callback(999, "adm:e:p0001:price_dh", 50)
        self.text(999, "200")
        self.callback(101, "confirm", old_confirm)
        new_confirm = self.next_id
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.callback(101, "confirm", old_confirm)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.callback(101, "confirm", new_confirm)
        self.assertEqual(self.db.execute("SELECT unit_price_dh FROM orders").fetchone()[0], 200)

    def test_sales_uses_saved_prices_and_excludes_cancelled_from_delivered_value(self):
        for i in range(4):
            self.insert_order(101 + i, f"Customer {i}")
        self.db.execute("UPDATE orders SET status='delivered', quantity=2 WHERE id=1")
        self.db.execute("UPDATE orders SET status='cancelled' WHERE id=2")
        self.db.execute("UPDATE orders SET status='confirmed' WHERE id=3")
        self.db.commit()
        self.products["p0001"]["price_dh"] = 999
        self.bot.save_products(self.products)
        self.text(999, "/sales")
        self.assertIn("Delivered order value: 240 DH", self.sent_text())
        self.assertIn("cancelled: 1 / 1 / 120 DH", self.sent_text())
        self.assertIn("confirmed: 1 / 1 / 120 DH", self.sent_text())
        self.calls.clear()
        self.db.execute("UPDATE orders SET created_at=datetime('now','-40 days') WHERE id=1")
        self.db.commit()
        self.text(999, "/sales today")
        self.assertIn("Delivered order value: 0 DH", self.sent_text())
        self.assertIn("UTC", self.sent_text())

    def test_panel_pagination_and_status_filter(self):
        for i in range(7):
            self.insert_order(101 + i, f"Customer {i}")
        self.callback(999, "adm:orders:0:all", 50)
        self.assertEqual(self.sent_text().count("Customer"), 5)
        self.calls.clear()
        self.callback(999, "adm:orders:1:all", 51)
        self.assertEqual(self.sent_text().count("Customer"), 2)
        self.calls.clear()
        self.callback(999, "adm:orders:0:cancelled", 52)
        self.assertNotIn("Customer", self.sent_text())

    def test_migration_and_restart_preserve_order_details_keys_and_team(self):
        self.insert_order(101, "Customer X")
        self.bot.admin.init_schema(self.db)
        original = dict(self.db.execute("SELECT * FROM orders").fetchone())
        self.text(999, "/team add 777")
        self.db.close()
        self.db = self.bot.connect()
        self.assertEqual(dict(self.db.execute("SELECT * FROM orders").fetchone()), original)
        self.assertTrue(self.bot.admin.is_admin(self.db, "999", 777))

    def test_private_only_admin_panel(self):
        self.insert_order(101, "Customer X")
        action = self.status_button(1, "confirmed")
        self.bot.handle_text({"chat": {"id": 999, "type": "group"}, "text": "/admin"}, self.products, self.db, "TestBot")
        self.bot.handle_callback({"id": "cb", "data": action, "message": {"message_id": 5,
                                  "chat": {"id": 999, "type": "group"}}}, self.products, self.db)
        self.assertNotIn("Customer X", self.sent_text())
        self.assertEqual(self.db.execute("SELECT status FROM orders").fetchone()[0], "new")

    def test_upgrade_from_legacy_orders_table_preserves_rows(self):
        with sqlite3.connect(":memory:") as legacy:
            legacy.row_factory = sqlite3.Row
            legacy.execute("""CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT, unit_price_dh INTEGER, status TEXT DEFAULT 'new')""")
            legacy.execute("INSERT INTO orders VALUES (7, 'Existing customer', 110, 'new')")
            self.bot.admin.init_schema(legacy)
            row = legacy.execute("SELECT * FROM orders").fetchone()
            self.assertEqual((row["id"], row["customer_name"], row["unit_price_dh"], row["status"]),
                             (7, "Existing customer", 110, "new"))
            self.assertEqual(len(row["admin_key"]), 32)
            self.assertEqual(row["status_version"], 0)
        legacy.close()

    def test_leaving_editor_via_panel_navigation_discards_pending_edit(self):
        self.callback(999, "adm:e:p0001:price_dh", 50)
        self.callback(999, "adm:products:0", 51)
        self.text(999, "500")
        self.assertEqual(self.bot.load_products()["p0001"]["price_dh"], 120)
