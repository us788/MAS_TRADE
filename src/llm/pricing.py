"""DeepSeek 단가와 오프피크 판정. 기획서 4.2절.

단가는 벤더가 바꾼다. 여기 숫자는 추정용이고, **실제 청구서와 주기적으로 대조**한다.
비용 수치를 성과 보고에 쓸 때는 추정치임을 명시한다.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

# 백만 토큰당 USD (공식 문서 2026-09 기준)
# (입력 캐시히트, 입력 캐시미스, 출력) — 오프피크 기준. 피크는 2배.
_OFF_PEAK_USD_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}

# 피크 구간: UTC 월~금 01:00-04:00, 06:00-10:00 (= KST 10~13시, 15~19시)
_PEAK_WINDOWS_UTC = ((time(1, 0), time(4, 0)), (time(6, 0), time(10, 0)))


def is_off_peak(at: datetime) -> bool:
    """오프피크면 True. 단가가 절반이 된다."""
    at_utc = at.astimezone(timezone.utc)
    if at_utc.weekday() >= 5:  # 토·일은 전 시간 오프피크
        return True
    t = at_utc.time()
    return not any(start <= t < end for start, end in _PEAK_WINDOWS_UTC)


def estimate_cost_usd(
    model: str,
    *,
    cached_input_tokens: int,
    uncached_input_tokens: int,
    output_tokens: int,
    at: datetime,
) -> float | None:
    """호출 1건의 추정 비용. 단가표에 없는 모델이면 None (모르면 0이 아니라 모른다고 한다)."""
    rates = _OFF_PEAK_USD_PER_MTOK.get(model)
    if rates is None:
        return None
    hit, miss, out = rates
    multiplier = 1.0 if is_off_peak(at) else 2.0
    usd = (
        cached_input_tokens * hit + uncached_input_tokens * miss + output_tokens * out
    ) / 1_000_000
    return usd * multiplier
