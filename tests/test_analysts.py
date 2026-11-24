"""분석 에이전트 검증. 네트워크를 타지 않는다.

여기서 지키는 것 둘.

- **각 에이전트는 컨텍스트의 일부만 본다.** 그게 베이스라인과의 유일한 구조적
  차이다. 슬라이스가 넓어지면 "구조가 이겼나 정보가 이겼나"가 분리되지 않는다.
- **베이스라인 프롬프트를 건드리지 않는다.** 해시가 바뀌면 이미 쌓인 286건과
  비교가 끊긴다 (기획서 7절).
"""
import json
from datetime import datetime, timezone

from src.agents.analyst_prompts import analyst_prompt_version, analyst_system_prompt
from src.agents.analysts import (ANALYSTS, MacroAnalyst, NewsAnalyst,
                                 TechnicalAnalyst, prompt_versions)
from src.agents.context import SymbolContext
from src.agents.prompts import prompt_version

AS_OF = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)

_IND = {
    "beta_120d": 1.4,
    "annualized_volatility_60d": 0.83,
    "index_annualized_volatility_60d": 0.49,
    "relative_strength": {"5d": -0.03, "20d": 0.01, "60d": 0.00, "120d": 0.00},
    "ma_gap": {"20d": -0.1, "60d": -0.2, "120d": -0.29},
    "volume_ratio_20d": 1.2,
    "returns": {"5d": -0.08, "20d": -0.09, "60d": -0.23, "120d": -0.37},
    "index_returns": {"5d": -0.05, "20d": -0.10},
    "drawdown_from_1y_peak": -0.2,
    "bars": 270,
}

_MACRO = {"vintage": "2026-11-17",
          "series": {"VIXCLS": {"label": "VIX", "level": 25.46,
                                "as_of_date": "2026-11-17",
                                "change_20d": 8.55, "change_60d": 10.65}}}


def _ctx():
    from src.agents.context import NewsRef
    return SymbolContext(
        symbol="000660", name="SK하이닉스", sector="반도체", market="KR",
        as_of=AS_OF, base_date=AS_OF.date(), base_price=1_100_000.0,
        indicators=_IND,
        news=(NewsRef("h1", "제목", "요약", "매체", AS_OF.isoformat(), False),),
    )


# ---- 슬라이스 ----

def test_거시는_뉴스를_보지_않는다():
    """뉴스는 다른 축의 몫이다. 여기 섞이면 전문화가 무의미해진다."""
    sl = MacroAnalyst().slice(_ctx(), _MACRO)
    blob = json.dumps(sl, ensure_ascii=False)
    assert "news" not in sl
    assert "제목" not in blob and "h1" not in blob


def test_거시는_가격_흐름_지표를_보지_않는다():
    """이동평균·거래량·수익률은 기술적 에이전트의 축이다."""
    sens = MacroAnalyst().slice(_ctx(), _MACRO)["sensitivity"]
    assert set(sens) == {"beta_120d", "annualized_volatility_60d",
                         "index_annualized_volatility_60d", "relative_strength"}


def test_거시가_보는_것은_레짐과_민감도다():
    """레짐만 주면 종목을 구분할 수 없고, 민감도만 주면 레짐을 모른다."""
    sl = MacroAnalyst().slice(_ctx(), _MACRO)
    assert sl["macro"]["series"]["VIXCLS"]["change_20d"] == 8.55
    assert sl["sensitivity"]["beta_120d"] == 1.4
    assert sl["sector"] == "반도체"


def test_슬라이스가_베이스라인보다_훨씬_좁다():
    """같은 종목·같은 시점인데 입력이 작다는 것이 전문화의 정의다."""
    ctx = _ctx()
    slim = json.dumps(MacroAnalyst().slice(ctx, _MACRO), ensure_ascii=False, default=float)
    full = json.dumps(ctx.to_payload(), ensure_ascii=False, default=float)
    assert len(slim) < len(full)


# ---- 프롬프트 ----

def test_베이스라인_프롬프트를_건드리지_않는다():
    """**이 테스트가 깨지면 286건과의 비교가 끊긴 것이다.**"""
    assert prompt_version("KR") == "0e7f98ed40f8"
    assert prompt_version("US") == "c31d1dcc550a"


def test_에이전트_프롬프트는_베이스라인과_다르다():
    from src.agents.prompts import system_prompt
    assert analyst_system_prompt("macro") != system_prompt("KR")


def test_프롬프트가_시장_방향_예측을_금지한다():
    """채점이 지수 대비 초과수익이라 시장 전체 등락은 상쇄된다."""
    p = analyst_system_prompt("macro")
    assert "시장 방향 예측이 아니다" in p
    assert "상쇄된다" in p


def test_프롬프트가_못_보는_것을_추측하지_말라고_한다():
    p = analyst_system_prompt("macro")
    assert "네가 못 보는 것을 추측해 채우지 않는다" in p


def test_모르는_에이전트는_거부한다():
    import pytest
    with pytest.raises(ValueError):
        analyst_system_prompt("없는에이전트")


def test_프롬프트_버전이_기록된다():
    v = prompt_versions("KR")
    assert set(v) == set(ANALYSTS)
    assert v["macro"] == analyst_prompt_version("macro")
    assert all(len(x) == 12 for x in v.values())


def test_뉴스만_시장별로_갈린다():
    """공시 형식·기사 톤·벤더 태깅 관행이 전부 다르다 (CLAUDE.md 3절).
    가격 흐름 지표와 매크로 레짐은 시장과 무관하게 같은 뜻이다.
    """
    assert prompt_versions("KR")["news"] != prompt_versions("US")["news"]
    for agent in ("technical", "macro"):
        assert prompt_versions("KR")[agent] == prompt_versions("US")[agent]


def test_시장별_프롬프트는_market을_요구한다():
    import pytest
    with pytest.raises(ValueError):
        analyst_system_prompt("news")


# ---- 세 에이전트가 축을 나눠 갖는다 ----

def test_기술적은_뉴스도_거시도_보지_않는다():
    sl = TechnicalAnalyst().slice(_ctx(), _MACRO)
    assert "news" not in sl and "macro" not in sl
    assert "VIX" not in json.dumps(sl, ensure_ascii=False)


def test_기술적은_베타를_보지_않는다():
    """`beta_120d`와 지수 변동성은 **거시의 민감도 축**이다."""
    ind = TechnicalAnalyst().slice(_ctx(), _MACRO)["indicators"]
    assert "beta_120d" not in ind
    assert "index_annualized_volatility_60d" not in ind
    assert "relative_strength" in ind          # 이건 기술적의 중심 지표다


def test_뉴스는_흐름_지표를_보지_않는다():
    """이동평균·거래량·낙폭은 기술적의 축이다."""
    sl = NewsAnalyst().slice(_ctx(), _MACRO)
    assert "indicators" not in sl
    assert set(sl["returns"]) == {"5d", "20d"}    # 반영 여부 판단용 최소 창
    assert len(sl["news"]) == 1


def test_겹침은_최소한이고_의도된_것이다():
    """뉴스가 `returns`를 받는 것은 **이미 반영됐는지** 보라는 것이고,
    거시가 `relative_strength`를 받는 것은 레짐 전달 여부를 보라는 것이다.
    """
    news = NewsAnalyst().slice(_ctx(), _MACRO)
    tech = TechnicalAnalyst().slice(_ctx(), _MACRO)["indicators"]
    macro = MacroAnalyst().slice(_ctx(), _MACRO)["sensitivity"]
    assert set(news["returns"]) < set(tech["returns"])        # 진부분집합
    assert "ma_gap" in tech and "ma_gap" not in macro


def test_세_축을_합치면_베이스라인_재료를_덮는다():
    """**같은 정보, 다른 구조**여야 기획서 12절의 비교가 성립한다."""
    ctx = _ctx()
    seen = set()
    for cls in ANALYSTS.values():
        sl = cls().slice(ctx, _MACRO)
        seen |= set(sl.get("indicators", {}))
        seen |= set(sl.get("sensitivity", {}))
        if "news" in sl:
            seen.add("news")
        if "returns" in sl:
            seen.add("returns")
    assert {"news", "returns", "relative_strength", "ma_gap", "beta_120d",
            "volume_ratio_20d"} <= seen


# ---- 펀더멘털: KR만, 그리고 가격을 보지 않는다 ----

_FUND = {
    "unit": "억원",
    "statement": "연결",
    "period": {"label": "3분기보고서", "fiscal_year": 2026, "months": 3,
               "filed_on": "2026-11-16", "days_since_filing": 9},
    "latest": {"revenue": 1952532.1, "operating_income": 1074404.8,
               "net_income": 751361.4, "assets": 7918418.8,
               "liabilities": 1777303.0, "equity": 6141115.8},
    "prior_year_same_period": {"revenue": 860617.5},
    "ratios_pct": {"operating_margin": 55.03, "net_margin": 38.48,
                   "debt_to_equity": 28.94, "equity_to_assets": 77.55},
    "yoy_growth_pct": {"revenue": 126.9},
    "prior_year_margins_pct": {"operating_margin": 14.14},
    "ttm": {"revenue": 5944634.9, "roe_pct": 34.79},
}


def _fund_analyst():
    from src.agents.analysts import FundamentalAnalyst
    return FundamentalAnalyst()


def test_펀더멘털은_뉴스도_가격_흐름도_거시도_보지_않는다():
    sl = _fund_analyst().slice(_ctx(), _MACRO, fundamentals=_FUND)
    assert set(sl) == {"symbol", "name", "sector", "market", "as_of", "financials"}
    blob = json.dumps(sl, ensure_ascii=False)
    assert "VIX" not in blob and "ma_gap" not in blob and "제목" not in blob


def test_펀더멘털에_주가를_주지_않는다():
    """주식 수가 없어 PER·PBR이 안 나온다. 기준가만 주면 LLM이 시가총액을
    **기억에서** 끌어와 채운다 — 시점 정합성 위반이고 검증도 불가능하다.
    """
    sl = _fund_analyst().slice(_ctx(), _MACRO, fundamentals=_FUND)
    assert "reference" not in sl and "base_price" not in sl
    assert "1100000" not in json.dumps(sl)


def test_재무가_없으면_빈_채로_간다():
    """US처럼 어댑터가 없는 경우. 없는 값을 지어내지 않는다."""
    assert _fund_analyst().slice(_ctx(), _MACRO)["financials"] == {}


def test_펀더멘털은_KR에서만_돈다():
    """EDGAR가 IP 차단으로 막혀 US 재무를 못 받는다."""
    from src.agents.analysts import analysts_for
    assert "fundamental" in analysts_for("KR")
    assert "fundamental" not in analysts_for("US")
    assert set(analysts_for("US")) == {"technical", "news", "macro"}


def test_시장마다_프롬프트_버전_키가_다르다():
    """**비대칭을 고정한 결정**(2026-11-25). 두 시장 성적은 원래 같은 기준으로
    비교하지 않으므로(CLAUDE.md 4절) 관점 수가 달라도 실험은 성립한다 —
    실험은 시장 **안에서** 멀티 에이전트 대 베이스라인이다.
    """
    assert "fundamental" in prompt_versions("KR")
    assert "fundamental" not in prompt_versions("US")


def test_펀더멘털_프롬프트는_US를_거부한다():
    import pytest
    with pytest.raises(ValueError):
        analyst_system_prompt("fundamental", "US")


def test_펀더멘털_프롬프트가_금융업과_밸류에이션_한계를_말한다():
    """KB금융은 매출·영업이익이 null이고 부채비율이 1,200%를 넘는다.
    프롬프트가 이걸 말해주지 않으면 에이전트가 부실로 읽는다.
    """
    p = analyst_system_prompt("fundamental", "KR")
    assert "금융업" in p and "예금이 부채로" in p
    assert "시가총액을 기억에서 끌어오는 것은 금지" in p


def test_베이스라인_프롬프트는_그대로다():
    """에이전트를 붙여도 이미 쌓인 시그널과의 비교가 끊기면 안 된다."""
    assert prompt_version("KR") == "0e7f98ed40f8"
    assert prompt_version("US") == "c31d1dcc550a"
