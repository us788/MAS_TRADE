"""에이전트 레이어 검증. 네트워크를 타지 않는다.

여기서 지킬 핵심 둘.

- **한 종목이 깨져도 나머지가 살아남는 것.** 주 1회 시그널인데 파싱 하나 때문에
  그 주가 통째로 사라지면 표본이 영구 손실이다.
- **전량 로깅.** 실패한 호출도 기록에 남아야 어디서 몇 건이 깨졌는지 셀 수 있다.
"""
import json
import tempfile
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.agents.baseline import BaselineAgent, BaselineResponse, _parse, run_market
from src.agents.context import NewsRef, SymbolContext
from src.agents.prompts import prompt_version, system_prompt
from src.agents.store import SignalStore
from src.llm.client import LLMCall

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2026, 9, 15, 16, 0, tzinfo=KST)

GOOD = {"direction": "buy", "confidence": 0.7, "rationale": "근거",
        "counter_rationale": "반대", "key_evidence": ["beta_120d"]}


_seq = iter(range(1, 10_000))


def _call(finish="stop", reasoning=100, completion=300):
    """호출마다 다른 call_id를 준다 — 실제 클라이언트는 uuid4를 쓴다."""
    return LLMCall(
        call_id=f"c-{next(_seq)}", tier="deep", requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro", thinking=True,
        called_at=datetime.now(timezone.utc), latency_ms=10,
        prompt_tokens=1000, cached_tokens=900, completion_tokens=completion,
        off_peak=True, estimated_cost_usd=0.001, finish_reason=finish,
        reasoning_tokens=reasoning)


class FakeClient:
    """정해진 응답을 순서대로 돌려준다. Exception이면 던진다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text, call = item
        return text, call


def _ctx(symbol="005930", market="KR"):
    return SymbolContext(
        symbol=symbol, name="삼성전자", sector="반도체", market=market, as_of=AS_OF,
        base_date=date(2026, 9, 15), base_price=250500.0,
        indicators={"beta_120d": 1.2, "returns": {"20d": -0.05}},
        news=(NewsRef("abc123", "제목", "요약", "매체", "2026-09-15T07:00:00+00:00", False),),
        universe_version="2026-09-15")


# ---------------------------------------------------------------- 응답 파싱

def test_평범한_json을_읽는다():
    assert _parse(json.dumps(GOOD)).direction.value == "buy"


def test_코드펜스가_붙어도_읽는다():
    assert _parse("```json\n" + json.dumps(GOOD) + "\n```").confidence == 0.7


def test_앞뒤에_설명이_붙어도_읽는다():
    text = "판단 결과입니다.\n" + json.dumps(GOOD) + "\n이상입니다."
    assert _parse(text).direction.value == "buy"


def test_스키마를_벗어나면_거부한다():
    """관대하게 읽되 스키마는 엄격하게."""
    with pytest.raises(Exception):
        _parse(json.dumps({**GOOD, "direction": "강력매수"}))
    with pytest.raises(Exception):
        _parse(json.dumps({**GOOD, "confidence": 1.5}))
    with pytest.raises(Exception):
        _parse(json.dumps({**GOOD, "confidence": "높음"}))


def test_json이_아니면_실패한다():
    with pytest.raises(ValueError):
        _parse("삼성전자는 매수 의견입니다.")


def test_빈_응답도_실패로_다룬다():
    with pytest.raises(ValueError):
        _parse("")


# ------------------------------------------------------------------ 에이전트

def test_정상_응답을_결과로_돌려준다():
    agent = BaselineAgent(FakeClient([(json.dumps(GOOD), _call())]))
    r = agent.analyze(_ctx())
    assert r.ok and r.response.direction.value == "buy" and r.error is None


def test_잘린_응답은_truncated로_구분한다():
    """스키마 오류로 뭉뚱그리면 max_tokens 문제를 영영 못 찾는다."""
    agent = BaselineAgent(FakeClient([("", _call(finish="length", reasoning=8000,
                                                 completion=8000))]))
    r = agent.analyze(_ctx())
    assert not r.ok and r.error.startswith("truncated")
    assert r.call is not None          # 비용은 나갔으므로 기록은 남는다


def test_벤더_예외도_결과로_감싼다():
    agent = BaselineAgent(FakeClient([RuntimeError("502 Bad Gateway")]))
    r = agent.analyze(_ctx())
    assert not r.ok and "RuntimeError" in r.error and r.call is None


def test_고정_프리픽스가_먼저_가고_가변부가_뒤에_온다():
    """순서가 뒤집히면 프리픽스 캐시가 매번 깨진다 (기획서 4.2절)."""
    fake = FakeClient([(json.dumps(GOOD), _call())])
    BaselineAgent(fake).analyze(_ctx())
    messages, kw = fake.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == system_prompt("KR")
    assert messages[-1]["role"] == "user"
    assert "005930" in messages[-1]["content"]
    assert "005930" not in messages[0]["content"]      # 프리픽스에 종목이 없다
    assert kw["thinking"] is True and kw["tier"] == "deep"


def test_프리픽스에_날짜가_들어가지_않는다():
    for market in ("KR", "US"):
        assert "2026" not in system_prompt(market)


def test_프롬프트를_고치면_버전이_바뀐다():
    """프롬프트 튜닝도 파라미터 튜닝이다 (기획서 7절). 경계를 그을 수 있어야 한다."""
    assert prompt_version("KR") != prompt_version("US")
    assert len(prompt_version("KR")) == 12


# -------------------------------------------------------------------- 실행

def test_한_종목이_깨져도_나머지가_살아남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = SignalStore(db_path=Path(tmp) / "s.sqlite3")
        fake = FakeClient([
            (json.dumps(GOOD), _call()),
            ("이건 JSON이 아니다", _call()),
            (json.dumps({**GOOD, "direction": "sell"}), _call()),
        ])
        ctxs = [_ctx("005930"), _ctx("000660"), _ctx("035420")]
        out = run_market(ctxs, market="KR", as_of=AS_OF, store=store,
                         agent=BaselineAgent(fake))
        assert out["attempted"] == 3 and out["succeeded"] == 2
        assert len(out["failures"]) == 1 and "000660" in out["failures"][0]


def test_실패한_호출도_기록에_남는다():
    """비용은 이미 나갔다. 어디서 몇 건이 깨졌는지 셀 수 있어야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SignalStore(db_path=Path(tmp) / "s.sqlite3")
        fake = FakeClient([(json.dumps(GOOD), _call()), ("깨진 응답", _call())])
        run_market([_ctx("005930"), _ctx("000660")], market="KR", as_of=AS_OF,
                   store=store, agent=BaselineAgent(fake))
        c = store.counts()
        assert c["signals"] == 1 and c["llm_calls"] == 2      # 호출은 둘 다 남는다
        assert c["opinions"] == 1


def test_의견과_참조를_따로_남긴다():
    """최종 의견만 남기면 부분집합 재채점을 영영 못 한다 (기획서 9.1절)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SignalStore(db_path=Path(tmp) / "s.sqlite3")
        out = run_market([_ctx()], market="KR", as_of=AS_OF, store=store,
                         agent=BaselineAgent(FakeClient([(json.dumps(GOOD), _call())])))
        with store.connect() as conn:
            op = dict(conn.execute("SELECT * FROM opinions").fetchone())
        assert op["agent"] == "baseline"
        refs = json.loads(op["data_refs"])
        assert "price:005930:2026-09-15" in refs and "news:abc123" in refs
        assert json.loads(op["llm_call_ids"])


def test_프롬프트_원문은_해시로_한_번만_저장된다():
    with tempfile.TemporaryDirectory() as tmp:
        store = SignalStore(db_path=Path(tmp) / "s.sqlite3")
        fake = FakeClient([(json.dumps(GOOD), _call()),
                           (json.dumps(GOOD), _call())])
        run_market([_ctx("005930"), _ctx("000660")], market="KR", as_of=AS_OF,
                   store=store, agent=BaselineAgent(fake))
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM prompt_texts").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM llm_payloads").fetchone()[0] == 2


def test_실행_기록에_프롬프트_버전과_유니버스_버전이_남는다():
    with tempfile.TemporaryDirectory() as tmp:
        store = SignalStore(db_path=Path(tmp) / "s.sqlite3")
        run_market([_ctx()], market="KR", as_of=AS_OF, store=store,
                   universe_version="2026-09-15",
                   agent=BaselineAgent(FakeClient([(json.dumps(GOOD), _call())])))
        with store.connect() as conn:
            r = dict(conn.execute("SELECT * FROM runs").fetchone())
        assert r["prompt_version"] == prompt_version("KR")
        assert r["universe_version"] == "2026-09-15"
        assert r["attempted"] == 1 and r["succeeded"] == 1
