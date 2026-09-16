"""as-of 시점 지표 계산.

**LLM에 넘기는 모든 수치는 여기서 나온다** (`CLAUDE.md` 3절). 프롬프트 안에서
산술을 시키지 않는다 — 조용히 틀린 숫자가 근거로 로그에 남아 사후 검증까지 오염시킨다.

실례가 있다. 2026-09-16에 `deepseek-v4-pro`(non-thinking)에게 `17 × 23`을 물으니
**491**이라고 답했다(정답 391). 두 자리 곱셈이 이렇다.

입력은 `PriceStore`가 준 as-of 봉 목록(오래된 것부터)이다. **미래 봉이 섞여 들어오면
여기서 막을 방법이 없다** — 그건 호출부(`src/agents/context.py`)의 책임이고,
이 모듈은 받은 것만 계산한다.

기준은 `close_px`다. 채점기와 같은 계열이어야 "지표가 말한 것"과 "채점된 것"이
같은 세계에 있다 (`docs/harness.md` 3.4.1).
"""
from __future__ import annotations

import math
from typing import Sequence

from src.compute import metrics
from src.data.prices import Bar

TRADING_DAYS_PER_YEAR = 252

# 기본 관측 창. 5일(1주) · 20일(1개월) · 60일(3개월) · 120일(6개월).
RETURN_WINDOWS = (5, 20, 60, 120)
VOL_WINDOW = 60
BETA_WINDOW = 120


def _closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close_px for b in bars]


def _daily_returns(closes: Sequence[float]) -> list[float]:
    out = []
    for prev, cur in zip(closes, closes[1:]):
        r = metrics.pct_return(prev, cur)
        if r is not None:
            out.append(r)
    return out


def window_return(bars: Sequence[Bar], days: int) -> float | None:
    """N거래일 수익률. 봉이 모자라면 None — 짧은 구간으로 대신 계산하지 않는다."""
    if len(bars) < days + 1:
        return None
    return metrics.pct_return(bars[-(days + 1)].close_px, bars[-1].close_px)


def annualized_volatility(bars: Sequence[Bar], days: int = VOL_WINDOW) -> float | None:
    if len(bars) < days + 1:
        return None
    sd = metrics.stdev(_daily_returns(_closes(bars[-(days + 1):])))
    return None if sd is None else sd * math.sqrt(TRADING_DAYS_PER_YEAR)


def moving_average_gap(bars: Sequence[Bar], days: int) -> float | None:
    """이격도. 현재가가 N일 이동평균에서 얼마나 떨어져 있는가."""
    if len(bars) < days:
        return None
    window = _closes(bars[-days:])
    ma = sum(window) / len(window)
    return metrics.pct_return(ma, bars[-1].close_px)


def drawdown_from_peak(bars: Sequence[Bar], days: int = 252) -> float | None:
    """최근 구간 고점 대비 현재 낙폭. 음수다."""
    if len(bars) < 2:
        return None
    window = _closes(bars[-days:])
    peak = max(window)
    return metrics.pct_return(peak, window[-1])


def _aligned(bars: Sequence[Bar], index_bars: Sequence[Bar], days: int
             ) -> tuple[list[float], list[float]] | None:
    """같은 날짜의 일간 수익률 쌍만 남긴다.

    **날짜로 맞춘다.** 길이만 맞춰 자르면 한쪽에 결측이 있을 때 서로 다른 날의
    수익률이 짝지어져 베타가 조용히 틀어진다.
    """
    idx = {b.date: b.close_px for b in index_bars}
    pairs = [(b.date, b.close_px, idx[b.date]) for b in bars if b.date in idx]
    if len(pairs) < days + 1:
        return None
    pairs = pairs[-(days + 1):]
    sym = _daily_returns([p[1] for p in pairs])
    mkt = _daily_returns([p[2] for p in pairs])
    return (sym, mkt) if len(sym) == len(mkt) and len(sym) >= 2 else None


def beta(bars: Sequence[Bar], index_bars: Sequence[Bar],
         days: int = BETA_WINDOW) -> float | None:
    """시장 베타. 공분산 / 시장분산."""
    pair = _aligned(bars, index_bars, days)
    if pair is None:
        return None
    sym, mkt = pair
    n = len(sym)
    ms, mm = sum(sym) / n, sum(mkt) / n
    cov = sum((a - ms) * (b - mm) for a, b in zip(sym, mkt)) / (n - 1)
    var = sum((b - mm) ** 2 for b in mkt) / (n - 1)
    return None if var == 0 else cov / var


def relative_strength(bars: Sequence[Bar], index_bars: Sequence[Bar],
                      days: int) -> float | None:
    """지수 대비 초과수익. 채점기의 `excess`와 같은 정의다."""
    return metrics.excess_return(window_return(bars, days),
                                 window_return(index_bars, days))


def volume_ratio(bars: Sequence[Bar], days: int = 20) -> float | None:
    """최근 거래량 / N일 평균. 거래량이 없는 시계열(지수 등)이면 None."""
    vols = [b.volume for b in bars[-days:] if b.volume]
    if len(vols) < days // 2 or not bars[-1].volume:
        return None
    avg = sum(vols) / len(vols)
    return None if avg == 0 else bars[-1].volume / avg


def compute_all(bars: Sequence[Bar], index_bars: Sequence[Bar]) -> dict:
    """프롬프트에 넣을 지표 묶음. **값이 없으면 None으로 남긴다.**

    모르는 것을 0이나 근사치로 채우면 LLM이 그걸 사실로 읽는다. 상장 초기라
    120일이 안 되는 종목은 그 칸이 비어 있어야 맞다.
    """
    return {
        "bars": len(bars),
        "returns": {f"{d}d": window_return(bars, d) for d in RETURN_WINDOWS},
        "relative_strength": {f"{d}d": relative_strength(bars, index_bars, d)
                              for d in RETURN_WINDOWS},
        "annualized_volatility_60d": annualized_volatility(bars),
        "ma_gap": {f"{d}d": moving_average_gap(bars, d) for d in (20, 60, 120)},
        "drawdown_from_1y_peak": drawdown_from_peak(bars),
        "beta_120d": beta(bars, index_bars),
        "volume_ratio_20d": volume_ratio(bars),
        "index_returns": {f"{d}d": window_return(index_bars, d)
                          for d in RETURN_WINDOWS},
        "index_annualized_volatility_60d": annualized_volatility(index_bars),
    }
