import json
import unittest

from kidsnote_kakao.ingest.android import PayloadError, decode_body, parse_payload


class AndroidPayloadTest(unittest.TestCase):
    def test_generic_json(self):
        n = parse_payload({"title": "키즈노트", "text": "새 알림장", "package": "com.vaultmicro.kidsnote",
                           "app": "키즈노트"})
        self.assertEqual((n.source, n.title, n.body, n.package), ("android", "키즈노트", "새 알림장",
                                                                   "com.vaultmicro.kidsnote"))

    def test_tasker_unexpanded_vars(self):
        n = parse_payload({"title": "%antitle", "text": "공지가 등록되었습니다", "big_text": "%anbigtext",
                           "app": "%anapp"})
        self.assertEqual((n.title, n.body, n.app), ("", "공지가 등록되었습니다", ""))

    def test_macrodroid_aliases(self):
        n = parse_payload({"not_title": "해바라기반", "notification": "투약 완료", "not_app_name": "[not_app_name]"})
        self.assertEqual((n.title, n.body, n.app), ("해바라기반", "투약 완료", ""))

    def test_big_text_preferred(self):
        n = parse_payload({"title": "t", "text": "내일 준비물…", "big_text": "내일 준비물: 우비, 장화, 여벌옷"})
        self.assertEqual(n.body, "내일 준비물: 우비, 장화, 여벌옷")

    def test_lines(self):
        n = parse_payload({"title": "t", "text": "2개", "lines": ["첫째 알림", "둘째 알림"]})
        self.assertEqual(n.body, "첫째 알림\n둘째 알림")

    def test_empty_rejected(self):
        with self.assertRaises(PayloadError):
            parse_payload({"title": "%antitle", "text": ""})

    def test_decode_form_and_json_and_text(self):
        self.assertEqual(decode_body(b"title=a&text=%EA%B0%80", "application/x-www-form-urlencoded"),
                         {"title": "a", "text": "가"})
        self.assertEqual(decode_body(json.dumps({"text": "x"}).encode(), "application/json; charset=utf-8"),
                         {"text": "x"})
        self.assertEqual(decode_body("그냥 본문".encode(), "text/plain"), {"text": "그냥 본문"})
        with self.assertRaises(PayloadError):
            decode_body(b"[1,2]", "application/json")
        with self.assertRaises(PayloadError):
            decode_body(b"{bad", "application/json")


if __name__ == "__main__":
    unittest.main()
