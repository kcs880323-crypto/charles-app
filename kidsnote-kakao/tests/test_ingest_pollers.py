import unittest
from email.message import EmailMessage

from helpers import FakeClock, FakeHttp, kst, make_cfg

from kidsnote_kakao.config import EmailConfig, WebChild, WebEndpoint, WebPollConfig
from kidsnote_kakao.ingest.email_imap import (
    ImapPoller,
    email_to_notification,
    html_to_text,
)
from kidsnote_kakao.ingest.kidsnote_web import (
    KidsnoteWebPoller,
    SessionExpired,
    item_to_notification,
)
from kidsnote_kakao.pipeline import Pipeline
from kidsnote_kakao.rules import RuleEngine
from kidsnote_kakao.store import Store


def make_mail(uid, subject, body, html=False):
    m = EmailMessage()
    m["From"] = "키즈노트 <noreply@kidsnote.com>"
    m["Subject"] = subject
    m["Message-ID"] = f"<{uid}@kidsnote.com>"
    if html:
        m.set_content("<html><head><style>p{}</style></head><body><p>" + body + "</p><br>끝</body></html>",
                      subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


class FakeImap:
    def __init__(self, mails: dict[int, bytes], uidvalidity=b"1"):
        self.mails = mails
        self.uidvalidity = uidvalidity
        self.logged_out = False

    def login(self, u, p):
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        assert readonly
        return "OK", [str(len(self.mails)).encode()]

    def response(self, code):
        return code, [self.uidvalidity]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            if args == ("UID", "*"):
                return "OK", [str(max(self.mails)).encode() if self.mails else b""]
            lo = int(args[1].split(":")[0])
            hits = [u for u in self.mails if u >= lo] or [max(self.mails)]  # 'N:*' 동작 흉내
            return "OK", [" ".join(map(str, hits)).encode()]
        if cmd == "FETCH":
            u = int(args[0])
            assert args[1] == "(BODY.PEEK[])"
            return "OK", [(f"{u} (UID {u} BODY[] {{1}}".encode(), self.mails[u]), b")"]
        raise AssertionError(cmd)

    def logout(self):
        self.logged_out = True


class EmailTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.pipeline = Pipeline(self.cfg, self.store, RuleEngine(self.cfg.rules),
                                 clock=FakeClock(kst(2026, 11, 5, 10)))

    def test_parse_plain_and_html(self):
        n = email_to_notification(make_mail(1, "[키즈노트] 새 공지", "11월 행사 안내입니다"))
        self.assertEqual((n.source, n.title, n.body), ("email", "[키즈노트] 새 공지", "11월 행사 안내입니다"))
        self.assertEqual(n.external_id, "<1@kidsnote.com>")
        h = email_to_notification(make_mail(2, "s", "준비물 &amp; 우비", html=True))
        self.assertEqual(h.body, "준비물 & 우비\n\n끝")
        self.assertNotIn("p{}", html_to_text("<style>p{}</style>x"))

    def test_baseline_then_new(self):
        mails = {10: make_mail(10, "old", "옛 공지")}
        imap = FakeImap(mails)
        poller = ImapPoller(EmailConfig(enabled=True, host="h"), self.pipeline, self.store, lambda *a: None,
                            imap_factory=lambda h, p: imap)
        self.assertEqual(poller.poll_once(), 0)  # 기준점만
        self.assertEqual(self.store.queue_size(), 0)
        self.assertEqual(poller.poll_once(), 0)  # 새 메일 없음('N:*' 가 마지막 UID 를 돌려줘도 무시)
        mails[11] = make_mail(11, "새 알림장", "알림장이 등록되었습니다")
        mails[12] = make_mail(12, "준비물", "우비 챙겨주세요")
        self.assertEqual(poller.poll_once(), 2)
        self.assertEqual(self.store.queue_size(), 2)
        self.assertEqual(self.store.kv_get("imap:last_uid"), "12")
        self.assertTrue(imap.logged_out)

    def test_uidvalidity_change_resets(self):
        mails = {5: make_mail(5, "a", "b")}
        imap = FakeImap(mails)
        poller = ImapPoller(EmailConfig(enabled=True, host="h"), self.pipeline, self.store, lambda *a: None,
                            imap_factory=lambda h, p: imap)
        poller.poll_once()
        mails[6] = make_mail(6, "c", "d")
        imap.uidvalidity = b"2"
        self.assertEqual(poller.poll_once(), 0)
        self.assertEqual(self.store.kv_get("imap:uidvalidity"), "2")


class WebTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.pipeline = Pipeline(self.cfg, self.store, RuleEngine(self.cfg.rules),
                                 clock=FakeClock(kst(2026, 11, 5, 10)))
        self.http = FakeHttp()
        self.web = WebPollConfig(enabled=True, session_cookie="sessionid=abc",
                                 children=[WebChild("123", "지우")],
                                 endpoints=[WebEndpoint("알림장", "/api/v1_2/children/{child_id}/reports/")])
        self.alerts = []
        self.poller = KidsnoteWebPoller(self.web, self.pipeline, self.store, lambda k, t: self.alerts.append(k),
                                        http=self.http)
        self.url = "https://www.kidsnote.com/api/v1_2/children/123/reports/"

    def test_first_run_baseline_then_new(self):
        self.http.add("GET", self.url, 200, {"results": [{"id": 2, "content": "b"}, {"id": 1, "content": "a"}]})
        self.http.add("GET", self.url, 200, {"results": [{"id": 3, "content": "내일 우비 챙겨주세요"},
                                                         {"id": 2, "content": "b"}]})
        self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual(self.poller.poll_once(), 1)
        [item] = self.store.all_pending()
        self.assertEqual((item.child, item.category), ("지우", "준비물"))
        self.assertEqual(self.http.calls[0]["headers"]["Cookie"], "sessionid=abc")

    def test_session_expired(self):
        for status, body in [(302, b""), (401, b"{}"), (200, b"<html>login</html>")]:
            self.http.routes.clear()
            self.http.add("GET", self.url, status, body)
            with self.subTest(status=status), self.assertRaises(SessionExpired):
                self.poller.poll_once()

    def test_item_mapping(self):
        n = item_to_notification({"id": 9, "title": "현장학습", "content": "본문", "author": {"name": "김선생"}},
                                 "공지", WebChild("1"))
        self.assertEqual((n.title, n.body, n.external_id), ("[공지] 현장학습 - 김선생", "본문", "공지:1:9"))
        self.assertIsNone(item_to_notification({"content": "id 없음"}, "공지", WebChild("1")))

    def test_discover_children(self):
        self.http.add("GET", "https://www.kidsnote.com/api/v1/me/info", 200,
                      {"user": {"children": [{"id": 55, "name": "지우", "class_name": "해바라기반"}]}})
        self.assertEqual(self.poller.discover_children(), [{"id": 55, "name": "지우", "class": "해바라기반"}])


if __name__ == "__main__":
    unittest.main()
