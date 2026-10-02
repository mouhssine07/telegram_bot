"""Offline checks for contextual WhatsApp drafts and public channel privacy."""
import json
from urllib.parse import urlparse, parse_qs

from test_bot import BotTestCase


class WhatsAppTests(BotTestCase):
    def draft(self, payload):
        buttons = [b for row in json.loads(payload['reply_markup'])['inline_keyboard'] for b in row]
        button = next(b for b in buttons if b.get('url', '').startswith(self.bot.WHATSAPP_URL))
        self.assertNotIn('callback_data', button)
        parsed = urlparse(button['url'])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ('https', 'wa.me', '/212781209365'))
        return parse_qs(parsed.query)['text'][0]

    def test_product_draft_has_product_and_price_without_customer_data(self):
        self.products['p0001']['name'] = 'ساعة ذهبية & سوداء + وردية'
        self.text(101, '/start p_p0001_channel')
        draft = self.draft(self.calls[-1][1])
        self.assertIn(self.products['p0001']['name'], draft)
        self.assertIn('p0001', draft)
        self.assertIn('120 درهم', draft)
        self.assertNotIn('رقم الطلب:', draft)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)

    def test_note_review_and_saved_order_have_correct_private_details(self):
        self.text(101, '/start p_p0001_channel')
        self.callback(101, 'order:p0001:channel', self.next_id)
        for value in ['10', 'أمين', '0612345678', 'كازا', 'زنقة 4 & باب + 2']:
            self.text(101, value)
        draft = self.draft(self.calls[-1][1])
        for expected in ['Test watch', 'p0001', '10', '1200 درهم', 'أمين', '0612345678', 'كازا', 'زنقة 4 & باب + 2']:
            self.assertIn(expected, draft)
        self.text(101, '5 كحلين\n5 ذهبيين')
        self.bot.handle_text({'chat': {'id': 101, 'type': 'private'}, 'message_id': 50,
                              'voice': {'file_id': 'private-voice-id'}}, self.products, self.db, 'TestBot')
        self.callback(101, 'note:done', self.next_id)
        review = self.draft(self.calls[-1][1])
        self.assertIn('5 كحلين\n5 ذهبيين', review)
        self.assertIn('نعاود نصيفطو', review)
        self.assertNotIn('private-voice-id', review)
        self.assertNotIn('رقم الطلب:', review)
        self.callback(101, 'confirm', self.next_id)
        confirmation = next(d for m, d in reversed(self.calls) if m == 'sendMessage' and d['chat_id'] == 101)
        saved = self.draft(confirmation)
        self.assertIn('رقم الطلب: #1', saved)
        self.assertIn('1200 درهم', saved)
        self.assertIn('5 كحلين', saved)
        self.text(202, '/start p_p0001_channel')
        other = self.draft(self.calls[-1][1])
        self.assertNotIn('أمين', other)
        self.assertNotIn('0612345678', other)

    def test_public_caption_has_product_only_and_valid_link_offsets(self):
        caption, entities = self.bot.product_post_caption(self.products['p0001'], 'TestBot', 'channel')
        contact = next(e for e in entities if e.get('url', '').startswith(self.bot.WHATSAPP_URL))
        encoded = caption.encode('utf-16-le')
        label = encoded[contact['offset'] * 2:(contact['offset'] + contact['length']) * 2].decode('utf-16-le')
        self.assertEqual(label, self.bot.WHATSAPP_LABEL)
        draft = parse_qs(urlparse(contact['url']).query)['text'][0]
        self.assertIn('p0001', draft)
        self.assertNotIn('العنوان', draft)

    def test_basket_draft_lists_each_product_and_total(self):
        button = self.bot.whatsapp_button(data={'items': [
            {'sku': 'p0001', 'name': 'ساعة', 'quantity': 5, 'price_dh': 120},
            {'sku': 'p0002', 'name': 'سوار', 'quantity': 5, 'price_dh': 30}]})
        text = parse_qs(urlparse(button['url']).query)['text'][0]
        self.assertIn('p0001', text)
        self.assertIn('p0002', text)
        self.assertIn('750 درهم', text)
