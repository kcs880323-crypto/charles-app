import unittest

from kidsnote_kakao.formatter import build_messages
from kidsnote_kakao.models import QueueItem


def item(body, cat="공지", prio="normal", child="", title="키즈노트", created=0.0):
    return QueueItem(created=created, hold_until=created, priority=prio, category=cat, child=child,
                     title=title, body=body, source="android")


class FormatterTest(unittest.TestCase):
    def test_single(self):
        [m] = build_messages([item("내일 우비를 챙겨주세요", "준비물", child="지우")], 200, 3, "키즈노트")
        self.assertEqual(m, "[키즈노트·준비물] 지우\n내일 우비를 챙겨주세요")

    def test_single_urgent_with_title(self):
        [m] = build_messages([item("투약 완료", "투약", "urgent", title="해바라기반")], 200, 3, "키즈노트")
        self.assertTrue(m.startswith("❗[키즈노트·투약]"))
        self.assertIn("해바라기반", m)

    def test_single_truncated(self):
        [m] = build_messages([item("가" * 500)], 200, 3, "키즈노트")
        self.assertEqual(len(m), 200)
        self.assertTrue(m.endswith("…"))

    def test_digest_sorted_urgent_first(self):
        items = [item("앨범 등록", "앨범", "low", created=1), item("투약 완료", "투약", "urgent", created=2),
                 item("공지 등록", created=3)]
        msgs = build_messages(items, 200, 3, "키즈노트")
        self.assertEqual(len(msgs), 1)
        lines = msgs[0].splitlines()
        self.assertEqual(lines[0], "[키즈노트] 새 소식 3건")
        self.assertTrue(lines[1].startswith("❗ [투약]"))
        self.assertTrue(lines[3].startswith("• [앨범]"))

    def test_split_and_overflow(self):
        items = [item(f"{i}번째 공지 " + "나" * 60, created=i) for i in range(20)]
        msgs = build_messages(items, 200, 3, "키즈노트")
        self.assertEqual(len(msgs), 3)
        for m in msgs:
            self.assertLessEqual(len(m), 200)
        self.assertTrue(msgs[1].startswith("[키즈노트] (계속)"))
        self.assertIn("…외 ", msgs[-1])
        shown = sum(m.count("\n• ") for m in msgs)
        hidden = int(msgs[-1].split("…외 ")[1].split("건")[0])
        self.assertEqual(shown + hidden, 20)

    def test_long_limit_no_split(self):
        items = [item(f"{i}번째 공지 " + "나" * 60, created=i) for i in range(20)]
        self.assertEqual(len(build_messages(items, 4000, 3, "키즈노트")), 1)

    def test_empty(self):
        self.assertEqual(build_messages([], 200, 3, "t"), [])
        self.assertEqual(build_messages([item("x")], 200, 0, "t"), [])


if __name__ == "__main__":
    unittest.main()
