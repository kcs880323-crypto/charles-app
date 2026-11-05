import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kidsnote_kakao.cli import AuthCodeError, extract_code, main
from kidsnote_kakao.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("WEBHOOK_TOKEN", "KAKAO_REST_API_KEY", "TELEGRAM_BOT_TOKEN", "KIDSNOTE_KAKAO_CONFIG"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, text):
        p = self.dir / "config.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_example_config_loads(self):
        cfg = load_config(EXAMPLE)
        self.assertEqual(cfg.server.host, "127.0.0.1")
        self.assertTrue(cfg.rules.children)

    def test_env_and_dotenv_override(self):
        p = self.write('[kakao]\nrest_api_key = "from_toml"\n')
        (self.dir / ".env").write_text('KAKAO_REST_API_KEY="from_env"\nWEBHOOK_TOKEN=abc\n', encoding="utf-8")
        cfg = load_config(p)
        self.assertEqual(cfg.kakao.rest_api_key, "from_env")
        self.assertEqual(cfg.server.webhook_token, "abc")
        self.assertEqual(cfg.path(cfg.kakao.token_file), self.dir.resolve() / "data/kakao_token.json")

    def test_unknown_keys_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[kakao]\nrest_api = 'x'\n"))
        with self.assertRaises(ConfigError):
            load_config(self.write("[kakaoo]\n"))
        with self.assertRaises(ConfigError):
            load_config(self.write('[[rules.categories]]\nname="x"\npriority="high"\n'))
        with self.assertRaises(ConfigError):
            load_config(self.dir / "missing.toml")

    def test_web_min_interval(self):
        cfg = load_config(self.write('[web]\ninterval_seconds = 10\nchildren = ["1", {id = 2, name = "지우"}]\n'))
        self.assertEqual(cfg.web.interval_seconds, 300)
        self.assertEqual([(c.id, c.name) for c in cfg.web.children], [("1", ""), ("2", "지우")])


class CliTest(unittest.TestCase):
    def test_extract_code(self):
        self.assertEqual(extract_code("http://localhost:8765/kakao/callback?code=ABC&state=s1", "s1"), "ABC")
        self.assertEqual(extract_code("RAWCODE", "s1"), "RAWCODE")
        with self.assertRaises(AuthCodeError):
            extract_code("http://localhost/cb?code=ABC&state=other", "s1")
        with self.assertRaises(AuthCodeError):
            extract_code("http://localhost/cb?error=access_denied&error_description=user%20cancelled", "s1")

    def test_check_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["-c", str(EXAMPLE), "check", "--title", "해바라기반", "지우", "투약", "완료했습니다"])
        self.assertEqual(rc, 0)
        self.assertIn("투약 / urgent / 지우", out.getvalue())
        self.assertIn("카카오톡 미리보기", out.getvalue())


if __name__ == "__main__":
    unittest.main()
