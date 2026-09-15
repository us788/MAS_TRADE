"""수집 저장소 — SQLite.

**중복 제거가 구조적으로 보장되는 것**이 SQLite를 쓰는 첫 번째 이유다.
`(symbol, content_hash)`가 PRIMARY KEY라 같은 기사를 두 번 넣을 수 없다.
JSONL로 날짜별 파일을 쓰던 때는 중복 검사 범위가 파일 하나여서, 1시간 주기로
같은 기사를 하루 24번 다시 받다가 **자정을 넘길 때마다 중복이 새로 생겼다.**

원칙 (기획서 8절):

1. **원본 응답은 파일로 따로 남긴다.** 재현성을 위해 쌓아둘 뿐 거의 읽지 않으므로
   DB에 넣으면 용량만 불린다. `data/snapshots/raw/` 아래.
2. **수집 실패는 예외가 아니라 정상 경로다.** gap으로 기록해 다음 실행에서 재시도한다.
   과거 뉴스는 나중에 살 수 없어 거른 구간은 영구 손실이다.
3. **먼저 본 것을 남긴다.** 재수집 시 INSERT OR IGNORE이므로 `collected_at`은 최초
   관측 시각으로 고정된다. known_at의 보수적 판정이 이 값에 기대기 때문이다.

레이아웃:
    data/mas_trade.sqlite3                          기사·gap·수집이력
    data/snapshots/raw/{source}/{날짜}/{종목}_{ts}.json  원본 응답
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from src import config
from src.data.news import NewsItem

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS news (
    symbol            TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    source            TEXT NOT NULL,
    market            TEXT NOT NULL,
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    original_url      TEXT NOT NULL DEFAULT '',
    publisher         TEXT NOT NULL DEFAULT '',
    published_at      TEXT NOT NULL,
    collected_at      TEXT NOT NULL,
    known_at          TEXT NOT NULL,
    timestamp_suspect INTEGER NOT NULL DEFAULT 0,
    relevance         TEXT NOT NULL DEFAULT 'none',
    vendor_tagged     INTEGER NOT NULL DEFAULT 0,
    extra             TEXT,
    PRIMARY KEY (symbol, content_hash)
);

-- as-of 조회는 항상 known_at 기준이다 (published_at 아님).
CREATE INDEX IF NOT EXISTS idx_news_symbol_known ON news(symbol, known_at);
CREATE INDEX IF NOT EXISTS idx_news_market_known ON news(market, known_at);
CREATE INDEX IF NOT EXISTS idx_news_relevance    ON news(symbol, relevance, known_at);

CREATE TABLE IF NOT EXISTS collection_gaps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    market    TEXT NOT NULL,
    source    TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    reason    TEXT NOT NULL,
    resolved  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gaps_open ON collection_gaps(resolved, at);

-- 수집 이력. 다음 수집 대상을 고르는 근거이자 커버리지 감사 기록.
CREATE TABLE IF NOT EXISTS collection_runs (
    symbol        TEXT NOT NULL,
    source        TEXT NOT NULL,
    ran_at        TEXT NOT NULL,
    oldest_seen   TEXT,
    reached_floor INTEGER NOT NULL DEFAULT 0,
    pages         INTEGER NOT NULL DEFAULT 0,
    fetched       INTEGER NOT NULL DEFAULT 0,
    inserted      INTEGER NOT NULL DEFAULT 0,
    duplicates    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, source, ran_at)
);
CREATE INDEX IF NOT EXISTS idx_runs_latest ON collection_runs(symbol, source, ran_at DESC);
"""


@dataclass
class WriteResult:
    written: int
    skipped_duplicate: int

    @property
    def total(self) -> int:
        return self.written + self.skipped_duplicate


class Store:
    """SQLite 저장소. 원본 응답만 파일로 따로 둔다."""

    def __init__(self, db_path: Path | None = None, raw_dir: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else config.DATA_DIR / "mas_trade.sqlite3"
        self.raw_dir = Path(raw_dir) if raw_dir else config.SNAPSHOT_DIR / "raw"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- 연결 ----

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            # WAL: 수집 중에도 읽기가 막히지 않는다.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                             (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"스키마 버전 불일치: DB {row['version']} vs 코드 {SCHEMA_VERSION}. "
                    "마이그레이션이 필요합니다."
                )

    # ---- 기사 ----

    def add_news(self, items: Iterable[NewsItem]) -> WriteResult:
        """INSERT OR IGNORE. 같은 (종목, 기사)는 최초 관측본이 남는다.

        collected_at을 최초 값으로 고정하는 것이 중요하다 — known_at의 보수적 판정이
        이 값에 기대기 때문이다. 나중에 받은 것으로 덮으면 '언제 알았나'가 늦춰진다.
        """
        rows = [(
            i.symbol, i.content_hash, i.source, i.market, i.title, i.summary,
            i.url, i.original_url, i.publisher,
            i.published_at.isoformat(), i.collected_at.isoformat(), i.known_at.isoformat(),
            int(i.timestamp_suspect), i.relevance, int(i.vendor_tagged),
            json.dumps(i.extra, ensure_ascii=False) if i.extra else None,
        ) for i in items]
        if not rows:
            return WriteResult(0, 0)

        with self.connect() as conn:
            before = conn.execute("SELECT COUNT(*) AS n FROM news").fetchone()["n"]
            conn.executemany(
                """INSERT OR IGNORE INTO news (
                       symbol, content_hash, source, market, title, summary,
                       url, original_url, publisher,
                       published_at, collected_at, known_at,
                       timestamp_suspect, relevance, vendor_tagged, extra
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            after = conn.execute("SELECT COUNT(*) AS n FROM news").fetchone()["n"]

        written = after - before
        return WriteResult(written=written, skipped_duplicate=len(rows) - written)

    def news_for(
        self,
        symbol: str,
        as_of: datetime,
        lookback_days: float = 7,
        primary_only: bool = True,
    ) -> list[dict]:
        """as_of 시점에 알 수 있었던 기사. 필터는 항상 known_at 기준이다."""
        cutoff = _utc(as_of)
        floor = cutoff - timedelta(days=lookback_days)
        sql = ["SELECT * FROM news WHERE symbol = ? AND known_at <= ? AND known_at >= ?"]
        params: list = [symbol, cutoff.isoformat(), floor.isoformat()]
        if primary_only:
            sql.append("AND (vendor_tagged = 1 OR relevance = 'title')")
        sql.append("ORDER BY known_at DESC")
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(" ".join(sql), params)]

    def counts(self) -> dict:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM news").fetchone()["n"]
            by_rel = {r["relevance"]: r["n"] for r in conn.execute(
                "SELECT relevance, COUNT(*) AS n FROM news GROUP BY relevance")}
            by_market = {r["market"]: r["n"] for r in conn.execute(
                "SELECT market, COUNT(*) AS n FROM news GROUP BY market")}
        return {"total": total, "by_relevance": by_rel, "by_market": by_market}

    # ---- 수집 이력 / 구멍 ----

    def record_run(
        self,
        symbol: str,
        source: str,
        *,
        ran_at: datetime,
        oldest_seen: datetime | None,
        reached_floor: bool,
        pages: int,
        fetched: int,
        inserted: int,
        duplicates: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO collection_runs (
                       symbol, source, ran_at, oldest_seen, reached_floor,
                       pages, fetched, inserted, duplicates
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (symbol, source, _utc(ran_at).isoformat(),
                 _utc(oldest_seen).isoformat() if oldest_seen else None,
                 int(reached_floor), pages, fetched, inserted, duplicates),
            )

    def last_run_at(self, symbol: str, source: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(ran_at) AS ran_at FROM collection_runs "
                "WHERE symbol = ? AND source = ?", (symbol, source)).fetchone()
        return datetime.fromisoformat(row["ran_at"]) if row and row["ran_at"] else None

    def record_gap(self, market: str, source: str, symbol: str, reason: str,
                   at: datetime | None = None) -> None:
        """수집 실패·커버리지 미달을 남긴다. 조용히 넘어가면 구멍을 영영 모른다."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO collection_gaps (at, market, source, symbol, reason) "
                "VALUES (?,?,?,?,?)",
                (_utc(at or datetime.now(timezone.utc)).isoformat(),
                 market, source, symbol, reason[:300]),
            )

    def open_gaps(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM collection_gaps WHERE resolved = 0 ORDER BY at DESC")]

    def resolve_gap(self, gap_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE collection_gaps SET resolved = 1 WHERE id = ?", (gap_id,))

    # ---- 원본 응답 ----

    def save_raw(self, source: str, symbol: str, payload, collected_at: datetime) -> Path:
        """원본 응답을 그대로 남긴다. 파서를 고쳤을 때 재현하려면 이것이 있어야 한다."""
        stamp = _utc(collected_at)
        path = (self.raw_dir / source / stamp.strftime("%Y-%m-%d")
                / f"{symbol.replace('/', '_')}_{stamp.strftime('%Y%m%dT%H%M%SZ')}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
