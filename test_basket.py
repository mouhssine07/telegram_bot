"""Mixed basket regression tests; isolated database/catalog and fake Telegram API."""
import json

from test_bot import BotTestCase


class BasketTests(BotTestCase):
    # Exercise retained legacy basket helpers directly. New customer routing is
    # covered by test_order_notes and no longer exposes these entry points.
    def callback(self, chat_id, data, message_id):
        if data.startswith(("add:", "basket:")):
            self.bot.basket_callback(chat_id, data, self.products, self.db)
        else:
            super().callback(chat_id, data, message_id)

    def setUp(self):
        super().setUp()
        self.products['p0002'] = {'sku': 'p0002', 'name': 'Second watch', 'price_dh': 80}
        self.products['p0003'] = {'sku': 'p0003', 'name': 'Third watch', 'price_dh': 50}
        self.bot.save_products(self.products)

    def add(self, sku, quantity, chat=101):
        self.callback(chat, f'add:{sku}:channel', self.next_id)
        self.text(chat, str(quantity))

    def fill_basket(self):
        self.add('p0001', 4)
        self.add('p0002', 3)
        self.add('p0003', 3)

    def checkout(self):
        self.callback(101, 'basket:checkout', self.next_id)
        for value in ['Customer X', '0612345678', 'Rabat', 'Test street']:
            self.text(101, value)
        return self.next_id

    def test_mixed_order_saved_once_with_all_prices_sources_and_one_admin_alert(self):
        self.fill_basket()
        review = self.checkout()
        self.assertIn('870 DH', self.sent_text())
        self.callback(101, 'confirm', review)
        self.callback(101, 'confirm', review)
        orders = self.db.execute('SELECT * FROM orders').fetchall()
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual((order['quantity'], order['total_dh']), (10, 870))
        items = json.loads(order['items_json'])
        self.assertEqual([x['quantity'] for x in items], [4, 3, 3])
        self.assertEqual([x['source'] for x in items], ['channel'] * 3)
        self.assertEqual(self.bot.basket_rows(self.db, 101), [])
        alerts = [data for method, data in self.calls if method == 'sendMessage'
                  and str(data['chat_id']) == '999']
        self.assertEqual(len(alerts), 1)
        self.assertIn('Second watch', alerts[0]['text'])

    def test_minimum_is_total_and_stock_under_ten_can_be_added(self):
        self.bot.shop.set_stock(self.db, 'p0001', 4)
        self.add('p0001', 4)
        self.callback(101, 'basket:checkout', self.next_id)
        self.assertIsNone(self.bot.session(self.db, 101))
        self.add('p0002', 6)
        self.callback(101, 'basket:checkout', self.next_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], 'name')

    def test_quantity_edit_remove_and_customer_isolation(self):
        self.add('p0001', 4)
        self.add('p0001', 9, chat=202)
        self.callback(101, 'basket:edit:p0001', self.next_id)
        self.text(101, '6')
        self.assertEqual(self.bot.basket_rows(self.db, 101)[0]['quantity'], 6)
        self.callback(101, 'basket:remove:p0001', self.next_id)
        self.assertEqual(self.bot.basket_rows(self.db, 101), [])
        self.assertEqual(self.bot.basket_rows(self.db, 202)[0]['quantity'], 9)

    def test_stock_rechecked_at_submission_and_cart_preserved(self):
        self.fill_basket()
        review = self.checkout()
        self.bot.shop.set_stock(self.db, 'p0002', 2)
        self.callback(101, 'confirm', review)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)
        self.assertEqual(len(self.bot.basket_rows(self.db, 101)), 3)
        self.bot.shop.set_stock(self.db, 'p0002', 3)
        self.callback(101, 'confirm', review)
        self.assertEqual(self.bot.shop.stock(self.db, 'p0002'), 3)

    def test_changed_price_requires_fresh_confirmation_and_saved_price_is_stable(self):
        self.fill_basket()
        old_review = self.checkout()
        self.products['p0002']['price_dh'] = 90
        self.bot.save_products(self.products)
        self.callback(101, 'confirm', old_review)
        review = self.next_id
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)
        self.callback(101, 'confirm', old_review)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)
        self.callback(101, 'confirm', review)
        self.products['p0002']['price_dh'] = 200
        self.bot.save_products(self.products)
        order = self.db.execute('SELECT * FROM orders').fetchone()
        self.assertEqual(self.bot.admin.order_total(order), 900)
        self.assertIn('900 DH', self.bot.order_message(order))

    def test_removed_product_blocks_checkout_and_can_be_removed(self):
        self.fill_basket()
        review = self.checkout()
        del self.products['p0002']
        self.bot.save_products(self.products)
        self.callback(101, 'confirm', review)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)
        self.callback(101, 'basket:remove:p0002', self.next_id)
        self.assertEqual(len(self.bot.basket_rows(self.db, 101)), 2)

    def test_editing_basket_invalidates_old_checkout(self):
        self.fill_basket()
        review = self.checkout()
        self.add('p0002', 5)
        self.callback(101, 'confirm', review)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)

    def test_basket_survives_restart_browsing_and_cancel(self):
        self.fill_basket()
        self.text(101, '/start')
        self.text(101, '/cancel')
        self.db.close()
        self.db = self.bot.connect()
        self.assertEqual(len(self.bot.basket_rows(self.db, 101)), 3)
        self.bot.show_basket(101, self.products, self.db)
        self.assertIn('870 DH', self.calls[-1][1]['text'])

    def test_quantity_validation_and_total_limit(self):
        self.add('p0001', 999)
        self.add('p0002', 2)
        self.assertEqual(len(self.bot.basket_rows(self.db, 101)), 1)
        for invalid in ['-1', '1.5', 'abc', '1001']:
            self.text(101, invalid)
            self.assertEqual(len(self.bot.basket_rows(self.db, 101)), 1)
        self.text(101, '1')
        self.assertEqual(sum(x['quantity'] for x in self.bot.basket_rows(self.db, 101)), 1000)
        self.add('p0001', 0)
        self.assertEqual(len(self.bot.basket_rows(self.db, 101)), 1)

    def test_history_sales_confirmation_and_legacy_orders(self):
        self.insert_order(202, 'Legacy Customer')
        self.fill_basket()
        self.callback(101, 'confirm', self.checkout())
        self.calls.clear()
        self.text(101, '/orders')
        self.assertIn('Second watch', self.sent_text())
        self.assertNotIn('Legacy Customer', self.sent_text())
        self.text(999, '/confirm 2')
        confirmations = [d['text'] for m, d in self.calls if m == 'sendMessage' and d['chat_id'] == 101]
        self.assertIn('Third watch', confirmations[-1])
        self.assertIn('870 DH', confirmations[-1])
        self.text(999, '/sales')
        self.assertIn('confirmed: 1 / 10 / 870 DH', self.sent_text())
        self.assertIn('new: 1 / 1 / 120 DH', self.sent_text())
        self.text(999, '/delete 1')
        self.assertEqual(self.bot.admin.order_total(self.db.execute('SELECT * FROM orders').fetchone()), 870)
        self.text(999, '/delete 1')
        for sku in self.products:
            self.assertIsNotNone(self.db.execute('SELECT 1 FROM retired_order_skus WHERE sku=?', (sku,)).fetchone())

    def test_returning_customer_reuses_details_from_mixed_order(self):
        self.fill_basket()
        self.callback(101, 'confirm', self.checkout())
        self.fill_basket()
        self.callback(101, 'basket:checkout', self.next_id)
        step, data = self.bot.session(self.db, 101)
        self.assertEqual(step, 'details')
        self.assertEqual(data['name'], 'Customer X')
        self.callback(101, 'details:use', self.next_id)
        self.callback(101, 'confirm', self.next_id)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 2)
