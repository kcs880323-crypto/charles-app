"""SQLite 상태 저장소: 중복 지문 / 발송 대기열 / 일일 발송 수 / 키-값.

중복 판정 테이블에는 해시만 저장하고, 알림 본문은 발송 대기 중에만 보관했다가 발송 후 지운다.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import PRIORITY_RANK, RANK_PRIORITY, QueueItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (fp TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    hold_until REAL NOT NULL,
    priority INTEGER NOT NULL,
    category TEXT NOT NULL,
    child TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS queue_hold ON queue(hold_until);
CREATE TABLE IF NOT EXISTS sends (day TEXT NOT NULL, sender TEXT NOT NULL, count INTEGER NOT NULL,
                                  PRIMARY KEY (day, sender));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- 중복 --------------------------------------------------------------

    def check_and_mark(self, fp: str, now: float, window_seconds: float) -> bool:
        """이미 window 안에 본 지문이면 True(중복). 아니면 기록하고 False."""
        with self._lock:
            row = self._db.execute("SELECT ts FROM seen WHERE fp=?", (fp,)).fetchone()
            if row and now - row[0] < window_seconds:
                return True
            self._db.execute("INSERT OR REPLACE INTO seen(fp, ts) VALUES (?, ?)", (fp, now))
            return False

    def purge_seen(self, before: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM seen WHERE ts < ?", (before,)).rowcount

    # --- 대기열 ------------------------------------------------------------

    def enqueue(self, it: QueueItem) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO queue(created, hold_until, priority, category, child, title, body, source)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (it.created, it.hold_until, PRIORITY_RANK.get(it.priority, 1), it.category, it.child,
                 it.title, it.body, it.source),
            )
            it.id = cur.lastrowid
            return it.id

    def _rows(self, sql: str, args: tuple) -> list[QueueItem]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            QueueItem(id=r[0], created=r[1], hold_until=r[2], priority=RANK_PRIORITY.get(r[3], "normal"),
                      category=r[4], child=r[5], title=r[6], body=r[7], source=r[8], attempts=r[9])
            for r in rows
        ]

    _COLS = "id, created, hold_until, priority, category, child, title, body, source, attempts"

    def ready(self, now: float) -> list[QueueItem]:
        return self._rows(f"SELECT {self._COLS} FROM queue WHERE hold_until <= ? ORDER BY priority, created", (now,))

    def all_pending(self) -> list[QueueItem]:
        return self._rows(f"SELECT {self._COLS} FROM queue ORDER BY hold_until, created", ())

    def delete(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM queue WHERE id=?", [(i,) for i in ids])

    def bump_attempts(self, ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("UPDATE queue SET attempts = attempts + 1 WHERE id=?", [(i,) for i in ids])

    def drop_stale(self, created_before: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM queue WHERE created < ?", (created_before,)).rowcount

    def queue_size(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    # --- 발송 수 -----------------------------------------------------------

    def incr_sends(self, day: str, sender: str, n: int = 1) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sends(day, sender, count) VALUES (?,?,?)"
                " ON CONFLICT(day, sender) DO UPDATE SET count = count + excluded.count",
                (day, sender, n),
            )

    def sends(self, day: str, sender: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT count FROM sends WHERE day=? AND sender=?", (day, sender)).fetchone()
        return row[0] if row else 0

    # --- 키-값 -------------------------------------------------------------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (key, value))
