"""KR 재무 어댑터 — DART. 기획서 8절 시점 정합성 규칙의 구현.

**접수일자 기준으로만 쓴다.** 회계기간이 끝난 시점이 아니라 **공시된 시점**부터
시장이 아는 정보다. 3월 말 분기 실적은 5월에 나온다.

다행히 재무 응답(`fnlttSinglAcntAll`)이 `rcept_no`를 들고 오고 **앞 8자리가
접수일자**다. 실측(2026-11-25): 삼성전자 2026 3분기보고서가
`rcept_no=20261116001482` → 접수 2026-11-16. **공시검색 API를 따로 조인할 필요가
없다** — ALFRED가 `realtime`으로 해주던 일을 DART는 `rcept_no`로 해준다.

US(EDGAR)는 못 쓴다. IP가 Akamai 엣지에서 막혀 있고
("Request Rate Threshold Exceeded") UA·헤더 문제가 아니다 — sec.gov 홈페이지조차
403이다. 그래서 **당분간 펀더멘털은 KR만 갖는다.** 기획서가 원래 시장별 분기
설계이고 수급도 "KR 비중 ↑"라 비대칭 자체는 설계 밖이 아니다.
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable
from zoneinfo import ZoneInfo

import requests

from src import config

KST = ZoneInfo("Asia/Seoul")

#: 보고서 코드. 분기·반기·사업보고서.
REPORT_CODES = {
    "11013": "1분기보고서",
    "11012": "반기보고서",
    "11014": "3분기보고서",
    "11011": "사업보고서",
}

#: 뽑아 쓸 계정. **계정명이 아니라 표준 태그(`account_id`)로 집는다.**
#:
#: 이름은 회사마다 다르다 — 실측(2026-11-25): 기아는 `당기순이익`인데 삼성전자는
#: `분기순이익`이고 그나마 손익계산서가 아니라 자본변동표에만 있다. 이름으로 집으면
#: **조용히 `None`이 된다.** 표준 태그는 유니버스 15종목 중 14종목에서 6/6 적중했다.
#:
#: KB금융만 `revenue`·`operating_income`이 빠진다 — **금융업이라 그렇다.**
#: 은행은 매출액·영업이익 대신 이자수익·영업수익 체계를 쓴다. 없는 것은
#: `None`으로 두고 채우지 않는다.
WANTED = {
    "ifrs-full_Assets": "assets",
    "ifrs-full_Liabilities": "liabilities",
    "ifrs-full_Equity": "equity",
    "ifrs-full_Revenue": "revenue",
    "ifrs-full_ProfitLoss": "net_income",
    "dart_OperatingIncomeLoss": "operating_income",
}

#: 값을 집을 재무제표. 자본변동표(SCE)·현금흐름표(CF)는 보지 않는다.
STATEMENTS = ("BS", "IS", "CIS")


class DartFetchError(RuntimeError):
    """벤더 조회 실패. 호출부는 예외가 아니라 gap으로 다룬다."""


@dataclass(frozen=True)
class Financials:
    """공시 하나에서 뽑은 재무. **접수일자를 들고 다닌다.**"""
    symbol: str
    corp_code: str
    bsns_year: int
    reprt_code: str
    rcept_no: str
    filed_on: date
    """접수일자. **이 날짜부터 시장이 아는 값이다.**"""
    fs_div: str
    values: dict[str, float | None]
    currency: str = "KRW"

    @property
    def report_name(self) -> str:
        return REPORT_CODES.get(self.reprt_code, self.reprt_code)


def filed_date(rcept_no: str) -> date:
    """접수번호 앞 8자리가 접수일자다."""
    return date(int(rcept_no[0:4]), int(rcept_no[4:6]), int(rcept_no[6:8]))


def _amount(raw: str | None) -> float | None:
    """`-` 와 빈 값은 결측이다. **0으로 바꾸지 않는다.**"""
    if raw in (None, "", "-"):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def extract(rows: list[dict], *, symbol: str, corp_code: str, bsns_year: int,
            reprt_code: str, fs_div: str) -> Financials:
    """응답 행들에서 재무를 뽑는다. **HTTP와 분리돼 있어 테스트가 네트워크를 안 탄다.**"""
    rcept_no = str(rows[0].get("rcept_no") or "")
    if len(rcept_no) < 8:
        # **접수일자를 모르면 쓸 수 없다.** 시점 정합성이 깨진다.
        raise DartFetchError(f"{symbol}: rcept_no가 없습니다")

    values: dict[str, float | None] = {k: None for k in set(WANTED.values())}
    for r in rows:
        if r.get("sj_div") not in STATEMENTS:
            continue
        field = WANTED.get((r.get("account_id") or "").strip())
        if field and values.get(field) is None:
            values[field] = _amount(r.get("thstrm_amount"))

    return Financials(
        symbol=symbol, corp_code=corp_code, bsns_year=bsns_year,
        reprt_code=reprt_code, rcept_no=rcept_no,
        filed_on=filed_date(rcept_no), fs_div=fs_div, values=values,
        currency=(rows[0].get("currency") or "KRW").strip(),
    )


class DartClient:
    """DART 조회. **접수일자 없는 값을 돌려주지 않는다.**"""

    def __init__(self, base_url: str | None = None, timeout: int | None = None,
                 session: requests.Session | None = None) -> None:
        self.base_url = (base_url or config.DART_BASE_URL).rstrip("/")
        self.timeout = timeout or config.DART_TIMEOUT_SEC
        self.session = session or requests.Session()
        self._corp_codes: dict[str, str] | None = None

    # ---- 종목코드 → corp_code ----

    def corp_codes(self) -> dict[str, str]:
        """6자리 종목코드 → DART corp_code. zip 한 번 받아 메모리에 둔다."""
        if self._corp_codes is not None:
            return self._corp_codes
        raw = self._get_bytes("corpCode.xml")
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            root = ET.fromstring(z.read(z.namelist()[0]))
        except (zipfile.BadZipFile, ET.ParseError) as e:
            raise DartFetchError(f"corpCode 파싱 실패: {type(e).__name__}") from e
        out = {}
        for el in root.findall("list"):
            code = (el.findtext("stock_code") or "").strip()
            if code:
                out[code] = (el.findtext("corp_code") or "").strip()
        self._corp_codes = out
        return out

    # ---- 재무 ----

    def financials(self, symbol: str, bsns_year: int, reprt_code: str,
                   fs_div: str = "CFS") -> Financials | None:
        """공시 하나. 없으면 None (status 013 = 조회 결과 없음)."""
        corp = self.corp_codes().get(symbol)
        if not corp:
            raise DartFetchError(f"{symbol}: corp_code를 찾을 수 없습니다")
        payload = self._get_json("fnlttSinglAcntAll.json", {
            "corp_code": corp, "bsns_year": str(bsns_year),
            "reprt_code": reprt_code, "fs_div": fs_div})
        status = payload.get("status")
        if status == "013":
            return None
        if status != "000":
            raise DartFetchError(f"{symbol} {bsns_year}/{reprt_code}: "
                                 f"status {status} {payload.get('message','')}")
        rows = payload.get("list") or []
        if not rows:
            return None

        return extract(rows, symbol=symbol, corp_code=corp, bsns_year=bsns_year,
                       reprt_code=reprt_code, fs_div=fs_div)

    # ---- HTTP ----

    def _get_bytes(self, path: str, params: dict | None = None) -> bytes:
        p = {"crtfc_key": config.require("DART_API_KEY"), **(params or {})}
        try:
            r = self.session.get(f"{self.base_url}/{path}", params=p, timeout=self.timeout)
        except requests.RequestException as e:
            raise DartFetchError(f"{path}: {type(e).__name__}: {e}") from e
        if not r.ok:
            raise DartFetchError(f"{path}: HTTP {r.status_code}")
        return r.content

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        import json
        raw = self._get_bytes(path, params)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise DartFetchError(f"{path}: JSON 아님 ({str(e)[:60]})") from e


# ---------------------------------------------------------------- 저장

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dart_schema_version (version INTEGER NOT NULL);

-- **`rcept_no`가 PK에 들어간다.** 정정공시(`[기재정정]`)는 같은 기간·같은 보고서에
-- 대해 **새 접수번호로** 다시 온다. 덮어쓰면 "그 시점에 알려져 있던 값"이 사라진다 —
-- 매크로의 `vintage`와 같은 구조다.
CREATE TABLE IF NOT EXISTS financials (
    symbol           TEXT NOT NULL,
    bsns_year        INTEGER NOT NULL,
    reprt_code       TEXT NOT NULL,
    fs_div           TEXT NOT NULL,
    rcept_no         TEXT NOT NULL,
    filed_on         TEXT NOT NULL,   -- 접수일자. 이 날부터 시장이 아는 값이다
    assets           REAL,
    liabilities      REAL,
    equity           REAL,
    revenue          REAL,
    operating_income REAL,
    net_income       REAL,
    currency         TEXT NOT NULL DEFAULT 'KRW',
    collected_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, bsns_year, reprt_code, fs_div, rcept_no)
);
CREATE INDEX IF NOT EXISTS idx_fin_asof ON financials(symbol, filed_on);
"""
DART_SCHEMA_VERSION = 1

#: 보고서 코드 → 그 기간의 끝(월). as-of 조회에서 **어느 보고서가 더 최신인지**를
#: 가리는 데 쓴다. 접수일자만 보면 정정공시가 과거 분기를 되살릴 수 있다.
_PERIOD_END_MONTH = {"11013": 3, "11012": 6, "11014": 9, "11011": 12}

_VALUE_FIELDS = ("assets", "liabilities", "equity", "revenue",
                 "operating_income", "net_income")


class DartStore:
    """재무 저장소. **조회는 항상 접수일자 기준이다.**"""

    def __init__(self, db_path=None) -> None:
        import sqlite3
        from pathlib import Path
        self._sqlite3 = sqlite3
        self.db_path = Path(db_path) if db_path else config.DATA_DIR / "mas_trade.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            if conn.execute("SELECT version FROM dart_schema_version").fetchone() is None:
                conn.execute("INSERT INTO dart_schema_version (version) VALUES (?)",
                             (DART_SCHEMA_VERSION,))

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

    def save(self, items: Iterable[Financials]) -> int:
        """INSERT OR IGNORE. **같은 접수번호는 덮지 않는다.**"""
        rows = []
        for f in items:
            rows.append((f.symbol, f.bsns_year, f.reprt_code, f.fs_div, f.rcept_no,
                         f.filed_on.isoformat(),
                         *[f.values.get(k) for k in _VALUE_FIELDS],
                         f.currency, datetime.now(tz=KST).isoformat()))
        if not rows:
            return 0
        cols = ("symbol, bsns_year, reprt_code, fs_div, rcept_no, filed_on, "
                + ", ".join(_VALUE_FIELDS) + ", currency, collected_at")
        marks = ",".join("?" * (6 + len(_VALUE_FIELDS) + 2))
        with self.connect() as conn:
            before = conn.execute("SELECT COUNT(*) AS n FROM financials").fetchone()["n"]
            conn.executemany(
                f"INSERT OR IGNORE INTO financials ({cols}) VALUES ({marks})", rows)
            after = conn.execute("SELECT COUNT(*) AS n FROM financials").fetchone()["n"]
        return after - before

    def as_of(self, symbol: str, as_of: datetime) -> dict | None:
        """`as_of` 시점에 알 수 있었던 **가장 최근 분기**의 재무.

        **접수일자로 먼저 거르고**, 그 안에서 회계기간이 가장 최근인 것을 고른다.
        접수일만 보면 정정공시가 과거 분기를 되살려 더 오래된 숫자를 최신으로
        만들 수 있다.
        """
        cutoff = as_of.astimezone(KST).date().isoformat()
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM financials WHERE symbol = ? AND filed_on <= ?",
                (symbol, cutoff))]
        if not rows:
            return None
        rows.sort(key=lambda r: (r["bsns_year"],
                                 _PERIOD_END_MONTH.get(r["reprt_code"], 0),
                                 r["filed_on"]))
        return rows[-1]

    def counts(self) -> dict:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM financials").fetchone()["n"]
            syms = conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS n FROM financials").fetchone()["n"]
            amended = conn.execute(
                "SELECT COUNT(*) AS n FROM (SELECT symbol, bsns_year, reprt_code, fs_div"
                " FROM financials GROUP BY 1,2,3,4 HAVING COUNT(*) > 1)").fetchone()["n"]
        return {"total": total, "symbols": syms, "amended_periods": amended}
