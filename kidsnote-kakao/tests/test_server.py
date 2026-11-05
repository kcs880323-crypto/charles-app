import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import FakeClock, kst, make_cfg

from kidsnote_kakao.pipeline import Pipeline
from kidsnote_kakao.rules import RuleEngine
from kidsnote_kakao.server import make_server
from kidsnote_kakao.store import Store

TOKEN = "t" * 32


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = make_cfg()
        cfg.server.port = 0
        cfg.server.webhook_token = TOKEN
        cfg.server.max_body_bytes = 2048
        cls.store = Store(":memory:")
        cls.pipeline = Pipeline(cfg, cls.store, RuleEngine(cfg.rules), clock=FakeClock(kst(2026, 11, 5, 10)))
        cls.srv = make_server(cfg.server, cls.pipeline, lambda: {"queue": cls.store.queue_size()})
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.store.close()

    def req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_healthz_public(self):
        self.assertEqual(self.req("GET", "/healthz"), (200, {"ok": True}))

    def test_status_requires_token(self):
        self.assertEqual(self.req("GET", "/status")[0], 401)
        code, body = self.req("GET", "/status", headers={"X-Webhook-Token": TOKEN})
        self.assertEqual(code, 200)
        self.assertIn("queue", body)

    def test_webhook_auth(self):
        body = {"title": "키즈노트", "text": "서버 테스트 알림장"}
        self.assertEqual(self.req("POST", "/webhook/android", body)[0], 401)
        self.assertEqual(self.req("POST", "/webhook/android", body, {"Authorization": "Bearer wrong"})[0], 401)
        code, res = self.req("POST", "/webhook/android", body,
                             {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertEqual((res["status"], res["category"]), ("queued", "알림장"))

    def test_webhook_bad_payload_and_size(self):
        h = {"X-Webhook-Token": TOKEN, "Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/webhook/android", b"{oops", h)[0], 400)
        self.assertEqual(self.req("POST", "/webhook/android", {"text": "x" * 5000}, h)[0], 413)
        self.assertEqual(self.req("POST", "/nope", {"text": "x"}, h)[0], 404)

    def test_short_token_refused(self):
        cfg = make_cfg()
        cfg.server.webhook_token = "short"
        with self.assertRaises(ValueError):
            make_server(cfg.server, self.pipeline, dict)


if __name__ == "__main__":
    unittest.main()
