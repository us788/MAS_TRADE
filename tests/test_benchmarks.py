"""벤치마크와 저울 검정. 네트워크를 타지 않는다.

**시드가 재현되지 않으면 벤치마크는 근거가 못 된다** (기획서 9.4절). 유리한 시드를
고른 것과 구분할 방법이 없기 때문이다.
"""
from datetime import date, datetime

from src.eval.benchmarks import (SIGNAL_HOUR, calibration_report, random_signals,
                                 signal_dates)

START, END = date(2026, 1, 1), date(2026, 2, 28)


def test_같은_시드는_같은_시그널을_준다():
    a = random_signals("KR", START, END, seed=42)
    b = random_signals("KR", START, END, seed=42)
    assert [(s.symbol, s.as_of, s.direction, s.confidence) for s in a] == \
           [(s.symbol, s.as_of, s.direction, s.confidence) for s in b]


def test_다른_시드는_다른_시그널을_준다():
    a = random_signals("KR", START, END, seed=1)
    b = random_signals("KR", START, END, seed=2)
    assert [s.direction for s in a] != [s.direction for s in b]


def test_시그널은_주_1회_고정_요일이다():
    """요일·시각을 고정해야 구간 간 비교가 성립한다 (기획서 9.1절)."""
    days = signal_dates(START, END)
    assert days and all(d.weekday() == 2 for d in days)
    assert len(set(days)) == len(days)


def test_시그널_시각은_kst_07시로_고정된다():
    """US/KR 기준가가 대칭이 되는 시각이다 (docs/harness.md 2절)."""
    sigs = random_signals("US", START, END, seed=7)
    assert {s.as_of.hour for s in sigs} == {SIGNAL_HOUR}
    assert {str(s.as_of.tzinfo) for s in sigs} == {"Asia/Seoul"}


def _summ(hit, spearman, n=5):
    return {n: {"scored": 100, "hit_rate": hit,
                "confidence_correlation": {"spearman": spearman, "n": 100}}}


def test_랜덤이_50퍼센트_근처면_통과한다():
    runs = [(s, _summ(0.5 + d, 0.01)) for s, d in
            zip(range(5), (0.01, -0.02, 0.0, 0.015, -0.01))]
    rep = calibration_report(runs)
    assert rep["passed"] and rep["checks"][0]["runs"] == 5


def test_적중률이_치우치면_저울을_의심한다():
    """랜덤인데 65%가 나오면 채점기가 편향된 것이다."""
    runs = [(s, _summ(0.65, 0.0)) for s in range(5)]
    rep = calibration_report(runs)
    assert not rep["passed"] and rep["checks"][0]["hit_rate_ok"] is False


def test_확신도_상관이_0이_아니면_저울을_의심한다():
    runs = [(s, _summ(0.5, 0.30)) for s in range(5)]
    rep = calibration_report(runs)
    assert not rep["passed"] and rep["checks"][0]["confidence_ok"] is False


def test_시드별_분산을_함께_보고한다():
    """평균만 보면 시드 운에 속는다 (기획서 6절)."""
    runs = [(s, _summ(h, 0.0)) for s, h in enumerate((0.48, 0.50, 0.52))]
    c = calibration_report(runs)["checks"][0]
    assert c["hit_rate_sd"] is not None and c["hit_rate_range"] == (0.48, 0.52)


def test_시드를_결과에_남긴다():
    rep = calibration_report([(11, _summ(0.5, 0.0)), (22, _summ(0.5, 0.0))])
    assert rep["seeds"] == [11, 22]
