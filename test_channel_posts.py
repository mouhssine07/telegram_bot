"""Link-only channel imports, permissions, persistence and edit failure handling."""
import json
from unittest.mock import patch

from test_bot import BotTestCase


class ChannelLinkTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.bot.CHANNEL_ID = '@channel'
        self.original = {'message_id': 600, 'photo': [{'file_id': 'watch-photo'}],
                         'caption': 'Gold watch\nSteel bracelet\n150 DH'}
        self.can_edit = True
        self.fail_method = None
        self.bot.api = self.telegram

    def telegram(self, method, data=None, timeout=15):
        result = self.api(method, data, timeout)
        if method == self.fail_method:
            raise self.bot.shop.TelegramError('Request failed', 400)
        if method == 'getChat':
            return {'id': -1001234, 'username': 'channel', 'type': 'channel'}
        if method == 'getMe':
            return {'id': 777, 'username': 'TestBot'}
        if method == 'getChatMember':
            return {'status': 'administrator', 'can_edit_messages': self.can_edit}
        if method == 'forwardMessage':
            return self.original.copy()
        return result

    def edit(self, url='https://t.me/channel/713', chat_id=999):
        self.text(chat_id, '/edit ' + url)

    def imported(self):
        return self.db.execute('SELECT * FROM channel_products').fetchone()

    def edits(self):
        return [(m, d) for m, d in self.calls if m.startswith('editMessage')]

    def test_manual_photo_import_assigns_sku_and_edits_original_with_button(self):
        self.edit()
        row = self.imported()
        self.assertEqual(row['sku'], 'p0002')
        product = self.bot.load_products()['p0002']
        self.assertEqual((product['name'], product['price_dh']), ('Gold watch', 150))
        self.assertIn('Steel bracelet', product['description'])
        method, data = self.edits()[0]
        self.assertEqual((method, data['chat_id'], data['message_id']), ('editMessageCaption', '-1001234', 713))
        self.assertIn('LUXEVISTA', data['caption'])
        self.assertIn('p_p0002_channel', data['reply_markup'])
        self.assertFalse(any(m in ('copyMessage', 'deleteMessage', 'sendMediaGroup') for m, d in self.calls))
        forwards = [d for m, d in self.calls if m == 'forwardMessage']
        self.assertEqual(forwards[0]['chat_id'], 999)
        self.assertEqual(self.db.execute('SELECT count(*) FROM notification_jobs').fetchone()[0], 0)
        self.text(101, '/start p_p0002_channel')
        self.assertTrue(any(m == 'sendPhoto' and d['photo'] == 'watch-photo' for m, d in self.calls))

    def test_repeat_and_sku_edit_reuse_mapping_current_catalog_after_restart(self):
        self.edit()
        self.db.close()
        self.db = self.bot.connect()
        self.products['p0002']['price_dh'] = 175
        self.bot.save_products(self.products)
        self.edit()
        self.text(999, '/edit p0002')
        self.assertEqual(self.db.execute('SELECT count(*) FROM channel_products').fetchone()[0], 1)
        self.assertEqual(sum(m == 'forwardMessage' for m, d in self.calls), 1)
        self.assertIn('175 DH', self.edits()[-1][1]['caption'])
        self.assertEqual(len(self.bot.load_products()), 2)

    def test_text_and_private_link_use_text_edit(self):
        self.original = {'message_id': 600, 'text': 'Gold watch\n150 DH'}
        self.edit('https://t.me/c/1234/713?single')
        method, data = self.edits()[0]
        self.assertEqual(method, 'editMessageText')
        self.assertIn('LUXEVISTA', data['text'])
        self.assertIn('reply_markup', data)

    def test_video_import_preserves_video(self):
        self.original = {'message_id': 600, 'video': {'file_id': 'watch-video'}, 'caption': 'Watch\n150 DH'}
        self.edit()
        self.assertEqual(self.edits()[0][0], 'editMessageCaption')
        self.text(101, '/start p_p0002_channel')
        self.assertTrue(any(m == 'sendVideo' and d['video'] == 'watch-video' for m, d in self.calls))

    def test_album_creates_one_button_and_reuses_it(self):
        self.original['media_group_id'] = 'album1'
        self.edit()
        first = self.imported()['cta_message_id']
        self.assertIsNotNone(first)
        self.edit()
        self.assertNotIn('reply_markup', self.edits()[0][1])
        self.assertEqual(self.edits()[-1][1]['message_id'], first)
        self.assertEqual(sum(m == 'sendMessage' and d['chat_id'] == '-1001234' for m, d in self.calls), 1)

    def test_existing_bot_post_reuses_sku_and_saved_product_details(self):
        self.db.execute("INSERT INTO published_posts VALUES (999, 1, 'p0001', '@channel', 713)")
        self.db.commit()
        self.edit()
        self.assertEqual(self.imported()['sku'], 'p0001')
        self.assertEqual(len(self.products), 1)
        self.assertIn('120 DH', self.edits()[0][1]['caption'])

    def test_wrong_channel_invalid_url_and_non_admin_do_not_forward(self):
        self.edit(chat_id=101)
        for url in ['https://t.me/wrong/713', 'https://t.me/c/999/713', 'https://example.com/channel/713']:
            self.edit(url)
        self.assertFalse(any(m == 'forwardMessage' for m, d in self.calls))
        self.assertIsNone(self.imported())

    def test_missing_edit_permission_stops_before_import(self):
        self.can_edit = False
        self.edit()
        self.assertIn('Enable Edit Messages', self.sent_text())
        self.assertFalse(any(m == 'forwardMessage' for m, d in self.calls))
        self.assertIsNone(self.imported())

    def test_protected_post_and_ambiguous_price_leave_catalog_unchanged(self):
        self.fail_method = 'forwardMessage'
        self.edit()
        self.assertIsNone(self.imported())
        self.fail_method = None
        for raw in ['Watch\n150 DH\nDelivery 30 DH', 'Watch without price', 'Watch\n150.50 DH', '']:
            self.original['caption'] = raw
            self.edit()
        self.assertEqual(len(self.products), 1)
        self.assertIsNone(self.imported())
        self.assertEqual(self.edits(), [])

    def test_edit_failure_keeps_saved_product_and_retry_does_not_duplicate(self):
        self.fail_method = 'editMessageCaption'
        self.edit()
        self.assertIn('p0002 is saved', self.sent_text())
        self.assertIn('p0002', self.bot.load_products())
        self.fail_method = None
        self.edit()
        self.assertEqual(len(self.products), 2)
        self.assertEqual(sum(m == 'forwardMessage' for m, d in self.calls), 1)

    def test_catalog_write_failure_keeps_recoverable_reservation_before_edit(self):
        with patch.object(self.bot, 'save_products', side_effect=OSError('disk full')):
            self.edit()
        self.assertEqual(self.edits(), [])
        self.assertEqual(self.imported()['catalog_ready'], 0)
        self.assertEqual(self.bot.next_sku(self.products, self.db), 'p0003')
        self.edit()
        self.assertEqual(self.imported()['catalog_ready'], 1)
        self.assertIn('p0002', self.bot.load_products())

    def test_deleted_catalog_product_is_not_silently_recreated(self):
        self.edit()
        del self.products['p0002']
        self.bot.save_products(self.products)
        self.calls.clear()
        self.edit()
        self.assertEqual(self.edits(), [])
        self.assertNotIn('p0002', self.bot.load_products())

    def test_existing_style_is_adopted_without_duplicate_brand_or_wrong_name(self):
        self.original['caption'], _ = self.bot.product_post_caption(
            {'sku': 'old', 'name': 'Gold watch', 'description': 'Steel bracelet', 'price_dh': 150}, 'TestBot', 'channel')
        self.edit()
        self.assertEqual(self.products['p0002']['name'], 'Gold watch')
        self.assertEqual(self.edits()[0][1]['caption'].count('LUXEVISTA'), 1)

    def test_album_button_failure_can_resume_without_duplicate_product(self):
        self.original['media_group_id'] = 'album1'
        channel_failures = [True]
        def fail_button(method, data=None, timeout=15):
            if method == 'sendMessage' and data['chat_id'] == '-1001234' and channel_failures[0]:
                raise self.bot.shop.TelegramError('Temporary failure', 500)
            return self.telegram(method, data, timeout)
        self.bot.api = fail_button
        self.edit()
        self.assertIsNone(self.imported()['cta_message_id'])
        self.assertIn('p0002 is saved', self.sent_text())
        channel_failures[0] = False
        self.edit()
        self.assertIsNotNone(self.imported()['cta_message_id'])
        self.assertEqual(len(self.bot.load_products()), 2)

    def test_sku_update_keeps_other_existing_publications_current(self):
        self.db.execute("INSERT INTO published_posts VALUES (999, 1, 'p0001', '@channel', 713)")
        self.db.execute("INSERT INTO published_posts VALUES (999, 2, 'p0001', '@channel', 800)")
        self.db.commit()
        self.edit()
        self.calls.clear()
        self.text(999, '/edit p0001')
        self.assertIn(800, [d['message_id'] for m, d in self.edits()])
