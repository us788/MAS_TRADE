"""가격 저장소와 as-of 조회.

설계 근거는 `docs/harness.md`. 코드를 고치기 전에 그 문서를 읽는다.

핵심 규칙 넷.

1. **as_of 조회와 forward 조회를 분리한다.** 에이전트에는 `MarketData`(as_of 전용)만
   주입하고 이 모듈을 직접 넘기지 않는다. 같은 객체에 두 메서드가 있으면 에이전트가
   실수로 미래를 본다. `forward_bar`는 채점기만 부른다.
2. **`close_px`(가격수익률용)와 `close_tr`(총수익률용)을 둘 다 저장한다.**
   두 열의 차이는 **배당**이지 분할이 아니다 — 2026-09-16 실측으로 확인했다.
   yfinance도 FDR도 **분할은 시계열 전체에 소급 적용된 값만 준다.** NVDA 10:1
   분할 전 봉이 44.78로 들어오고(실거래가는 약 447달러) 시계열에 10배 점프가 없다.
   **미수정 실거래가는 이 벤더들로 얻을 수 없다.**

   - `close_px` — 벤더 종가. 분할 조정됨, **배당 미조정**. 기준가와 초과수익 계산용
   - `close_tr` — 분할+배당 조정. 배당 재투자를 가정한 총수익률용

   **초과수익 판정은 `close_px`로 한다.** 벤치마크 `^GSPC`·`KS200`이 배당 미포함
   가격지수이므로, 종목만 배당 조정하면 고배당주가 구조적으로 유리해진다
   (XOM은 3년에 10.4%를 공짜로 얻는다).
3. **거래일 이동은 달력을 계산하지 않는다.** 시계열에 그 시장 거래일만 행이 있으므로
   인덱스 오프셋으로 센다. 임시공휴일·대체공휴일·조기폐장을 직접 관리하지 않기 위해서다.
   대신 **결측일이 없어야 성립한다** — 연속성 검사와 `price_gaps`가 그 전제를 지킨다.
4. **장마감 +30분 이후에만 그날 종가를 아는 것으로 친다.** 늦게 아는 쪽으로 틀리는
   것은 안전하고, 일찍 아는 쪽은 룩어헤드다.

스키마 버전을 `Store`와 따로 두는 이유: `Store._init_schema`는 `schema_version`이
코드와 다르면 예외를 던진다. 되돌릴 수 없는 기사 5,900여 건이 든 DB를 가격 테이블
추가 때문에 마이그레이션하는 것은 위험 대비 실익이 없다. 두 스키마는 서로 독립이므로
각자 버전을 갖는다.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

from src import config

PRICE_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS prices (
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL,          -- US | KR | INDEX
    date        TEXT NOT NULL,          -- 거래일 YYYY-MM-DD (그 시장 현지 기준)
    open_px     REAL,
    high_px     REAL,
    low_px      REAL,
    close_px    REAL NOT NULL,         -- 분할조정·배당미조정. 기준가·초과수익용
    close_tr    REAL NOT NULL,         -- 분할+배당조정. 총수익률용
    volume      INTEGER,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,          -- 이 값을 받은 시각. "그때 받은 값"의 근거
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices(symbol, date);

-- 값이 사후에 바뀐 이력. 뉴스와 달리 가격은 수정되는 것이 정상이므로 덮되 흔적을
-- 남긴다. 다만 close_px(분할조정 가격)가 바뀌면 분할이 새로 반영됐거나 벤더 오류다.
CREATE TABLE IF NOT EXISTS price_revisions (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   REAL NOT NULL,
    new_value   REAL NOT NULL,
    applied     INTEGER NOT NULL,       -- 1이면 덮었다, 0이면 기록만 했다
    noticed_at  TEXT NOT NULL
);

-- 뉴스의 collection_gaps와 같은 역할. 실패는 예외가 아니라 정상 경로다.
CREATE TABLE IF NOT EXISTS price_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    from_date   TEXT NOT NULL,
    to_date     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    noticed_at  TEXT NOT NULL,
    resolved_at TEXT
);
"""

# 시장별 정규장 마감과 "그날 종가를 알 수 있다"고 보는 시각.
# 마감 직후가 아니라 30분 여유를 둔다. 벤더 반영이 늦을 수 있고, 늦게 아는 쪽으로
# 틀리는 것은 안전하지만 일찍 아는 쪽은 룩어헤드다.
# 미국은 서머타임이 있으므로 고정 오프셋을 쓰지 않는다 — ZoneInfo가 계산한다.
MARKET_TZ = {
    "US": ZoneInfo("America/New_York"),
    "KR": ZoneInfo("Asia/Seoul"),
}
MARKET_CLOSE_KNOWN_AT = {
    "US": time(16, 30),   # 정규장 16:00 ET + 30분
    "KR": time(16, 0),    # 정규장 15:30 KST + 30분
}


@dataclass(frozen=True)
class Bar:
    symbol: str
    market: str
    date: date
    close_px: float
    close_tr: float
    source: str
    open_px: float | None = None
    high_px: float | None = None
    low_px: float | None = None
    volume: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Bar":
        return cls(
            symbol=row["symbol"],
            market=row["market"],
            date=date.fromisoformat(row["date"]),
            close_px=row["close_px"],
            close_tr=row["close_tr"],
            source=row["source"],
            open_px=row["open_px"],
            high_px=row["high_px"],
            low_px=row["low_px"],
            volume=row["volume"],
        )


@dataclass
class UpsertResult:
    inserted: int = 0
    revised: int = 0        # close_tr가 바뀌어 덮은 건수 (분할·배당 — 정상)
    conflicted: int = 0     # close_px가 달라 덮지 않은 건수 (벤더 오류 의심)
    unchanged: int = 0


def known_trading_date(market: str, as_of: datetime) -> date:
    """`as_of` 시점에 종가를 알 수 있는 **달력일 상한**.

    실제 거래일인지는 여기서 따지지 않는다. 휴장일이면 DB 조회가 그 이전 봉을 집는다
    (`date <= 상한`). 캘린더를 코드로 들고 있지 않기 위해서다.
    """
    if market not in MARKET_TZ:
        raise ValueError(f"알 수 없는 시장: {market!r}")
    if as_of.tzinfo is None:
        raise ValueError("as_of는 타임존이 있어야 합니다. 날짜가 아니라 타임스탬프입니다.")
    local = as_of.astimezone(MARKET_TZ[market])
    if local.time() >= MARKET_CLOSE_KNOWN_AT[market]:
        return local.date()
    return local.date() - timedelta(days=1)


#: 가격 비교 허용오차. 벤더는 호출마다 미세하게 다른 부동소수점을 준다.
#: 1e-9로 뒀더니 **수정 이력 4,759건 중 4,744건이 잡음**이었다 (2026-09-18 실측).
#: 소수점 넷째 자리까지 유효한 가격을 가정하면 1e-6이 충분하다.
PRICE_REL_TOL = 1e-6


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=PRICE_REL_TOL, abs_tol=1e-6)


class PriceStore:
    """가격 저장·조회. 에이전트에 직접 주입하지 않는다 (`forward_bar` 때문)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else config.DATA_DIR / "mas_trade.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
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
            row = conn.execute("SELECT version FROM price_schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO price_schema_version (version) VALUES (?)",
                             (PRICE_SCHEMA_VERSION,))
            elif row["version"] != PRICE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"가격 스키마 버전 불일치: DB {row['version']} vs 코드 "
                    f"{PRICE_SCHEMA_VERSION}. 마이그레이션이 필요합니다."
                )

    # ---- 쓰기 ----

    def upsert(self, bars: Iterable[Bar], *, force: bool = False) -> UpsertResult:
        """가격은 뉴스와 달리 **덮는다.** 수정주가가 사후에 바뀌는 것이 정상이기 때문이다.

        - `close_tr`만 달라지면 배당 반영이다 → 덮고 `price_revisions`에 남긴다
        - `close_px`가 달라지면 **분할이 새로 소급 적용됐거나 벤더가 값을 고친 것**이다.
          어느 쪽인지 코드가 구분할 수 없으므로 **그 행을 통째로 보류한다.** 둘 다
          기록만 하고 사람이 확인할 때까지 기존 값을 유지한다. 분할이 확인되면
          해당 종목을 `--refetch`로 다시 받는다.
        """
        result = UpsertResult()
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            for bar in bars:
                key = (bar.symbol, bar.date.isoformat())
                old = conn.execute(
                    "SELECT close_px, close_tr FROM prices WHERE symbol = ? AND date = ?",
                    key,
                ).fetchone()

                if old is None:
                    conn.execute(
                        "INSERT INTO prices (symbol, market, date, open_px, high_px,"
                        " low_px, close_px, close_tr, volume, source, fetched_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (bar.symbol, bar.market, bar.date.isoformat(), bar.open_px,
                         bar.high_px, bar.low_px, bar.close_px, bar.close_tr,
                         bar.volume, bar.source, now),
                    )
                    result.inserted += 1
                    continue

                px_changed = not _same(old["close_px"], bar.close_px)
                tr_changed = not _same(old["close_tr"], bar.close_tr)

                if px_changed and force:
                    # 사람이 명시적으로 재적재를 요청했다. 보류 규칙을 넘긴다.
                    conn.execute(
                        "UPDATE prices SET open_px=?, high_px=?, low_px=?, close_px=?,"
                        " close_tr=?, volume=?, source=?, fetched_at=?"
                        " WHERE symbol=? AND date=?",
                        (bar.open_px, bar.high_px, bar.low_px, bar.close_px,
                         bar.close_tr, bar.volume, bar.source, now, *key),
                    )
                    conn.execute(
                        "INSERT INTO price_revisions (symbol, date, field, old_value,"
                        " new_value, applied, noticed_at) VALUES (?,?,?,?,?,1,?)",
                        (*key, "close_px", old["close_px"], bar.close_px, now),
                    )
                    result.revised += 1
                elif px_changed:
                    # 분할이 새로 반영됐거나 벤더가 값을 고쳤다. 코드가 구분할 수 없다.
                    # 이 응답 전체를 보류한다 — 한 열만 골라 받으면 행이 어긋난다.
                    conn.execute(
                        "INSERT INTO price_revisions (symbol, date, field, old_value,"
                        " new_value, applied, noticed_at) VALUES (?,?,?,?,?,0,?)",
                        (*key, "close_px", old["close_px"], bar.close_px, now),
                    )
                    if tr_changed:
                        conn.execute(
                            "INSERT INTO price_revisions (symbol, date, field,"
                            " old_value, new_value, applied, noticed_at)"
                            " VALUES (?,?,?,?,?,0,?)",
                            (*key, "close_tr", old["close_tr"], bar.close_tr, now),
                        )
                    result.conflicted += 1
                elif tr_changed:
                    # 분할·배당. 정상적인 변경이다.
                    conn.execute(
                        "INSERT INTO price_revisions (symbol, date, field, old_value,"
                        " new_value, applied, noticed_at) VALUES (?,?,?,?,?,1,?)",
                        (*key, "close_tr", old["close_tr"], bar.close_tr, now),
                    )
                    conn.execute(
                        "UPDATE prices SET close_tr = ?, fetched_at = ?"
                        " WHERE symbol = ? AND date = ?",
                        (bar.close_tr, now, *key),
                    )
                    result.revised += 1
                else:
                    result.unchanged += 1
        return result

    # ---- as-of 조회 (에이전트용) ----

    def close_at(self, symbol: str, market: str, as_of: datetime) -> Bar | None:
        """`as_of` 시점에 알려져 있던 마지막 종가. 없으면 None."""
        cutoff = known_trading_date(market, as_of)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM prices WHERE symbol = ? AND date <= ?"
                " ORDER BY date DESC LIMIT 1",
                (symbol, cutoff.isoformat()),
            ).fetchone()
        return Bar.from_row(row) if row else None

    def series(self, symbol: str, market: str, as_of: datetime,
               lookback_days: int = 365) -> list[Bar]:
        """`as_of` 이전 일봉. 오래된 것부터. 에이전트가 쓰는 유일한 시계열 조회."""
        cutoff = known_trading_date(market, as_of)
        floor = cutoff - timedelta(days=lookback_days)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM prices WHERE symbol = ? AND date <= ? AND date >= ?"
                " ORDER BY date ASC",
                (symbol, cutoff.isoformat(), floor.isoformat()),
            ).fetchall()
        return [Bar.from_row(r) for r in rows]

    # ---- forward 조회 (채점기 전용) ----
    # 에이전트가 부르면 룩어헤드다. src/eval/ 밖에서 호출하지 않는다.

    def forward_bar(self, symbol: str, base: date, trading_days: int) -> Bar | None:
        """`base` **이후** N번째 거래일의 봉. 아직 안 왔으면 None.

        달력을 계산하지 않는다. 저장된 봉을 순서대로 세는 것으로 거래일이 정의된다.
        `base`가 휴장일이어도 그 다음 거래일부터 센다.

        None은 두 가지를 뜻한다 — 아직 N거래일이 안 지났거나, DB가 거기까지 채워지지
        않았거나. 채점기는 `latest_date()`로 둘을 구분한다.
        """
        if trading_days < 1:
            raise ValueError("trading_days는 1 이상이어야 합니다.")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM prices WHERE symbol = ? AND date > ?"
                " ORDER BY date ASC LIMIT 1 OFFSET ?",
                (symbol, base.isoformat(), trading_days - 1),
            ).fetchone()
        return Bar.from_row(row) if row else None

    def close_at_date(self, symbol: str, on: date) -> Bar | None:
        """`on` 날짜 이하의 마지막 봉. 거래일이 이미 확정된 뒤의 조회에 쓴다.

        `close_at`과 달리 장마감 판정을 하지 않는다 — 날짜가 이미 정해진 경우
        (예: 기준봉의 날짜로 레짐을 계산할 때)에만 쓴다.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM prices WHERE symbol = ? AND date <= ?"
                " ORDER BY date DESC LIMIT 1", (symbol, on.isoformat()),
            ).fetchone()
        return Bar.from_row(row) if row else None

    def bar_on_or_after(self, symbol: str, on: date) -> Bar | None:
        """`on` 날짜 **이상**의 첫 봉. 체결가를 집을 때 쓴다.

        `close_at_date`(이하의 마지막 봉)와 방향이 반대다. 판단은 과거를 보지만
        **체결은 미래에 일어난다** — 수요일 아침에 낸 시그널은 그날 종가로 체결된다.
        휴장이면 다음 거래일이다.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM prices WHERE symbol = ? AND date >= ?"
                " ORDER BY date ASC LIMIT 1", (symbol, on.isoformat()),
            ).fetchone()
        return Bar.from_row(row) if row else None

    def trailing_bar(self, symbol: str, base: date, trading_days: int) -> Bar | None:
        """`base` **이전** N번째 거래일의 봉. `forward_bar`의 과거 방향 대칭.

        미래를 보지 않으므로 as_of 안전하다. 레짐 판정(시그널 시점의 장세)에 쓴다.
        """
        if trading_days < 1:
            raise ValueError("trading_days는 1 이상이어야 합니다.")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM prices WHERE symbol = ? AND date < ?"
                " ORDER BY date DESC LIMIT 1 OFFSET ?",
                (symbol, base.isoformat(), trading_days - 1),
            ).fetchone()
        return Bar.from_row(row) if row else None

    def latest_date(self, symbol: str) -> date | None:
        """이 종목이 DB에 채워진 마지막 거래일. forward_bar의 None을 해석할 때 쓴다."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM prices WHERE symbol = ?", (symbol,)
            ).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    # ---- 상태·gap ----

    def coverage(self, symbol: str) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS bars, MIN(date) AS first, MAX(date) AS last"
                " FROM prices WHERE symbol = ?", (symbol,)
            ).fetchone()
        return {"symbol": symbol, "bars": row["bars"],
                "first": row["first"], "last": row["last"]}

    def counts(self) -> dict:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
            by_market = {r["market"]: r["n"] for r in conn.execute(
                "SELECT market, COUNT(*) AS n FROM prices GROUP BY market")}
            symbols = conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS n FROM prices").fetchone()["n"]
            revisions = conn.execute(
                "SELECT COUNT(*) AS n FROM price_revisions").fetchone()["n"]
        return {"total": total, "by_market": by_market,
                "symbols": symbols, "revisions": revisions}

    def record_gap(self, symbol: str, from_date: date, to_date: date, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO price_gaps (symbol, from_date, to_date, reason, noticed_at)"
                " VALUES (?,?,?,?,?)",
                (symbol, from_date.isoformat(), to_date.isoformat(), reason,
                 datetime.now(timezone.utc).isoformat()),
            )

    def open_gaps(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM price_gaps WHERE resolved_at IS NULL ORDER BY noticed_at")]

    def resolve_gap(self, gap_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE price_gaps SET resolved_at = ? WHERE id = ?",
                         (datetime.now(timezone.utc).isoformat(), gap_id))

    def missing_weekdays(self, symbol: str) -> list[date]:
        """저장된 구간 안에서 **평일인데 봉이 없는** 날.

        거래일 인덱스 이동(`forward_bar`)이 결측 없음을 전제하므로 확인이 필요하다.
        공휴일도 여기 걸리므로 이 결과가 곧 오류는 아니다 — **급증을 보는 지표**다.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT date FROM prices WHERE symbol = ? ORDER BY date", (symbol,)
            ).fetchall()
        if not rows:
            return []
        have = {date.fromisoformat(r["date"]) for r in rows}
        first, last = min(have), max(have)
        missing, cur = [], first
        while cur <= last:
            if cur.weekday() < 5 and cur not in have:
                missing.append(cur)
            cur += timedelta(days=1)
        return missing
