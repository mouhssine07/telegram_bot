"""Offline checks for updating existing channel messages without republishing."""
import json

from test_bot import BotTestCase


class PostEditTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.bot.CHANNEL_ID = '@channel'
        self.db.execute("INSERT INTO published_posts VALUES (999, 1, 'p0001', '@channel', 50)")
        self.db.commit()

    def edits(self):
        return [(method, data) for method, data in self.calls if method.startswith('editMessage')]

    def assert_no_republication(self):
        self.assertFalse(any(method in ('copyMessage', 'sendPhoto', 'sendMediaGroup', 'deleteMessage', 'deleteMessages')
                             or (method == 'sendMessage' and data['chat_id'] == '@channel')
                             for method, data in self.calls))

    def test_edit_uses_latest_catalog_and_keeps_original_ids_and_button(self):
        updated = {'p0001': {'sku': 'p0001', 'name': 'New watch', 'price_dh': 190,
                            'description': 'New description'}}
        self.bot.save_products(updated)
        self.text(999, '/edit p0001')
        edits = self.edits()
        self.assertEqual(len(edits), 1)
        method, data = edits[0]
        self.assertEqual((method, data['chat_id'], data['message_id']), ('editMessageCaption', '@channel', 50))
        for value in ('New watch', 'New description', '190 DH'):
            self.assertIn(value, data['caption'])
        button = json.loads(data['reply_markup'])['inline_keyboard'][0][0]
        self.assertEqual(button['url'], 'https://t.me/TestBot?start=p_p0001_channel')
        self.assertEqual(self.db.execute('SELECT channel_message_id FROM published_posts').fetchone()[0], 50)
        self.assertEqual(self.db.execute('SELECT count(*) FROM notification_jobs').fetchone()[0], 0)
        self.assert_no_republication()

    def test_album_updates_first_caption_and_existing_button_only(self):
        self.db.execute('DELETE FROM published_posts')
        self.db.execute("INSERT INTO published_albums VALUES (999, 'a', 'p0001', '@channel', '[70,71]', 72)")
        self.db.commit()
        self.text(999, '/edit p0001')
        edits = self.edits()
        self.assertEqual([(m, d['message_id']) for m, d in edits], [('editMessageCaption', 70), ('editMessageText', 72)])
        self.assertNotIn('reply_markup', edits[0][1])
        self.assertIn('120 DH', edits[1][1]['text'])
        self.assertIn('reply_markup', edits[1][1])
        self.assert_no_republication()

    def test_permissions_private_chat_and_team_revocation(self):
        self.text(101, '/edit p0001')
        self.bot.handle_text({'chat': {'id': 999, 'type': 'group'}, 'text': '/edit p0001'},
                             self.products, self.db, 'TestBot')
        self.assertEqual(self.edits(), [])
        self.text(999, '/team add 101')
        self.text(101, '/edit p0001')
        self.assertEqual(len(self.edits()), 1)
        self.text(999, '/team remove 101')
        self.text(101, '/edit p0001')
        self.assertEqual(len(self.edits()), 1)

    def test_invalid_unknown_unpublished_and_missing_channel(self):
        for command in ['/edit', '/edit p0001 extra', '/edit unknown']:
            self.text(999, command)
        self.bot.CHANNEL_ID = '@other'
        self.text(999, '/edit p0001')
        self.bot.CHANNEL_ID = ''
        self.text(999, '/edit p0001')
        self.assertEqual(self.edits(), [])
        self.assert_no_republication()

    def test_multiple_posts_only_for_sku_in_current_channel(self):
        self.db.execute("INSERT INTO published_posts VALUES (999, 2, 'p0001', '@channel', 51)")
        self.db.execute("INSERT INTO published_posts VALUES (999, 3, 'p0001', '@other', 52)")
        self.db.execute("INSERT INTO published_posts VALUES (999, 4, 'other', '@channel', 53)")
        self.db.commit()
        self.text(999, '/edit p0001')
        self.assertEqual([d['message_id'] for m, d in self.edits()], [50, 51])

    def test_partial_failure_and_retry_treats_unchanged_as_success(self):
        self.db.execute("INSERT INTO published_albums VALUES (999, 'a', 'p0001', '@channel', '[70,71]', 72)")
        self.db.commit()
        fail_button = True
        def edit_api(method, data, timeout=15):
            if method == 'editMessageCaption':
                self.api(method, data, timeout)
                raise self.bot.shop.TelegramError('Bad Request: message is not modified', 400)
            if method == 'editMessageText' and fail_button:
                raise self.bot.shop.TelegramError('Bad Request: message to edit not found', 400)
            return self.api(method, data, timeout)
        self.bot.api = edit_api
        self.text(999, '/edit p0001')
        self.assertIn('2 already current', self.sent_text())
        self.assertIn('1 update(s) failed', self.sent_text())
        fail_button = False
        self.text(999, '/edit p0001')
        self.assertIn('1 channel message(s) updated; 2 already current', self.calls[-1][1]['text'])
        self.assert_no_republication()

    def test_legacy_album_without_button_and_bad_records_do_not_create_posts(self):
        self.db.execute('DELETE FROM published_posts')
        self.db.execute("INSERT INTO published_albums VALUES (999, 'a', 'p0001', '@channel', '[70,71]', NULL)")
        self.db.execute("INSERT INTO published_albums VALUES (999, 'b', 'p0001', '@channel', 'invalid', NULL)")
        self.db.commit()
        self.text(999, '/edit p0001')
        self.assertEqual([d['message_id'] for m, d in self.edits()], [70])
        self.assertIn('no recorded button', self.sent_text())
        self.assertIn('invalid saved message IDs', self.sent_text())
        self.assert_no_republication()

    def test_long_caption_fails_before_any_edit(self):
        self.products['p0001']['description'] = 'x' * 1100
        self.bot.save_products(self.products)
        self.text(999, '/edit p0001')
        self.assertEqual(self.edits(), [])
        self.assertIn('too long', self.sent_text())
