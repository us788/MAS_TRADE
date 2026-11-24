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
from src.agents.analysts import ANALYSTS, MacroAnalyst, prompt_versions
from src.agents.context import SymbolContext
from src.agents.prompts import prompt_version

AS_OF = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)

_IND = {
    "beta_120d": 1.4,
    "annualized_volatility_60d": 0.83,
    "index_annualized_volatility_60d": 0.49,
    "relative_strength": {"20d": 0.01},
    "ma_gap": {"20d": -0.1},
    "volume_ratio_20d": 1.2,
    "returns": {"20d": -0.09},
    "drawdown_from_1y_peak": -0.2,
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
    v = prompt_versions()
    assert set(v) == set(ANALYSTS)
    assert v["macro"] == analyst_prompt_version("macro")
    assert len(v["macro"]) == 12
