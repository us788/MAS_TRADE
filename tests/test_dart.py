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


# ---- 프롬프트용 지표: 계산은 코드가 끝낸다 (CLAUDE.md 3절) ----
#
# 실측 기반 픽스처(2026-11-25 DB). 삼성전자 2025/2026, 조 단위를 원으로.
# **분기 금액은 누계가 아니라 당분기 3개월치다** — 2025 Q1+Q2+Q3에 함축 Q4를
# 더하면 사업보고서 연간치와 정확히 맞는다.

def _q(year, code, rcept, rev, op, ni, *, eq=None, li=None, ast=None):
    return _fin(year=year, code=code, rcept=rcept, revenue=rev,
                operating_income=op, net_income=ni,
                equity=eq, liabilities=li, assets=ast)


_T = 1e12   # 조


def _samsung(store):
    """2025 네 분기 + 사업보고서, 2026 세 분기."""
    store.save([
        _q(2025, "11013", "20250515000001", 79.1 * _T, 6.7 * _T, 6.9 * _T),
        _q(2025, "11012", "20250814000001", 74.6 * _T, 4.7 * _T, 5.1 * _T),
        _q(2025, "11014", "20251114000001", 86.1 * _T, 12.2 * _T, 12.2 * _T),
        _q(2025, "11011", "20260310000001", 333.6 * _T, 43.6 * _T, 42.0 * _T),
        _q(2026, "11013", "20260515000001", 133.9 * _T, 57.2 * _T, 40.0 * _T),
        _q(2026, "11012", "20260814000001", 171.5 * _T, 89.5 * _T, 62.0 * _T),
        _q(2026, "11014", "20261116000001", 195.3 * _T, 107.4 * _T, 75.1 * _T,
           eq=614.1 * _T, li=177.7 * _T, ast=791.8 * _T),
    ])


def _at(d):
    return datetime(*d, tzinfo=KST)


def test_최근_12개월은_지나간_분기를_빼고_올해를_더한다():
    """`TTM = Σ올해분기 + 작년연간 − Σ작년분기`.

    분기 하나만 보면 계절성과 일회성 비용에 휘둘린다. 분기 금액이 3개월치라
    이 식이 성립한다 — 누계였다면 이중계상이 된다.
    """
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        _samsung(s)
        f = fundamentals_as_of(s, "005930", _at((2026, 11, 25)))

    expected = (133.9 + 171.5 + 195.3) + 333.6 - (79.1 + 74.6 + 86.1)
    assert f["ttm"]["revenue"] == pytest.approx(expected * _T / 1e8, rel=1e-6)


def test_조각이_하나라도_없으면_TTM을_추정하지_않는다():
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_q(2026, "11014", "20261116000001", 195.3 * _T, 107.4 * _T,
                     75.1 * _T, eq=614.1 * _T)])
        f = fundamentals_as_of(s, "005930", _at((2026, 11, 25)))

    assert f["ttm"]["revenue"] is None          # 작년 사업보고서가 없다
    assert f["ratios_pct"]["operating_margin"] is not None   # 분기 비율은 나온다


def test_전년_동기는_같은_분기끼리만_비교한다():
    """사업보고서는 12개월치라 분기와 섞으면 증감률이 통째로 거짓이 된다."""
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        _samsung(s)
        f = fundamentals_as_of(s, "005930", _at((2026, 11, 25)))

    assert f["period"]["months"] == 3
    assert f["prior_year_same_period"]["revenue"] == pytest.approx(86.1 * _T / 1e8)
    assert f["yoy_growth_pct"]["revenue"] == pytest.approx(126.8, abs=0.1)


def test_접수_전_공시는_지표에도_안_들어온다():
    """3분기보고서 접수일(11-16) 전날에는 반기까지만 알려져 있다."""
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        _samsung(s)
        f = fundamentals_as_of(s, "005930", _at((2026, 11, 15)))

    assert f["period"]["label"] == "반기보고서"
    assert f["latest"]["revenue"] == pytest.approx(171.5 * _T / 1e8)


def test_기준이_적자면_증감률을_주지_않는다():
    """-1,000억에서 +500억이 -150%로 나오면 악화로 읽힌다. 금액을 그대로 준다."""
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([
            _q(2025, "11014", "20251114000001", 10.0 * _T, -1.0 * _T, -1.0 * _T),
            _q(2026, "11014", "20261116000001", 12.0 * _T, 0.5 * _T, 0.5 * _T,
               eq=20.0 * _T),
        ])
        f = fundamentals_as_of(s, "005930", _at((2026, 11, 25)))

    assert f["yoy_growth_pct"]["operating_income"] is None
    assert f["yoy_growth_pct"]["revenue"] == pytest.approx(20.0)
    assert f["latest"]["operating_income"] > 0 > f["prior_year_same_period"]["operating_income"]


def test_금융업_결측은_0으로_채우지_않는다():
    """KB금융은 매출·영업이익이 없다. 은행이라 그렇지 결측이 아니다."""
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([_fin(symbol="105560", rcept="20261116000002",
                       net_income=1.76 * _T, equity=62.5 * _T,
                       liabilities=788.9 * _T, assets=851.4 * _T)])
        f = fundamentals_as_of(s, "105560", _at((2026, 11, 25)))

    assert f["latest"]["revenue"] is None
    assert f["ratios_pct"]["operating_margin"] is None     # 0이 아니다
    assert f["ratios_pct"]["debt_to_equity"] == pytest.approx(1262.24, abs=1.0)


def test_정정공시가_지표의_기준이_된다():
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        s = _store(tmp)
        s.save([
            _q(2026, "11014", "20261116000001", 100.0 * _T, 10.0 * _T, 8.0 * _T,
               eq=50.0 * _T),
            _q(2026, "11014", "20261201000001", 100.0 * _T, 9.0 * _T, 7.0 * _T,
               eq=50.0 * _T),
        ])
        before = fundamentals_as_of(s, "005930", _at((2026, 11, 25)))
        after = fundamentals_as_of(s, "005930", _at((2026, 12, 2)))

    assert before["ratios_pct"]["operating_margin"] == pytest.approx(10.0)
    assert after["ratios_pct"]["operating_margin"] == pytest.approx(9.0)


def test_공시가_없으면_None이다():
    from src.data.dart import fundamentals_as_of
    with tempfile.TemporaryDirectory() as tmp:
        assert fundamentals_as_of(_store(tmp), "005930", _at((2026, 11, 25))) is None
