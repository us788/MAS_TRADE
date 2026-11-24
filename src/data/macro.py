"""매크로 어댑터 — ALFRED vintage. 기획서 8절 시점 정합성 규칙의 구현.

**FRED가 아니라 ALFRED다.** 같은 엔드포인트지만 `realtime_start`/`realtime_end`를
주면 "그 날짜에 알려져 있던 값"이 온다. 안 주면 최종 수정치가 오고, 그건 그 시점에
세상에 없던 숫자다.

실측(2026-11-24) — 2026-09-15 vintage와 지금을 비교하면:

    GDPC1   2026-04-01 관측   당시 24,269.613  →  지금 24,408.011   (사후 수정)
    PAYEMS  2026-07-01 관측   당시 158,913     →  지금 158,882
    UNRATE  09-15 시점엔 09월·10월 관측이 **아예 없다**              (미발표)

두 종류의 룩어헤드가 다 막힌다 — 수정된 값과 아직 발표되지 않은 값.

**왜 vintage를 우리가 재구성하지 않는가.** 재무(EDGAR/DART)는 공시 제출일로 직접
조인해야 하지만 매크로는 벤더가 그 일을 대신해준다. 기획서가 매크로에서 가장
어렵다고 한 부분이 API 파라미터 하나로 끝난다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import requests

from src import config

ET = ZoneInfo("America/New_York")

#: 이 시각 이후면 그날 발표분이 전부 나온 것으로 본다.
#:
#: ALFRED의 `realtime`은 **일 단위**인데 우리 as_of는 타임스탬프다. CPI는 08:30 ET,
#: FOMC는 14:00 ET에 나오므로 같은 날이어도 시각에 따라 알 수 있는 것이 다르다.
#: 미국장 마감(16:00) + 30분을 기준으로 삼는다 — `prices.known_trading_date`와 같은 철학.
RELEASE_SETTLED_ET = time(16, 30)

#: 수집 대상 계열.
#:
#: 일별 넷이 **주간 변동**을 만들고, 월별 둘이 **시점 정합성 검증**을 맡는다.
#: 재무·수급과 달리 금리·스프레드는 주 단위로 실제로 움직인다.
SERIES: dict[str, str] = {
    "DGS10": "미 10년 국채 금리",
    "T10Y2Y": "10년-2년 기간 스프레드",
    "BAMLH0A0HYM2": "하이일드 스프레드",
    "VIXCLS": "VIX",
    "UNRATE": "실업률",
    "CPIAUCSL": "CPI",
}


class MacroFetchError(RuntimeError):
    """벤더 조회 실패. 호출부는 예외가 아니라 gap으로 다룬다."""


@dataclass(frozen=True)
class MacroPoint:
    """`as_of` 시점에 알려져 있던 관측값 하나."""
    series_id: str
    date: date
    """관측이 가리키는 기간의 시작일. 발표일이 아니다."""
    value: float | None
    """`.` 는 결측이다. **0으로 바꾸지 않는다** — 모르는 것은 모르는 것이다."""
    vintage: date
    """어느 날짜 기준으로 조회했는가. 재현성의 전제."""


def vintage_date(as_of: datetime) -> date:
    """`as_of`에 대응하는 ALFRED vintage 날짜.

    **보수적으로 끊는다.** ET 기준 16:30 전이면 그날 발표분이 다 나왔다고 볼 수
    없으므로 하루 물린다. 우리 시그널 시각(수 07:00 KST = 화 17:00 ET)은 마감 뒤라
    그대로 통과한다.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of는 타임존이 있어야 합니다. 날짜가 아니라 타임스탬프입니다.")
    local = as_of.astimezone(ET)
    if local.time() >= RELEASE_SETTLED_ET:
        return local.date()
    return local.date() - timedelta(days=1)


class AlfredClient:
    """ALFRED 조회. **vintage 없이 부르는 경로를 두지 않는다.**"""

    def __init__(self, base_url: str | None = None, timeout: int | None = None,
                 session: requests.Session | None = None) -> None:
        self.base_url = (base_url or config.FRED_BASE_URL).rstrip("/")
        self.timeout = timeout or config.FRED_TIMEOUT_SEC
        self.session = session or requests.Session()

    def observations(self, series_id: str, as_of: datetime, *,
                     lookback_days: int = 400) -> list[MacroPoint]:
        """`as_of` 시점에 알려져 있던 관측들.

        `lookback_days`는 **관측 기간**의 범위지 vintage와 무관하다.
        """
        vint = vintage_date(as_of)
        start = vint - timedelta(days=lookback_days)
        params = {
            "series_id": series_id,
            "api_key": config.require("FRED_API_KEY"),
            "file_type": "json",
            "observation_start": start.isoformat(),
            # **이 둘이 전부다.** 빼면 최종 수정치가 온다.
            "realtime_start": vint.isoformat(),
            "realtime_end": vint.isoformat(),
        }
        try:
            r = self.session.get(f"{self.base_url}/series/observations",
                                 params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MacroFetchError(f"{series_id}: {type(exc).__name__}: {exc}") from exc
        if not r.ok:
            raise MacroFetchError(f"{series_id}: HTTP {r.status_code}")
        payload = r.json()
        return [_point(series_id, o, vint) for o in payload.get("observations", [])]

    def latest(self, series_id: str, as_of: datetime) -> MacroPoint | None:
        """`as_of` 시점의 최신 관측. 결측(`.`)은 건너뛴다."""
        for p in reversed(self.observations(series_id, as_of)):
            if p.value is not None:
                return p
        return None

    def snapshot(self, as_of: datetime,
                 series: Iterable[str] | None = None) -> dict[str, MacroPoint | None]:
        """계열별 최신 관측 한 묶음. 거시 에이전트가 보는 입력이다."""
        return {s: self.latest(s, as_of) for s in (series or SERIES)}


def _point(series_id: str, row: dict, vint: date) -> MacroPoint:
    raw = row.get("value")
    return MacroPoint(
        series_id=series_id,
        date=date.fromisoformat(row["date"]),
        value=None if raw in (None, ".", "") else float(raw),
        vintage=vint,
    )


# ---------------------------------------------------------------- 저장

_SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_schema_version (version INTEGER NOT NULL);

-- **vintage가 PK에 들어간다.** 같은 관측이 vintage마다 다른 값을 가지는 것이
-- 정상이고, 그게 이 테이블의 존재 이유다. 덮어쓰면 사후 수정이 과거를 삼킨다.
CREATE TABLE IF NOT EXISTS macro (
    series_id    TEXT NOT NULL,
    date         TEXT NOT NULL,   -- 관측 기간 시작일 (발표일 아님)
    vintage      TEXT NOT NULL,   -- 그 날짜 기준으로 알려져 있던 값
    value        REAL,            -- NULL은 결측. 0이 아니다
    collected_at TEXT NOT NULL,
    PRIMARY KEY (series_id, date, vintage)
);
CREATE INDEX IF NOT EXISTS idx_macro_asof ON macro(series_id, vintage, date);
"""
MACRO_SCHEMA_VERSION = 1


class MacroStore:
    """매크로 저장소. **조회는 항상 vintage 기준이다.**"""

    def __init__(self, db_path=None) -> None:
        import sqlite3
        from pathlib import Path
        self._sqlite3 = sqlite3
        self.db_path = Path(db_path) if db_path else config.DATA_DIR / "mas_trade.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM macro_schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO macro_schema_version (version) VALUES (?)",
                             (MACRO_SCHEMA_VERSION,))

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = self._sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = self._sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return _cm()

    def save(self, points: Iterable[MacroPoint]) -> int:
        """INSERT OR IGNORE. **같은 (계열, 관측일, vintage)는 덮지 않는다.**"""
        rows = [(p.series_id, p.date.isoformat(), p.vintage.isoformat(), p.value,
                 datetime.now(tz=ET).isoformat()) for p in points]
        if not rows:
            return 0
        with self.connect() as conn:
            before = conn.execute("SELECT COUNT(*) AS n FROM macro").fetchone()["n"]
            conn.executemany(
                "INSERT OR IGNORE INTO macro (series_id, date, vintage, value, collected_at)"
                " VALUES (?,?,?,?,?)", rows)
            after = conn.execute("SELECT COUNT(*) AS n FROM macro").fetchone()["n"]
        return after - before

    def snapshot_as_of(self, as_of: datetime,
                       series: Iterable[str] | None = None) -> dict[str, MacroPoint | None]:
        """`as_of` 시점에 알 수 있었던 계열별 최신 관측.

        **vintage가 정확히 일치하지 않아도 된다.** 주 1회 수집이라 그 사이 시점은
        직전 vintage를 쓴다 — 더 오래된 vintage는 정보가 **덜** 들어 있으므로
        보수적이고 룩어헤드가 아니다.
        """
        target = vintage_date(as_of).isoformat()
        out: dict[str, MacroPoint | None] = {}
        with self.connect() as conn:
            for s in (series or SERIES):
                row = conn.execute(
                    "SELECT series_id, date, vintage, value FROM macro "
                    "WHERE series_id = ? AND vintage <= ? AND value IS NOT NULL "
                    "ORDER BY vintage DESC, date DESC LIMIT 1", (s, target)).fetchone()
                out[s] = MacroPoint(
                    row["series_id"], date.fromisoformat(row["date"]),
                    row["value"], date.fromisoformat(row["vintage"])) if row else None
        return out

    def counts(self) -> dict:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM macro").fetchone()["n"]
            by = {r["series_id"]: r["n"] for r in conn.execute(
                "SELECT series_id, COUNT(*) AS n FROM macro GROUP BY series_id")}
            vints = conn.execute(
                "SELECT COUNT(DISTINCT vintage) AS n FROM macro").fetchone()["n"]
        return {"total": total, "by_series": by, "vintages": vints}
