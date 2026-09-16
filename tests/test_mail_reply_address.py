import unittest

from outlook_web.ai.context import normalize_email_detail, resolve_contact_email
from outlook_web.mail_reply_address import (
    attach_preferred_reply_address,
    extract_shopify_contact_form_email,
    looks_like_shopify_contact_form,
    resolve_preferred_reply_address,
)

SHOPIFY_CONTACT_FORM_HTML = '''
<table class="mail-body__content">
  <div class="primary-message">You received a new message from your online store's contact form.</div>
  <div class="form-section"><b>Country Code:</b> <pre>IT</pre></div>
  <div class="form-section"><b>Name:</b> <pre>Luca Giust</pre></div>
  <div class="form-section"><b>Email:</b> <pre>luca.giust@libero.it</pre></div>
  <div class="form-section"><b>Phone:</b> <pre></pre></div>
  <div class="form-section"><b>Body:</b> <pre>Buongiorno, vorrei sapere le caratteristiche tecniche, (scheda tecnica) della macchina, grazie.</pre></div>
  <div class="form-section"><b>Save Info:</b> <pre>on</pre></div>
</table>
'''

SHOPIFY_CONTACT_FORM_INLINE_HTML = (
    '<div style="margin-top:8px" class="form-section"><b>Email:</b> '
    '<pre style="font-size:14px; font-weight:400; line-height:20px; color:#202223; '
    'margin-top:0; margin-bottom:0; white-space:pre-line; padding:0">'
    'luca.giust@libero.it</pre></div>'
)


class MailReplyAddressTests(unittest.TestCase):
    def test_extracts_customer_email_from_shopify_contact_form_html(self):
        self.assertEqual(
            extract_shopify_contact_form_email(SHOPIFY_CONTACT_FORM_HTML),
            'luca.giust@libero.it',
        )
        self.assertEqual(
            extract_shopify_contact_form_email(SHOPIFY_CONTACT_FORM_INLINE_HTML),
            'luca.giust@libero.it',
        )

    def test_uses_reply_to_header_when_form_email_is_missing(self):
        preferred = resolve_preferred_reply_address(
            sender='mailer@shopify.com',
            subject='New customer message on September 15, 2026 at 10:45 am',
            body="You received a new message from your online store's contact form.",
            header_reply_to='Luca Giust <luca.giust@libero.it>',
        )
        self.assertEqual(preferred, 'luca.giust@libero.it')

    def test_resolves_shopify_contact_form_reply_address(self):
        preferred = resolve_preferred_reply_address(
            sender='Shopify <mailer@shopify.com>',
            subject='New customer message on September 15, 2026 at 10:45 am',
            body=SHOPIFY_CONTACT_FORM_HTML,
        )
        self.assertEqual(preferred, 'luca.giust@libero.it')
        self.assertTrue(looks_like_shopify_contact_form(
            'mailer@shopify.com',
            'New customer message on September 15, 2026 at 10:45 am',
            SHOPIFY_CONTACT_FORM_HTML,
        ))

    def test_does_not_rewrite_ordinary_customer_mail(self):
        preferred = resolve_preferred_reply_address(
            sender='customer@example.com',
            subject='Need a quote',
            body='Please email me at other@example.com',
        )
        self.assertEqual(preferred, '')

    def test_does_not_rewrite_shopify_order_mail_without_contact_form(self):
        preferred = resolve_preferred_reply_address(
            sender='Store <store+1@shopifyemail.com>',
            subject='Order #1234 confirmed',
            body='Customer email: luca.giust@libero.it',
        )
        self.assertEqual(preferred, '')

    def test_attach_preferred_reply_address_sets_reply_to(self):
        detail = attach_preferred_reply_address({
            'from': 'mailer@shopify.com',
            'subject': 'New customer message on September 15, 2026 at 10:45 am',
            'body': SHOPIFY_CONTACT_FORM_HTML,
        })
        self.assertEqual(detail['reply_to'], 'luca.giust@libero.it')

    def test_ai_contact_email_uses_shopify_form_customer(self):
        current = normalize_email_detail({
            'from': 'mailer@shopify.com',
            'subject': 'New customer message on September 15, 2026 at 10:45 am',
            'body': SHOPIFY_CONTACT_FORM_HTML,
            'body_type': 'html',
        })
        self.assertEqual(current['reply_to'], 'luca.giust@libero.it')
        self.assertEqual(
            resolve_contact_email(current, 'store@example.com'),
            'luca.giust@libero.it',
        )


if __name__ == '__main__':
    unittest.main()
