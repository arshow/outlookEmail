from pathlib import Path
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_JS_PATH = ROOT_DIR / 'static' / 'js' / 'index' / '13-compose.js'


class ComposeReplyFrontendTests(unittest.TestCase):
    def test_reply_defaults_to_cc_sending_account(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')

        self.assertIn('function applyReplyDefaultCc', source)
        self.assertIn('function onComposeFromEmailChange', source)
        self.assertIn('applyReplyDefaultCc(accountEmail)', source)
        self.assertIn("if (mode !== 'reply' && mode !== 'reply_all') return", source)
        self.assertIn("select.addEventListener('change', onComposeFromEmailChange)", source)

    def test_reply_uses_shopify_contact_form_customer_email(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')

        self.assertIn('function resolveComposeReplyTo', source)
        self.assertIn('function extractShopifyContactFormEmail', source)
        self.assertIn('const replyTo = resolveComposeReplyTo(detail);', source)
        self.assertIn('let toList = replyTo ? [replyTo] : [];', source)
        self.assertIn('address !== replyTo', source)
        self.assertIn('new customer message on', source)

    def test_reply_sidebar_loads_related_history_and_preview(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')
        html = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')

        self.assertIn('function loadComposeContactHistory', source)
        self.assertIn('function openComposeHistoryPreview', source)
        self.assertIn('/api/emails/contact-history', source)
        self.assertIn('setComposeSidebarTab(\'history\')', source)
        self.assertIn('id="composeHistoryList"', html)
        self.assertIn('id="composeHistoryPreview"', html)
        self.assertIn('compose-history-preview-overlay', html)
        self.assertIn('compose-history-excerpt', source)
        self.assertNotIn('class="compose-history-preview"', source)
        self.assertNotIn('class="compose-history-preview"', html)
        self.assertIn('相关往来', html)

    def test_email_detail_has_contact_history_entries(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')
        emails_js = (ROOT_DIR / 'static' / 'js' / 'index' / '05-emails.js').read_text(encoding='utf-8')
        html = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')
        layout = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'layout.html').read_text(encoding='utf-8')

        self.assertIn('function showEmailContactHistoryModal()', source)
        self.assertIn("source: 'detail'", source)
        self.assertIn('id="emailContactHistoryModal"', html)
        self.assertIn('id="emailContactHistoryList"', html)
        self.assertIn('email-detail-action-btn--history', layout)
        self.assertIn('onclick="showEmailContactHistoryModal()"', layout)
        self.assertIn('onclick="showEmailContactHistoryModal()"', emails_js)
        self.assertIn('往来邮件', layout)
        self.assertIn('往来邮件', emails_js)

    def test_history_preview_has_ai_translate(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')
        html = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')

        self.assertIn('function toggleComposeHistoryAiTranslation', source)
        self.assertIn("fetchWithTimeout('/api/ai/translate'", source)
        self.assertIn('id="composeHistoryTranslatePanel"', source)
        self.assertIn('id="composeHistoryAiTranslateBtn"', html)
        self.assertIn('onclick="toggleComposeHistoryAiTranslation()"', html)

    def test_reply_window_has_email_note_field(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')
        html = (ROOT_DIR / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')

        self.assertIn('function syncComposeEmailNoteField', source)
        self.assertIn('function saveComposeEmailNote', source)
        self.assertIn('syncComposeEmailNoteField(mode, detail);', source)
        self.assertIn('id="composeEmailNote"', html)
        self.assertIn('id="composeNoteGroup"', html)
        self.assertIn('保存备注', html)

    def test_quoted_html_is_sanitized_and_cleared_on_close(self):
        source = COMPOSE_JS_PATH.read_text(encoding='utf-8')

        self.assertIn('function sanitizeComposeQuotedHtml', source)
        self.assertIn("FORBID_TAGS: [", source)
        self.assertIn("sanitizeComposeQuotedHtml(typeof rawBody === 'string' ? rawBody : '')", source)
        self.assertIn('resetComposeForm();', source)
        self.assertIn('引用里的 <style> 会漏到整页', source)


if __name__ == '__main__':
    unittest.main()
