"""Direct checkout, saved text/audio notes, and admin playback permissions."""
import contextlib
import io
import json
import sqlite3

from test_bot import BotTestCase


class OrderNoteTests(BotTestCase):
    def test_existing_database_gains_empty_notes_without_changing_order(self):
        with contextlib.closing(sqlite3.connect(':memory:')) as legacy:
            legacy.row_factory = sqlite3.Row
            legacy.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_name TEXT)')
            legacy.execute("INSERT INTO orders VALUES (7, 'Existing customer')")
            self.bot.admin.init_schema(legacy)
            self.bot.admin.init_schema(legacy)
            order = legacy.execute('SELECT * FROM orders').fetchone()
            self.assertEqual((order['id'], order['customer_name']), (7, 'Existing customer'))
            self.assertEqual(order['description'], '')
            self.assertIsNone(order['note_audio_json'])

    def begin_note(self):
        self.text(101, '/start p_p0001_channel')
        markup = self.calls[-1][1]['reply_markup']
        self.assertIn('order:p0001:channel', markup)
        self.assertNotIn('basket', markup)
        self.callback(101, 'order:p0001:channel', self.next_id)
        for value in ['10', 'Customer X', '0612345678', 'Rabat', 'Test street']:
            self.text(101, value)
        self.assertEqual(self.bot.session(self.db, 101)[0], 'description')

    def audio(self, kind='voice', file_id='saved-audio'):
        self.bot.handle_text({'chat': {'id': 101, 'type': 'private'},
                              'message_id': 40, kind: {'file_id': file_id}},
                             self.products, self.db, 'TestBot')

    def submit(self):
        self.callback(101, 'note:done', self.bot.session(self.db, 101)[1]['note_message_id'])
        self.assertEqual(self.bot.session(self.db, 101)[0], 'confirm')
        self.callback(101, 'confirm', self.next_id)
        return self.db.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 1').fetchone()

    def test_text_and_voice_survive_restart_and_reach_admin_before_cleanup(self):
        self.begin_note()
        self.text(101, 'Green and red, please')
        self.audio()
        self.db.close()
        self.db = self.bot.connect()
        order = self.submit()
        self.assertEqual(order['description'], 'Green and red, please')
        self.assertEqual(json.loads(order['note_audio_json'])['file_id'], 'saved-audio')
        alert = next(d for m, d in self.calls if m == 'sendMessage' and d['chat_id'] == '999')
        self.assertIn('Green and red, please', alert['text'])
        self.assertIn('audio:' + order['admin_key'], alert['reply_markup'])
        voice_index = next(i for i, (m, d) in enumerate(self.calls) if m == 'sendVoice')
        self.assertEqual(self.calls[voice_index][1]['voice'], 'saved-audio')
        self.assertEqual(self.calls[voice_index][1]['chat_id'], '999')
        self.assertLess(voice_index, next(i for i, (m, d) in enumerate(self.calls) if m == 'deleteMessages'))
        self.text(101, '/orders')
        self.assertIn('Green and red, please', self.calls[-1][1]['text'])

    def test_audio_file_only_and_authorized_playback_after_renumber(self):
        self.insert_order(202, 'Older customer')
        self.begin_note()
        self.audio('audio')
        order = self.submit()
        self.assertEqual(order['description'], '')
        self.text(999, '/delete 1')
        self.calls.clear()
        self.callback(202, 'audio:' + order['admin_key'], 1)
        self.assertFalse(any(m == 'sendAudio' for m, d in self.calls))
        self.callback(999, 'audio:' + order['admin_key'], 1)
        playback = next(d for m, d in self.calls if m == 'sendAudio')
        self.assertIn('Order #1', playback['caption'])
        self.text(999, '/delete 1')
        self.calls.clear()
        self.callback(999, 'audio:' + order['admin_key'], 1)
        self.assertFalse(any(m == 'sendAudio' for m, d in self.calls))

    def test_optional_skip_and_stale_note_buttons(self):
        self.begin_note()
        old_id = self.next_id
        self.text(101, 'x' * 1001)
        self.assertNotIn('description', self.bot.session(self.db, 101)[1])
        self.audio()
        self.callback(101, 'note:done', old_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], 'description')
        self.text(101, '/cancel')
        self.begin_note()
        order = self.submit()
        self.assertEqual(order['description'], '')
        self.assertIsNone(order['note_audio_json'])

    def test_clear_removes_both_text_and_audio(self):
        self.begin_note()
        self.text(101, 'Green')
        self.audio()
        self.callback(101, 'note:clear', self.next_id)
        order = self.submit()
        self.assertEqual(order['description'], '')
        self.assertIsNone(order['note_audio_json'])
        self.assertFalse(any(m == 'sendVoice' for m, d in self.calls))

    def test_edit_note_invalidates_confirmation_and_does_not_reuse_last_order_note(self):
        self.begin_note()
        self.text(101, 'Green')
        self.callback(101, 'note:done', self.next_id)
        old_review = self.next_id
        self.callback(101, 'note:edit', old_review)
        self.text(101, 'Red')
        self.callback(101, 'confirm', old_review)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)
        order = self.submit()
        self.assertEqual(order['description'], 'Red')
        self.callback(101, 'order:p0001:channel', self.next_id)
        self.text(101, '10')
        self.callback(101, 'details:use', self.next_id)
        self.assertEqual(self.bot.session(self.db, 101)[0], 'description')
        self.assertNotIn('description', self.bot.session(self.db, 101)[1])

    def test_failed_audio_delivery_keeps_saved_order_and_replay(self):
        self.begin_note()
        self.audio()
        def failing_api(method, data=None, timeout=15):
            if method == 'sendVoice':
                raise RuntimeError('Temporary audio failure')
            return self.api(method, data, timeout)
        self.bot.api = failing_api
        with contextlib.redirect_stderr(io.StringIO()):
            order = self.submit()
        self.assertIsNotNone(order['note_audio_json'])
        self.assertIsNone(self.bot.session(self.db, 101))
        self.bot.api = self.api
        self.callback(999, 'audio:' + order['admin_key'], 1)
        self.assertTrue(any(m == 'sendVoice' for m, d in self.calls))

    def test_old_add_button_enters_direct_checkout_and_basket_returns_catalog(self):
        self.callback(101, 'add:p0001:channel', 1)
        self.assertEqual(self.bot.session(self.db, 101)[0], 'quantity')
        self.callback(101, 'basket:checkout', 1)
        self.assertIsNone(self.bot.session(self.db, 101))
        self.assertNotIn('basket:', self.calls[-1][1]['reply_markup'])
        self.text(101, '/basket')
        self.assertNotIn('basket:', self.calls[-1][1]['reply_markup'])
