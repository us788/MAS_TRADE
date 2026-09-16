"""가격 벤더 어댑터 — US는 yfinance, KR은 FinanceDataReader.

벤더 선정 근거는 `docs/harness.md` 3.6절. 2026-09-16 실측 결과다.

- **pykrx는 가격 경로에서 뺐다.** `get_index_ohlcv`가 `KeyError: '지수명'`으로 깨져
  KOSPI200을 못 받고, 종목 조회도 호출마다 KRX 로그인 경고가 뜬다. 지금 되는 것도
  언제 막힐지 모른다. 수급 데이터(투자자별 매매동향) 용도로만 남긴다.
- **같은 시계열을 두 소스에서 받지 않는다.** 값이 미세하게 달라지면 비교가 오염된다.
  소스를 바꾸면 `prices.source` 열에 남고 바꾼 날짜를 일지에 적는다.

**두 벤더 모두 미수정 실거래가를 주지 않는다** (2026-09-16 실측).
분할은 시계열 전체에 소급 적용된 값으로 온다. NVDA 10:1 분할 전 봉이 44.78로
들어오고(실거래가는 약 447달러) 시계열에 10배 점프가 없다. 두 열의 차이는
**분할이 아니라 배당**이다.

- `close_px` — 벤더 종가. 분할 조정됨, 배당 미조정. **초과수익 판정은 이 값으로 한다**
  (벤치마크가 배당 미포함 가격지수이기 때문)
- `close_tr` — 분할+배당 조정. 배당 재투자 총수익률용

**KR은 총수익률을 계산할 수 없다.** FDR이 배당 조정 계열을 주지 않아
`close_px = close_tr`로 같은 값이 들어간다. 기획서 9.2절이 "배당은 재투자 가정"이라고
했는데 KR에서는 데이터로 불가능하다. 두 시장을 합산하지 않으므로(9.4절) 치명적이지는
않지만, **US 총수익률과 KR 가격수익률을 나란히 놓고 비교하지 않는다.**
층 1의 초과수익 판정은 양쪽 다 `close_px`라 영향받지 않는다.
"""
from __future__ import annotations

import warnings
from datetime import date, timedelta

from src.data.prices import Bar

# 벤치마크 지수. 적중 판정이 지수 대비 초과라 채점에 반드시 필요하다 (harness.md 4.1).
BENCHMARKS: dict[str, tuple[str, str]] = {
    "US": ("^GSPC", "S&P500"),
    "KR": ("KS200", "KOSPI200"),
}


class PriceFetchError(RuntimeError):
    """벤더 조회 실패. 호출부는 예외가 아니라 gap으로 다룬다 (harness.md 3.7)."""


def _flatten(df):
    """yfinance가 단일 티커에도 MultiIndex 열을 주는 경우가 있다."""
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df = df.copy()
        df.columns = [c[0] for c in df.columns]
    return df


def _f(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN 제거


def fetch_us(symbol: str, start: date, end: date, market: str = "US") -> list[Bar]:
    """yfinance. `auto_adjust=False`로 받아 `Close`와 `Adj Close`를 모두 쓴다.

    `end`는 yfinance에서 **배타적**이라 하루를 더해 넘긴다.
    """
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.download(symbol, start=start.isoformat(),
                         end=(end + timedelta(days=1)).isoformat(),
                         progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        raise PriceFetchError(f"yfinance가 {symbol} {start}~{end} 구간에 빈 응답을 줬습니다.")

    df = _flatten(df)
    if "Close" not in df.columns or "Adj Close" not in df.columns:
        raise PriceFetchError(f"{symbol}: 예상한 열이 없습니다 — {list(df.columns)}")

    bars = []
    for idx, row in df.iterrows():
        close_px, close_tr = _f(row["Close"]), _f(row["Adj Close"])
        if close_px is None or close_tr is None:
            continue  # 결측 행. 연속성 검사가 나중에 잡는다
        vol = _f(row.get("Volume"))
        bars.append(Bar(
            symbol=symbol, market=market, date=idx.date(),
            open_px=_f(row.get("Open")), high_px=_f(row.get("High")),
            low_px=_f(row.get("Low")), close_px=close_px, close_tr=close_tr,
            volume=int(vol) if vol is not None else None, source="yfinance",
        ))
    if not bars:
        raise PriceFetchError(f"{symbol}: 유효한 봉이 없습니다.")
    return bars


def fetch_kr(symbol: str, start: date, end: date, market: str = "KR") -> list[Bar]:
    """FinanceDataReader. 종목(`005930`)과 지수(`KS200`) 모두 같은 경로다.

    수정주가 하나만 오므로 `close_px = close_tr`다. 이유는 모듈 docstring 참고.
    """
    import FinanceDataReader as fdr

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = fdr.DataReader(symbol, start.isoformat(), end.isoformat())
    if df is None or df.empty:
        raise PriceFetchError(f"FDR이 {symbol} {start}~{end} 구간에 빈 응답을 줬습니다.")
    if "Close" not in df.columns:
        raise PriceFetchError(f"{symbol}: Close 열이 없습니다 — {list(df.columns)}")

    bars = []
    for idx, row in df.iterrows():
        close = _f(row["Close"])
        if close is None:
            continue
        vol = _f(row.get("Volume"))
        bars.append(Bar(
            symbol=symbol, market=market, date=idx.date(),
            open_px=_f(row.get("Open")), high_px=_f(row.get("High")),
            low_px=_f(row.get("Low")),
            close_px=close, close_tr=close,   # FDR은 수정주가 하나만 준다
            volume=int(vol) if vol is not None else None, source="fdr",
        ))
    if not bars:
        raise PriceFetchError(f"{symbol}: 유효한 봉이 없습니다.")
    return bars


def fetch(symbol: str, market: str, start: date, end: date) -> list[Bar]:
    """시장에 맞는 어댑터로 넘긴다. 상위 코드는 벤더를 몰라야 한다 (`CLAUDE.md` 3절)."""
    if market == "US":
        return fetch_us(symbol, start, end)
    if market == "KR":
        return fetch_kr(symbol, start, end)
    raise ValueError(f"알 수 없는 시장: {market!r}")


def fetch_benchmark(market: str, start: date, end: date) -> list[Bar]:
    """해당 시장의 벤치마크 지수. `market='INDEX'`로 저장된다."""
    try:
        symbol, _ = BENCHMARKS[market]
    except KeyError:
        raise ValueError(f"{market!r}의 벤치마크가 정의되지 않았습니다.") from None
    fetcher = fetch_us if market == "US" else fetch_kr
    return fetcher(symbol, start, end, market="INDEX")
