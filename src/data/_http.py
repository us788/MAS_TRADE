"""어댑터 공용 HTTP 유틸. 속도 제한과 재시도를 한 곳에서 관리한다."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests


class RateLimiter:
    """최소 호출 간격을 강제한다. 벤더 한도를 넘기면 차단당한다."""

    def __init__(self, max_rps: float) -> None:
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last = time.monotonic()


class VendorError(RuntimeError):
    """벤더가 정상 응답을 주지 않았다. 호출부는 이걸 잡아 gap으로 기록한다."""


def get_json(
    session: requests.Session,
    url: str,
    *,
    limiter: RateLimiter,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
    attempts: int = 3,
    auth_hint: str = "",
) -> Any:
    """GET → JSON. 일시적 오류는 물러났다 재시도하고, 인증 오류는 즉시 올린다."""
    last: Exception | None = None
    for attempt in range(attempts):
        limiter.wait()
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last = exc
            time.sleep(2**attempt)
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise VendorError(f"JSON이 아닌 응답: {url}") from exc
        if response.status_code in (401, 403):
            raise VendorError(f"인증 실패 (HTTP {response.status_code}). {auth_hint}")
        if response.status_code in (429, 500, 502, 503, 504):
            last = VendorError(f"HTTP {response.status_code}")
            time.sleep(2**attempt)
            continue
        raise VendorError(f"HTTP {response.status_code} from {url}")

    raise VendorError(f"재시도 {attempts}회 실패: {url} ({last})")
