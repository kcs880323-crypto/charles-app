import unittest
from datetime import datetime

from helpers import KST, kst, make_cfg

from kidsnote_kakao.config import CategoryRule, ChildRule
from kidsnote_kakao.models import Notification
from kidsnote_kakao.rules import RuleEngine
from kidsnote_kakao.timeutil import in_range, parse_hhmm, parse_range


def note(body, title="키즈노트", **kw):
    return Notification(source=kw.pop("source", "android"), title=title, body=body, **kw)


class TimeUtilTest(unittest.TestCase):
    def test_overnight_range(self):
        s, e = parse_range("22:00-07:00")
        self.assertTrue(in_range(parse_hhmm("23:30"), s, e))
        self.assertTrue(in_range(parse_hhmm("06:59"), s, e))
        self.assertFalse(in_range(parse_hhmm("07:00"), s, e))
        self.assertFalse(in_range(parse_hhmm("12:00"), s, e))

    def test_bad_format(self):
        with self.assertRaises(ValueError):
            parse_hhmm("25:00")
        with self.assertRaises(ValueError):
            parse_range("22:00")


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.r = RuleEngine(make_cfg().rules)

    def test_urgent_beats_normal(self):
        d = self.r.classify(note("알림장: 오늘 투약 의뢰하신 약 먹였습니다"))
        self.assertTrue(d.accepted)
        self.assertEqual((d.category, d.priority), ("투약", "urgent"))

    def test_normal_beats_low(self):
        d = self.r.classify(note("알림장이 등록되었습니다. 사진 3장"))
        self.assertEqual((d.category, d.priority), ("알림장", "normal"))

    def test_low(self):
        d = self.r.classify(note("새 앨범이 등록되었습니다"))
        self.assertEqual((d.category, d.priority), ("앨범", "low"))

    def test_default_category(self):
        d = self.r.classify(note("좋은 하루 보내세요"))
        self.assertEqual((d.category, d.priority), ("기타", "normal"))

    def test_empty_dropped(self):
        self.assertFalse(self.r.classify(Notification(source="android")).accepted)

    def test_package_filter(self):
        ok = self.r.classify(note("공지", package="com.vaultmicro.kidsnote"))
        other = self.r.classify(note("공지", package="com.kakao.talk", app="카카오톡"))
        by_name = self.r.classify(note("공지", app="키즈노트"))
        unknown = self.r.classify(note("공지"))
        self.assertTrue(ok.accepted)
        self.assertFalse(other.accepted)
        self.assertTrue(by_name.accepted)
        self.assertTrue(unknown.accepted)  # 포워더가 이미 걸렀다고 봄

    def test_package_filter_only_android(self):
        self.assertTrue(self.r.classify(note("공지", source="email", app="noreply@kidsnote.com")).accepted)


class CustomRulesTest(unittest.TestCase):
    def test_exclude_include(self):
        r = RuleEngine(make_cfg(exclude_keywords=["광고"], exclude_patterns=[r"^\d+개의 새 알림$"]).rules)
        self.assertFalse(r.classify(note("[광고] 신학기 이벤트")).accepted)
        self.assertFalse(r.classify(note("3개의 새 알림", title="")).accepted)
        r2 = RuleEngine(make_cfg(include_keywords=["해바라기반"]).rules)
        self.assertFalse(r2.classify(note("공지입니다")).accepted)
        self.assertTrue(r2.classify(note("해바라기반 공지입니다")).accepted)

    def test_user_category_overrides_default(self):
        r = RuleEngine(make_cfg(categories=[CategoryRule("앨범", ["앨범"], priority="mute"),
                                            CategoryRule("소풍", ["소풍"], priority="urgent")]).rules)
        self.assertFalse(r.classify(note("새 앨범")).accepted)
        d = r.classify(note("내일 소풍 행사"))
        self.assertEqual((d.category, d.priority), ("소풍", "urgent"))

    def test_use_default_categories_false(self):
        r = RuleEngine(make_cfg(use_default_categories=False).rules)
        d = r.classify(note("투약 완료"))
        self.assertEqual((d.category, d.priority), ("기타", "normal"))

    def test_children(self):
        kids = [ChildRule("지우", aliases=["김지우"], mute_categories=["식단"]),
                ChildRule("하준", enabled=False),
                ChildRule("서연", categories=["투약", "귀가"])]
        r = RuleEngine(make_cfg(children=kids).rules)
        d = r.classify(note("김지우 어린이 알림장"))
        self.assertEqual(d.child, "지우")
        self.assertFalse(r.classify(note("지우 이번 주 식단표")).accepted)
        self.assertFalse(r.classify(note("하준 알림장")).accepted)
        self.assertFalse(r.classify(note("서연 알림장")).accepted)
        self.assertTrue(r.classify(note("서연 투약 완료")).accepted)
        self.assertEqual(r.classify(note("반 전체 공지")).child, "")

    def test_child_hint(self):
        r = RuleEngine(make_cfg(children=[ChildRule("지우", aliases=["Jiwoo"])]).rules)
        self.assertEqual(r.classify(note("알림장", child_hint="Jiwoo")).child, "지우")


class HoldTest(unittest.TestCase):
    def setUp(self):
        self.r = RuleEngine(make_cfg(quiet_hours="22:00-07:00", digest_times=["12:30", "19:00"]).rules)

    def at(self, ts):
        return datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M")

    def test_daytime_normal_immediate(self):
        now = kst(2026, 11, 5, 10)
        self.assertEqual(self.r.hold_until("normal", now), now)

    def test_quiet_normal_waits_until_morning(self):
        self.assertEqual(self.at(self.r.hold_until("normal", kst(2026, 11, 5, 23))), "11-06 07:00")
        self.assertEqual(self.at(self.r.hold_until("normal", kst(2026, 11, 6, 3))), "11-06 07:00")

    def test_urgent_bypass(self):
        now = kst(2026, 11, 5, 23)
        self.assertEqual(self.r.hold_until("urgent", now), now)
        cfg = make_cfg(quiet_hours="22:00-07:00", urgent_bypass_quiet=False)
        self.assertEqual(self.at(RuleEngine(cfg.rules).hold_until("urgent", now)), "11-06 07:00")

    def test_low_goes_to_next_digest(self):
        self.assertEqual(self.at(self.r.hold_until("low", kst(2026, 11, 5, 10))), "11-05 12:30")
        self.assertEqual(self.at(self.r.hold_until("low", kst(2026, 11, 5, 13))), "11-05 19:00")
        self.assertEqual(self.at(self.r.hold_until("low", kst(2026, 11, 5, 20))), "11-06 12:30")

    def test_low_digest_inside_quiet_moves_to_quiet_end(self):
        r = RuleEngine(make_cfg(quiet_hours="22:00-07:00", digest_times=["23:00"]).rules)
        self.assertEqual(self.at(r.hold_until("low", kst(2026, 11, 5, 20))), "11-06 07:00")


if __name__ == "__main__":
    unittest.main()
