import unittest

from helpers import FakeClock, FakeSender, kst, make_cfg

from kidsnote_kakao.dispatcher import Dispatcher
from kidsnote_kakao.models import Notification
from kidsnote_kakao.pipeline import Pipeline
from kidsnote_kakao.rules import RuleEngine
from kidsnote_kakao.senders.base import AuthError, SendError
from kidsnote_kakao.store import Store


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(kst(2026, 11, 5, 10))
        self.cfg = make_cfg(quiet_hours="22:00-07:00", digest_times=["19:00"])
        self.cfg.delivery.batch_window_seconds = 60
        self.cfg.delivery.batch_max_wait_seconds = 300
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.rules = RuleEngine(self.cfg.rules)
        self.kakao = FakeSender("kakao", 200, daily_limit=100)
        self.tg = FakeSender("telegram", 4000)
        self.make([self.kakao, self.tg])

    def make(self, senders):
        self.d = Dispatcher(self.cfg, self.store, senders, self.rules, clock=self.clock)
        self.p = Pipeline(self.cfg, self.store, self.rules, clock=self.clock)

    def submit(self, body, title="키즈노트"):
        return self.p.submit(Notification(source="android", title=title, body=body))

    def test_urgent_sent_immediately(self):
        self.assertEqual(self.submit("투약 완료했습니다").status, "queued")
        self.assertEqual(self.d.tick(), 1)
        self.assertEqual(len(self.kakao.sent), 1)
        self.assertEqual(self.tg.sent, [])  # fallback 모드: 카카오 성공 시 텔레그램 안 보냄
        self.assertEqual(self.store.queue_size(), 0)

    def test_normal_debounced_and_batched(self):
        self.submit("알림장이 등록되었습니다")
        self.assertEqual(self.d.tick(), 0)
        self.clock.advance(30)
        self.submit("공지사항이 등록되었습니다")
        self.clock.advance(40)  # 첫 알림 70초, 마지막 40초 → 아직
        self.assertEqual(self.d.tick(), 0)
        self.clock.advance(25)
        self.assertEqual(self.d.tick(), 2)
        self.assertEqual(len(self.kakao.sent), 1)
        self.assertIn("새 소식 2건", self.kakao.sent[0])

    def test_max_wait(self):
        flushed = []
        for i in range(12):
            self.submit(f"공지 {i}")
            self.clock.advance(30)
            flushed.append(self.d.tick())
        # 30초마다 계속 들어와도 첫 알림 후 300초가 되면 한 번 묶어서 보낸다
        self.assertEqual([n for n in flushed if n], [10])
        self.assertIn("새 소식 10건", self.kakao.sent[0])
        self.assertEqual(self.store.queue_size(), 2)

    def test_urgent_flushes_pending_normal(self):
        self.submit("알림장")
        self.submit("투약 완료")
        self.assertEqual(self.d.tick(), 2)

    def test_duplicate(self):
        self.assertEqual(self.submit("알림장").status, "queued")
        self.assertEqual(self.submit("알림장").status, "duplicate")

    def test_quiet_hours_hold_until_morning(self):
        self.clock.t = kst(2026, 11, 5, 23)
        self.submit("알림장")
        self.clock.advance(3600)
        self.assertEqual(self.d.tick(), 0)
        self.clock.t = kst(2026, 11, 6, 7, 0, 30)
        self.assertEqual(self.d.tick(), 0)  # 풀린 시각부터 묶음 대기
        self.clock.t = kst(2026, 11, 6, 7, 1, 1)
        self.assertEqual(self.d.tick(), 1)

    def test_low_waits_for_digest(self):
        self.submit("새 앨범이 등록되었습니다")
        self.clock.advance(3600)
        self.assertEqual(self.d.tick(), 0)
        self.clock.t = kst(2026, 11, 5, 19, 1, 1)
        self.assertEqual(self.d.tick(), 1)

    def test_fallback_on_failure(self):
        self.kakao.fail = SendError("down")
        self.submit("투약")
        self.assertEqual(self.d.tick(), 1)
        self.assertEqual(len(self.tg.sent), 1)

    def test_auth_error_marks_dead_and_alerts_other(self):
        self.kakao.fail = AuthError("token expired")
        self.submit("투약")
        self.assertEqual(self.d.tick(), 1)
        self.assertFalse(self.kakao.available()[0])
        self.assertEqual(len(self.tg.sent), 2)  # 경고 1 + 알림 1
        self.assertIn("발송 중단", self.tg.sent[0])

    def test_all_fail_backoff_then_retry(self):
        self.kakao.fail = SendError("down")
        self.tg.fail = SendError("down")
        self.submit("투약")
        self.assertEqual(self.d.tick(), 0)
        self.assertEqual(self.store.queue_size(), 1)
        self.kakao.fail = None
        self.clock.advance(10)
        self.assertEqual(self.d.tick(), 0)  # 백오프 중
        self.clock.advance(30)
        self.assertEqual(self.d.tick(), 1)

    def test_broadcast(self):
        self.cfg.delivery.mode = "broadcast"
        self.submit("투약")
        self.d.tick()
        self.assertEqual((len(self.kakao.sent), len(self.tg.sent)), (1, 1))

    def test_daily_limit_falls_back(self):
        self.kakao.daily_limit = 1
        self.submit("투약 1")
        self.d.tick()
        self.submit("투약 2")
        self.d.tick()
        self.assertEqual((len(self.kakao.sent), len(self.tg.sent)), (1, 1))

    def test_low_quota_mode_slows_batches(self):
        self.kakao.daily_limit = 12
        self.cfg.delivery.low_quota_reserve = 12
        self.cfg.delivery.low_quota_batch_seconds = 3600
        self.submit("알림장")
        self.clock.advance(400)
        self.assertEqual(self.d.tick(), 0)
        self.clock.advance(3600)
        self.assertEqual(self.d.tick(), 1)

    def test_alert_cooldown(self):
        self.make([self.kakao])
        self.assertTrue(self.d.alert("x", "hello"))
        self.assertFalse(self.d.alert("x", "hello"))
        self.clock.advance(3601)
        self.assertTrue(self.d.alert("x", "hello"))
        self.assertTrue(all(len(m) <= 200 for m in self.kakao.sent))

    def test_maintenance_drops_stale_and_silence_alert(self):
        self.cfg.monitor.silence_alert_hours = 24
        self.kakao.fail = SendError("down")
        self.tg.fail = SendError("down")
        self.submit("알림장")
        self.clock.advance(73 * 3600)
        self.kakao.fail = None
        self.d.maintenance()
        self.assertEqual(self.store.queue_size(), 0)
        keys = " ".join(self.kakao.sent)
        self.assertIn("버렸습니다", keys)
        self.assertIn("들어온 알림이 없습니다", keys)

    def test_status(self):
        self.submit("알림장")
        st = self.d.status()
        self.assertEqual(st["queue"], 1)
        self.assertIn("kakao", st["senders"])


if __name__ == "__main__":
    unittest.main()
