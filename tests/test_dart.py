"""KR 재무 시점 정합성 검증. 네트워크를 타지 않는다.

지키는 것 둘.

- **접수일자 전에는 그 숫자가 존재하지 않는다.** 회계기간이 끝난 시점이 아니라
  공시된 시점부터 시장이 아는 정보다 (기획서 8절).
- **정정공시가 과거를 삼키지 않는다.** `[기재정정]`은 새 접수번호로 다시 오고,
  그 사이 시점에는 원본만 알려져 있었다 — 매크로의 `vintage`와 같은 구조다.
"""
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.data.dart import (DartFetchError, DartStore, Financials, _amount,
                           extract, filed_date)

KST = ZoneInfo("Asia/Seoul")


def _store(tmp):
    return DartStore(db_path=Path(tmp) / "d.sqlite3")


def _row(aid, sj, amount, rcept="20261116001482", nm=""):
    return {"account_id": aid, "sj_div": sj, "thstrm_amount": amount,
            "rcept_no": rcept, "account_nm": nm, "currency": "KRW"}


def _fin(symbol="005930", year=2026, code="11014", rcept="20261116001482", **vals):
    base = {"assets": None, "liabilities": None, "equity": None,
            "revenue": None, "operating_income": None, "net_income": None}
    return Financials(symbol=symbol, corp_code="00126380", bsns_year=year,
                      reprt_code=code, rcept_no=rcept, filed_on=filed_date(rcept),
                      fs_div="CFS", values={**base, **vals})


# ---- 접수일자 ----

def test_접수번호_앞_8자리가_접수일자다():
    assert filed_date("20261116001482") == date(2026, 11, 16)


def test_접수번호가_없으면_거부한다():
    """**접수일자를 모르면 쓸 수 없다.** 시점 정합성이 깨진다."""
    with pytest.raises(DartFetchError):
        extract([_row("ifrs-full_Assets", "BS", "1", rcept="")],
                symbol="A", corp_code="c", bsns_year=2026,
                reprt_code="11014", fs_div="CFS")


# ---- 파싱 ----

def test_계정명이_아니라_표준_태그로_집는다():
    """**이름은 회사마다 다르다** (2026-11-25 실측). 기아는 `당기순이익`인데
    삼성전자는 `분기순이익`이고 손익계산서가 아니라 자본변동표에만 있다.
    이름으로 집으면 조용히 `None`이 된다.
    """
    rows = [_row("ifrs-full_ProfitLoss", "IS", "75136138000000", nm="분기순이익")]
    f = extract(rows, symbol="005930", corp_code="c", bsns_year=2026,
                reprt_code="11014", fs_div="CFS")
    assert f.values["net_income"] == 75136138000000.0


def test_자본변동표는_보지_않는다():
    """같은 계정이 SCE에도 있는데 거기 값은 기간 누계가 아니다."""
    rows = [_row("ifrs-full_Assets", "BS", "100"),
            _row("ifrs-full_ProfitLoss", "SCE", "999")]
    f = extract(rows, symbol="A", corp_code="c", bsns_year=2026,
                reprt_code="11014", fs_div="CFS")
    assert f.values["assets"] == 100.0 and f.values["net_income"] is None


def test_결측은_0이_아니라_None이다():
    """KB금융은 금융업이라 매출액·영업이익이 없다. 0으로 채우면 적자로 읽힌다."""
    assert _amount("-") is None and _amount("") is None and _amount(None) is None
    assert _amount("1,234") == 1234.0


# ---- 시점 정합성 ----

def test_접수_전에는_보이지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_fin(rcept="20261116001482", revenue=195.0)])
        before = s.as_of("005930", datetime(2026, 11, 10, 7, 0, tzinfo=KST))
        after = s.as_of("005930", datetime(2026, 11, 18, 7, 0, tzinfo=KST))
        assert before is None
        assert after["revenue"] == 195.0


def test_그_시점의_최신_분기를_준다():
    """11-10엔 반기, 11-18엔 3분기가 최신이다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_fin(code="11012", rcept="20260814000001", revenue=171.0),
                _fin(code="11014", rcept="20261116001482", revenue=195.0)])
        assert s.as_of("005930", datetime(2026, 11, 10, 7, 0, tzinfo=KST))["revenue"] == 171.0
        assert s.as_of("005930", datetime(2026, 11, 18, 7, 0, tzinfo=KST))["revenue"] == 195.0


def test_정정공시는_원본을_덮지_않는다():
    """**이 테이블의 존재 이유다.** 그 사이 시점에는 원본만 알려져 있었다."""
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_fin(code="11012", rcept="20260814000001", revenue=171.0),
                _fin(code="11012", rcept="20261123000009", revenue=170.5)])  # 기재정정
        assert s.counts()["total"] == 2
        assert s.counts()["amended_periods"] == 1
        mid = s.as_of("005930", datetime(2026, 11, 1, 7, 0, tzinfo=KST))
        late = s.as_of("005930", datetime(2026, 11, 24, 7, 0, tzinfo=KST))
        assert mid["revenue"] == 171.0          # 정정 전
        assert late["revenue"] == 170.5         # 정정 후


def test_정정공시가_과거_분기를_되살리지_않는다():
    """**접수일로만 정렬하면 틀린다.** 11-23에 정정된 반기가 11-16 접수된
    3분기보다 최신이 돼버린다. 회계기간을 먼저 본다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_fin(code="11014", rcept="20261116001482", revenue=195.0),
                _fin(code="11012", rcept="20261123000009", revenue=170.5)])
        got = s.as_of("005930", datetime(2026, 11, 24, 7, 0, tzinfo=KST))
        assert got["reprt_code"] == "11014" and got["revenue"] == 195.0


def test_같은_접수번호는_덮지_않는다():
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        f = _fin(revenue=195.0)
        assert s.save([f]) == 1
        assert s.save([f]) == 0
