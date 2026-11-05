import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from helpers import FakeClock, FakeHttp

from kidsnote_kakao.senders.base import QuotaExceeded, SendError
from kidsnote_kakao.senders.kakao import (
    MEMO_URL,
    TOKEN_URL,
    KakaoAuthError,
    KakaoClient,
    KakaoSender,
)


class KakaoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "data" / "kakao_token.json"
        self.http = FakeHttp()
        self.clock = FakeClock(1_000_000.0)
        self.client = KakaoClient("REST_KEY", "http://localhost:8765/kakao/callback", self.token_file,
                                  http=self.http, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def write_token(self, **kw):
        tok = {"access_token": "A1", "refresh_token": "R1", "expires_at": self.clock() + 3600,
               "refresh_token_expires_at": self.clock() + 50 * 86400, "updated_at": self.clock()}
        tok.update(kw)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok))

    def form(self, call):
        return {k: v[0] for k, v in parse_qs(call["data"].decode()).items()}

    def test_authorize_url(self):
        url = self.client.authorize_url("st")
        self.assertIn("client_id=REST_KEY", url)
        self.assertIn("scope=talk_message", url)
        self.assertIn("state=st", url)

    def test_exchange_code_saves_private_file(self):
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A", "refresh_token": "R", "expires_in": 21599,
                                               "refresh_token_expires_in": 5183999, "scope": "talk_message"})
        tok = self.client.exchange_code("CODE")
        self.assertEqual(tok["refresh_token"], "R")
        sent = self.form(self.http.calls[0])
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "CODE")
        self.assertNotIn("client_secret", sent)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)

    def test_client_secret_sent(self):
        self.client.client_secret = "SECRET"
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A", "expires_in": 100})
        self.write_token()
        self.client.refresh()
        self.assertEqual(self.form(self.http.calls[0])["client_secret"], "SECRET")

    def test_refresh_keeps_old_refresh_token_when_not_returned(self):
        self.write_token()
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A2", "expires_in": 21599})
        tok = self.client.refresh()
        self.assertEqual((tok["access_token"], tok["refresh_token"]), ("A2", "R1"))

    def test_refresh_rotates_refresh_token_when_returned(self):
        self.write_token()
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A2", "expires_in": 21599, "refresh_token": "R2",
                                               "refresh_token_expires_in": 5183999})
        tok = self.client.refresh()
        self.assertEqual(tok["refresh_token"], "R2")
        self.assertAlmostEqual(tok["refresh_token_expires_at"], self.clock() + 5183999)
        self.assertEqual(json.loads(self.token_file.read_text())["refresh_token"], "R2")

    def test_refresh_expired_raises_auth_error(self):
        self.write_token()
        self.http.add("POST", TOKEN_URL, 400, {"error": "invalid_grant", "error_code": "KOE322",
                                               "error_description": "expired"})
        with self.assertRaises(KakaoAuthError) as cm:
            self.client.refresh()
        self.assertIn("kakao-auth", str(cm.exception))

    def test_no_token(self):
        with self.assertRaises(KakaoAuthError):
            self.client.access_token()

    def test_proactive_refresh_before_send(self):
        self.write_token(expires_at=self.clock() + 60)
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A2", "expires_in": 21599})
        self.http.add("POST", MEMO_URL, 200, {"result_code": 0})
        self.client.send_memo_text("안녕", "https://www.kidsnote.com")
        memo = [c for c in self.http.calls if c["url"] == MEMO_URL][0]
        self.assertEqual(memo["headers"]["Authorization"], "Bearer A2")
        tpl = json.loads(self.form(memo)["template_object"])
        self.assertEqual(tpl["object_type"], "text")
        self.assertEqual(tpl["link"]["web_url"], "https://www.kidsnote.com")

    def test_401_triggers_refresh_and_retry(self):
        self.write_token()
        self.http.add("POST", MEMO_URL, 401, {"code": -401, "msg": "this access token does not exist"})
        self.http.add("POST", MEMO_URL, 200, {"result_code": 0})
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A2", "expires_in": 21599})
        self.client.send_memo_text("안녕", "https://www.kidsnote.com")
        memos = [c for c in self.http.calls if c["url"] == MEMO_URL]
        self.assertEqual([m["headers"]["Authorization"] for m in memos], ["Bearer A1", "Bearer A2"])

    def test_error_mapping(self):
        self.write_token()
        cases = [(403, {"code": -402, "msg": "insufficient scopes"}, KakaoAuthError),
                 (400, {"code": -10, "msg": "quota"}, QuotaExceeded),
                 (500, {"code": -1}, SendError)]
        for status, body, exc in cases:
            self.http.routes.clear()
            self.http.add("POST", MEMO_URL, status, body)
            with self.subTest(code=body["code"]), self.assertRaises(exc):
                self.client.send_memo_text("x", "https://www.kidsnote.com")
        self.http.routes.clear()
        self.http.add("POST", MEMO_URL, 400, {"code": -2, "msg": "invalid template"})
        with self.assertRaises(SendError) as cm:
            self.client.send_memo_text("x", "https://www.kidsnote.com")
        self.assertFalse(cm.exception.retryable)


class KakaoSenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "kakao_token.json"
        self.http = FakeHttp()
        self.clock = FakeClock(2_000_000.0)
        self.client = KakaoClient("K", "http://localhost/cb", self.token_file, http=self.http, clock=self.clock)
        self.sender = KakaoSender(self.client, "https://www.kidsnote.com")
        self.alerts = []

    def tearDown(self):
        self.tmp.cleanup()

    def alert(self, key, text):
        self.alerts.append((key, text))

    def write(self, **kw):
        tok = {"access_token": "A", "refresh_token": "R", "expires_at": self.clock() + 3600,
               "refresh_token_expires_at": self.clock() + 50 * 86400, "updated_at": self.clock()}
        tok.update(kw)
        self.token_file.write_text(json.dumps(tok))

    def test_unavailable_without_token(self):
        self.assertFalse(self.sender.available()[0])
        self.sender.maintenance(self.clock(), self.alert)
        self.assertEqual(self.alerts[0][0], "kakao:token")

    def test_dead_until_token_file_changes(self):
        self.write()
        self.sender.mark_dead("expired")
        self.assertFalse(self.sender.available()[0])
        os.utime(self.token_file, (1, 1))  # kakao-auth 재실행 흉내
        self.assertTrue(self.sender.available()[0])

    def test_daily_refresh_and_expiry_warning(self):
        self.write(updated_at=self.clock() - 21 * 3600, refresh_token_expires_at=self.clock() + 3 * 86400)
        self.http.add("POST", TOKEN_URL, 200, {"access_token": "A2", "expires_in": 21599})
        self.sender.maintenance(self.clock(), self.alert)
        self.assertEqual(json.loads(self.token_file.read_text())["access_token"], "A2")
        self.assertEqual([k for k, _ in self.alerts], ["kakao:refresh-expiring"])

    def test_daily_refresh_failure_marks_dead(self):
        self.write(updated_at=self.clock() - 21 * 3600)
        self.http.add("POST", TOKEN_URL, 401, {"error": "invalid_grant", "error_code": "KOE322"})
        self.sender.maintenance(self.clock(), self.alert)
        self.assertFalse(self.sender.available()[0])
        self.assertEqual(self.alerts[0][0], "kakao:auth")


if __name__ == "__main__":
    unittest.main()
