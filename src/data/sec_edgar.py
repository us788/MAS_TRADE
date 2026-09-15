"""SEC EDGAR 어댑터 — US 공시. 기획서 8절 시점 정합성 규칙의 구현.

핵심은 하나다. XBRL 데이터 포인트마다 붙는 `filed`(제출일)로 as_of 시점에
알 수 없었던 값을 잘라낸다. **회계기간 종료일(`end`)로 자르면 룩어헤드다.**
3월 말 분기 실적은 5월에 공시된다.

정정 공시는 같은 회계기간에 `filed`가 다른 항목으로 쌓인다. 따라서
"`filed <= as_of` 중 가장 나중에 제출된 것"이 그 시점의 vintage가 된다.
ALFRED vintage와 같은 구조여서, 재무는 별도 vintage 소스가 필요 없다.

키는 없다. User-Agent가 신원 표시이고 없으면 403이다. 속도 제한은 초당 10건.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from src import config
from src.data._http import RateLimiter

# 같은 개념이라도 회사마다 태그가 다르다. 폴백을 두지 않으면 종목 절반이
# 조용히 None으로 빠진다. 앞에서부터 찾아 처음 걸리는 태그를 쓴다.
CONCEPT_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
    ),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "operating_income": (
        "OperatingIncomeLoss",
    ),
    "cash_flow_operating": (
        "NetCashProvidedByUsedInOperatingActivities",
    ),
    "shares_outstanding": (
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ),
}


@dataclass(frozen=True)
class Fact:
    """as_of 시점에 알려져 있던 재무 수치 하나."""

    concept: str
    tag: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    filed: date
    form: str
    accession: str


@dataclass(frozen=True)
class Filing:
    """공시 1건. 날짜가 아니라 타임스탬프를 들고 있다."""

    accession: str
    form: str
    filing_date: date
    accepted_at: datetime | None
    """ET 17:30 이후 접수분은 다음 영업일에 공시된다. 날짜만 쓰면 이 구분이 사라진다."""
    report_date: date | None
    primary_document: str
    items: str

    @property
    def url(self) -> str:
        return self.primary_document


class EdgarClient:
    """EDGAR 조회. 원본 응답을 스냅샷으로 남긴다 (재현성의 전제)."""

    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: Path | None = None,
        max_rps: float | None = None,
    ) -> None:
        self._user_agent = user_agent or config.require("SEC_USER_AGENT")
        self._cache_dir = cache_dir or (config.SNAPSHOT_DIR / "edgar")
        self._limiter = RateLimiter(max_rps if max_rps is not None else config.SEC_MAX_RPS)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": self._user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._cik_map: dict[str, int] | None = None

    # ---- 저수준 ----

    def _get_json(self, url: str, cache_key: str | None = None, refresh: bool = False) -> Any:
        path = self._cache_dir / f"{cache_key}.json" if cache_key else None
        if path is not None and path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))

        payload = self._fetch_json(url)

        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    def _fetch_json(self, url: str, attempts: int = 4) -> Any:
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._limiter.wait()
            try:
                response = self._session.get(url, timeout=config.SEC_TIMEOUT_SEC)
            except requests.RequestException as exc:  # 네트워크 단절 등
                last_error = exc
                time.sleep(2**attempt)
                continue

            if response.status_code == 200:
                return response.json()
            if response.status_code == 403:
                raise RuntimeError(
                    "EDGAR가 403을 반환했습니다. SEC_USER_AGENT에 이름과 연락처 이메일이 "
                    "들어 있는지 확인하세요 (예: 'MAS_TRADE you@example.com')."
                )
            if response.status_code == 404:
                raise FileNotFoundError(url)
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(2**attempt)
                last_error = RuntimeError(f"HTTP {response.status_code} from {url}")
                continue
            response.raise_for_status()

        raise RuntimeError(f"EDGAR 조회 실패: {url}") from last_error

    # ---- 종목 식별 ----

    def cik_for_ticker(self, ticker: str, refresh: bool = False) -> int:
        """티커 → CIK.

        주의: 이 매핑은 **현재 스냅샷**이다. 티커는 재사용·변경되므로 과거 구간에서는
        잘못 연결될 수 있다. 종목 마스터에 CIK를 박아두고 CIK로 조회하는 것이 안전하다.
        """
        if self._cik_map is None or refresh:
            payload = self._get_json(
                f"{config.SEC_WWW_BASE_URL}/files/company_tickers.json",
                cache_key="company_tickers",
                refresh=refresh,
            )
            self._cik_map = {
                str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()
            }
        try:
            return self._cik_map[ticker.upper()]
        except KeyError:
            raise KeyError(f"EDGAR에서 티커 {ticker!r}의 CIK를 찾지 못했습니다.") from None

    # ---- 공시 목록 ----

    def get_filings(
        self,
        cik: int,
        as_of: datetime,
        forms: Iterable[str] | None = None,
        lookback_days: int | None = None,
    ) -> list[Filing]:
        """as_of **이전에 접수된** 공시만 돌려준다.

        접수 타임스탬프(`acceptanceDateTime`)가 있으면 그것으로, 없으면 제출일로 자른다.
        """
        payload = self._get_json(
            f"{config.SEC_DATA_BASE_URL}/submissions/CIK{cik:010d}.json",
            cache_key=f"submissions/CIK{cik:010d}",
            refresh=True,
        )
        filings = [_row_to_filing(row) for row in _iter_recent(payload)]

        as_of_utc = _as_utc(as_of)
        wanted = {f.upper() for f in forms} if forms else None
        result = []
        for filing in filings:
            if not _is_known_by(filing, as_of_utc):
                continue
            if wanted and filing.form.upper() not in wanted:
                continue
            if lookback_days is not None:
                age = (as_of_utc.date() - filing.filing_date).days
                if age > lookback_days:
                    continue
            result.append(filing)
        result.sort(key=lambda f: (f.accepted_at or _midnight(f.filing_date)), reverse=True)
        return result

    # ---- 재무 수치 ----

    def get_fact_as_of(
        self,
        cik: int,
        concept: str,
        as_of: datetime,
        unit: str = "USD",
    ) -> Fact | None:
        """as_of 시점에 알려져 있던 최신 값. 없으면 None.

        태그 폴백을 앞에서부터 시도해 처음 값이 나오는 태그를 쓴다.
        """
        tags = CONCEPT_TAGS.get(concept, (concept,))
        for tag in tags:
            try:
                payload = self._get_json(
                    f"{config.SEC_DATA_BASE_URL}/api/xbrl/companyconcept"
                    f"/CIK{cik:010d}/us-gaap/{tag}.json",
                    cache_key=f"companyconcept/CIK{cik:010d}/{tag}",
                )
            except FileNotFoundError:
                continue  # 이 회사는 그 태그를 쓰지 않는다

            fact = pick_fact_as_of(payload, concept=concept, tag=tag, as_of=as_of, unit=unit)
            if fact is not None:
                return fact
        return None


# ---- 순수 함수 (네트워크 없이 테스트 가능) ----


def pick_fact_as_of(
    payload: dict[str, Any],
    *,
    concept: str,
    tag: str,
    as_of: datetime,
    unit: str = "USD",
) -> Fact | None:
    """companyconcept 응답에서 as_of 시점의 값을 고른다.

    규칙 두 가지:
    1. `filed <= as_of` 인 항목만 남긴다 — 그 시점에 공개돼 있던 것만.
    2. 남은 것 중 **가장 최근 회계기간**을, 같은 기간이 여럿이면 **가장 나중에 제출된 것**을
       고른다. 정정 공시가 반영된 그 시점의 vintage가 이것이다.
    """
    rows = payload.get("units", {}).get(unit, [])
    cutoff = _as_utc(as_of).date()

    known = [row for row in rows if _parse_date(row.get("filed")) and _parse_date(row["filed"]) <= cutoff]
    if not known:
        return None

    best = max(known, key=lambda r: (r["end"], r["filed"]))
    return Fact(
        concept=concept,
        tag=tag,
        unit=unit,
        value=float(best["val"]),
        period_start=_parse_date(best.get("start")),
        period_end=_parse_date(best["end"]),
        filed=_parse_date(best["filed"]),
        form=best.get("form", ""),
        accession=best.get("accn", ""),
    )


def _iter_recent(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """submissions의 columnar 구조를 행으로 편다."""
    recent = payload.get("filings", {}).get("recent", {})
    if not recent:
        return []
    keys = list(recent)
    length = len(recent[keys[0]])
    return [{key: recent[key][i] for key in keys} for i in range(length)]


def _row_to_filing(row: dict[str, Any]) -> Filing:
    return Filing(
        accession=row.get("accessionNumber", ""),
        form=row.get("form", ""),
        filing_date=_parse_date(row.get("filingDate")),
        accepted_at=_parse_datetime(row.get("acceptanceDateTime")),
        report_date=_parse_date(row.get("reportDate")),
        primary_document=row.get("primaryDocument", ""),
        items=row.get("items", ""),
    )


def _is_known_by(filing: Filing, as_of_utc: datetime) -> bool:
    if filing.accepted_at is not None:
        return filing.accepted_at <= as_of_utc
    # 접수 시각이 없으면 보수적으로 제출일 자정 기준
    return _midnight(filing.filing_date) <= as_of_utc


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
