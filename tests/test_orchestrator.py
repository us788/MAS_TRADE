"""멀티 에이전트 회차 검증. **네트워크도 돈도 쓰지 않는다.**

여기서 지키는 것 넷.

- **베이스라인과 섞이지 않는다.** `kind`가 트랙을 가른다. 섞이면 기획서 12절의
  "구조가 단일 호출을 이기는가"를 물을 수 없다.
- **의견이 개별로 남는다.** 그래야 사후 ablation이 LLM 호출 0회다.
- **게이트는 시장 전체를 보고 판정한다.** 섹터 집중은 종목 하나만 봐서는 못 센다.
- **일부가 깨져도 종목을 버리지 않는다.** 버리면 채점 분모가 회차마다 달라진다.
"""
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.agents.context import NewsRef, SymbolContext
from src.agents.orchestrator import (AGENTS_KIND, prompt_version_label,
                                     run_market_agents)
from src.agents.store import SignalStore
from src.llm.client import LLMCall

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2026, 11, 18, 7, 0, tzinfo=KST)

_seq = iter(range(1, 100_000))


def _call(cost=0.001):
    return LLMCall(
        call_id=f"c-{next(_seq)}", tier="deep", requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro", thinking=True,
        called_at=datetime.now(KST), latency_ms=10, prompt_tokens=1000,
        cached_tokens=900, completion_tokens=300, off_peak=True,
        estimated_cost_usd=cost, finish_reason="stop", reasoning_tokens=100)


def _body(direction="buy", conf=0.7):
    return json.dumps({"direction": direction, "confidence": conf,
                       "rationale": "근거", "counter_rationale": "반대",
                       "key_evidence": ["revenue"]}, ensure_ascii=False)


class ScriptedClient:
    """에이전트 이름이 아니라 **호출 순서**로 답한다.

    `plan`은 (방향, 확신도) 또는 Exception의 열이다. 소진되면 마지막 것을 반복한다 —
    종목이 여럿일 때 매번 다 적지 않아도 되게.
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.n = 0

    def complete(self, messages, **kw):
        item = self.plan[self.n] if self.n < len(self.plan) else self.plan[-1]
        self.n += 1
        if isinstance(item, Exception):
            raise item
        direction, conf = item
        return _body(direction, conf), _call()


def _ctx(symbol, name, sector, market="KR", vol=0.5, ivol=0.4):
    return SymbolContext(
        symbol=symbol, name=name, sector=sector, market=market, as_of=AS_OF,
        base_date=date(2026, 11, 17), base_price=70000.0,
        indicators={"annualized_volatility_60d": vol,
                    "index_annualized_volatility_60d": ivol,
                    "returns": {"5d": 0.01, "20d": 0.02},
                    "relative_strength": {"20d": 0.01}},
        news=(NewsRef("h1", "제목", "요약", "매체", AS_OF.isoformat(), False),),
        universe_version="2026-09-15")


def _store(tmp):
    return SignalStore(db_path=Path(tmp) / "s.sqlite3")


def _run(tmp, contexts, plan, *, market="KR", **kw):
    return run_market_agents(contexts, market=market, as_of=AS_OF,
                             store=_store(tmp) if isinstance(tmp, str) else tmp,
                             client=ScriptedClient(plan), **kw)


# ---- 트랙 분리 ----

def test_베이스라인_트랙에_섞이지_않는다():
    """읽는 쪽이 기본으로 `baseline`만 본다. 섞이면 같은 시점이 두 번 세어진다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _run(store, [_ctx("005930", "삼성전자", "반도체")], [("buy", 0.7)])
        assert store.all_signals() == []                       # 기본 = baseline
        assert len(store.all_signals(kind=AGENTS_KIND)) == 1


def test_에이전트_수가_시장마다_다르다():
    """KR 넷, US 셋. EDGAR가 막혀 US는 재무가 없다."""
    with tempfile.TemporaryDirectory() as tmp:
        kr = _run(_store(tmp), [_ctx("005930", "삼성전자", "반도체")], [("buy", 0.7)])
        assert kr["agents"] == ["fundamental", "macro", "news", "technical"]
    with tempfile.TemporaryDirectory() as tmp:
        us = _run(_store(tmp), [_ctx("NVDA", "NVIDIA", "Technology", market="US")],
                  [("buy", 0.7)], market="US")
        assert us["agents"] == ["macro", "news", "technical"]


def test_프롬프트_버전이_에이전트별로_남는다():
    """`runs.prompt_version`이 칼럼 하나라 한 줄로 묶는다. 하나만 바뀌어도 달라진다."""
    label = prompt_version_label("KR")
    assert label.count("=") == 4 and "fundamental=" in label
    assert "fundamental=" not in prompt_version_label("US")


# ---- 전량 로깅과 무료 ablation ----

def test_의견이_에이전트별로_남는다():
    """**이게 있어야 사후 재채점이 LLM 호출 0회다.**"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        out = _run(store, [_ctx("005930", "삼성전자", "반도체")],
                   [("buy", 0.8), ("buy", 0.6), ("sell", 0.5), ("hold", 0.4)])
        sid = out["outcomes"][0].signal_id
        with store.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT agent, direction, confidence FROM opinions WHERE signal_id = ?",
                (sid,))]
    assert len(rows) == 4
    assert {r["agent"] for r in rows} == {"technical", "news", "macro", "fundamental"}
    assert sorted(r["direction"] for r in rows) == ["buy", "buy", "hold", "sell"]


def test_실패한_호출도_기록에_남는다():
    """비용은 이미 나갔다. 어디서 몇 건이 깨졌는지 셀 수 있어야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        out = _run(store, [_ctx("005930", "삼성전자", "반도체")],
                   [("buy", 0.7), ("buy", 0.7), ("buy", 0.7), RuntimeError("벤더 오류")])
        with store.connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"]
    assert n == 3                       # 예외는 call이 없다 — 응답을 못 받았다
    assert any("벤더 오류" in f for f in out["failures"])


# ---- 일부 실패 ----

def test_일부가_깨져도_종목을_버리지_않는다():
    """버리면 그 종목이 표본에서 사라져 **채점 분모가 회차마다 달라진다.**
    누가 빠졌는지는 `opinions`에 남으므로 사후에 걸러낼 수 있다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        out = _run(store, [_ctx("005930", "삼성전자", "반도체")],
                   [("buy", 0.8), ("buy", 0.6), RuntimeError("타임아웃"),
                    RuntimeError("타임아웃")])
    o = out["outcomes"][0]
    assert out["succeeded"] == 1 and o.signal_id is not None
    assert len(o.opinions) == 2 and len(o.failures) == 2


def test_전원_실패하면_시그널이_없다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        out = _run(store, [_ctx("005930", "삼성전자", "반도체")],
                   [RuntimeError("죽었다")])
    assert out["succeeded"] == 0
    assert out["outcomes"][0].agg is None and out["outcomes"][0].signal_id is None


# ---- 게이트는 시장 전체를 본다 ----

def test_섹터_상한은_종목_하나만_봐서는_못_센다():
    """같은 섹터 매수 4건. 상한이 3이라 **확신도가 가장 낮은 하나가 기각된다.**
    1단계를 전 종목 돌린 뒤에야 판정할 수 있다는 것이 이 테스트의 요지다.
    """
    confs = {"A": 0.9, "B": 0.8, "C": 0.7, "D": 0.4}

    class PerSymbol:
        """종목 순서대로 네 번씩 같은 확신도를 준다."""
        def __init__(self):
            self.i = 0

        def complete(self, messages, **kw):
            conf = list(confs.values())[self.i // 4]
            self.i += 1
            return _body("buy", conf), _call()

    ctxs = [_ctx(s, s, "반도체") for s in confs]
    with tempfile.TemporaryDirectory() as tmp:
        out = run_market_agents(ctxs, market="KR", as_of=AS_OF, store=_store(tmp),
                                client=PerSymbol())
    by = {o.context.symbol: o for o in out["outcomes"]}
    assert by["D"].verdict.approved is False
    assert "sector_concentration" in by["D"].verdict.rule_ids
    for s in ("A", "B", "C"):
        assert by[s].verdict.approved is True


def test_기각은_시그널을_지우지_않고_hold로_바꾼다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        # 변동성이 지수의 3배 — 한도 2.0배를 넘는다.
        out = _run(store, [_ctx("005930", "삼성전자", "반도체", vol=1.2, ivol=0.4)],
                   [("buy", 0.8)])
        rows = store.all_signals(kind=AGENTS_KIND)
    o = out["outcomes"][0]
    assert o.agg.direction.value == "buy"          # 집계는 매수였다
    assert rows[0]["direction"] == "hold"          # 게이트가 바꿨다
    assert rows[0]["risk_approved"] == 0
    assert "변동성" in json.loads(rows[0]["risk_reasons"])[0]


def test_기각_사유가_전부_기록된다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _run(store, [_ctx("005930", "삼성전자", "반도체", vol=1.2, ivol=0.4)],
             [("buy", 0.9), ("sell", 0.9), ("buy", 0.2), ("sell", 0.2)])
        rows = store.all_signals(kind=AGENTS_KIND)
    reasons = json.loads(rows[0]["risk_reasons"])
    assert len(reasons) == 2                       # 분산 + 변동성 둘 다
    assert any("분산" in r for r in reasons) and any("변동성" in r for r in reasons)


# ---- 시그널 내용 ----

def test_분산이_시그널에_남는다():
    """기획서가 '그 자체가 피처다'라고 한 값이다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _run(store, [_ctx("005930", "삼성전자", "반도체")],
             [("buy", 0.7), ("buy", 0.7), ("buy", 0.7), ("buy", 0.7)])
        rows = store.all_signals(kind=AGENTS_KIND)
    assert rows[0]["dispersion"] == 0.0            # 전원일치


def test_비용은_에이전트_넷을_합친_것이다():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _run(store, [_ctx("005930", "삼성전자", "반도체")], [("buy", 0.7)])
        rows = store.all_signals(kind=AGENTS_KIND)
    assert rows[0]["estimated_cost_usd"] == 0.004  # 0.001 × 4


def test_기준가와_시점은_베이스라인과_같은_것을_쓴다():
    """**같은 시점·같은 기준가**라야 구조만 다른 두 트랙이 비교된다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _run(store, [_ctx("005930", "삼성전자", "반도체")], [("buy", 0.7)])
        rows = store.all_signals(kind=AGENTS_KIND)
    assert rows[0]["as_of"] == AS_OF.isoformat()
    assert rows[0]["reference_date"] == "2026-11-17"
    assert rows[0]["reference_price"] == 70000.0


def test_재무는_KR_종목에만_붙는다():
    """US는 EDGAR가 막혀 재무가 없다. 빈 채로 가고 지어내지 않는다."""
    seen = []

    class Spy:
        def complete(self, messages, **kw):
            seen.append(messages[-1]["content"])
            return _body(), _call()

    with tempfile.TemporaryDirectory() as tmp:
        run_market_agents([_ctx("NVDA", "NVIDIA", "Technology", market="US")],
                          market="US", as_of=AS_OF, store=_store(tmp), client=Spy(),
                          fundamentals={"NVDA": {"latest": {"revenue": 1.0}}})
    assert not any("financials" in s for s in seen)      # US에 펀더멘털 에이전트가 없다
