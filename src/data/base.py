"""시점 정합 데이터 인터페이스.

모든 조회는 as_of를 받는다. as_of 시점 이후에 공개된 정보는 반환하지 않는다.
US/KR 어댑터가 이 인터페이스를 구현하고, 상위 에이전트는 어느 시장인지 몰라야 한다.
"""
from abc import ABC, abstractmethod
from datetime import datetime


class MarketDataSource(ABC):
    """as_of는 날짜가 아니라 타임스탬프다. 장전/장중 발표 구분이 필요하기 때문이다."""

    @abstractmethod
    def get_prices(self, symbol: str, as_of: datetime, lookback_days: int):
        """as_of 이전 종가 시계열."""

    @abstractmethod
    def get_filings(self, symbol: str, as_of: datetime):
        """as_of 이전에 **제출된** 공시. 회계기간 종료일이 아니라 제출일 기준."""

    @abstractmethod
    def get_news(self, symbol: str, as_of: datetime, lookback_days: int):
        """as_of 이전에 **발행된** 기사. 발행 시각 기준으로 필터."""

    @abstractmethod
    def get_macro(self, series_id: str, as_of: datetime):
        """as_of 시점에 알려져 있던 발표치(vintage). 수정 최종치를 반환하면 룩어헤드다."""
