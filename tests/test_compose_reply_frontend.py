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


if __name__ == '__main__':
    unittest.main()
