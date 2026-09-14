"""환경 설정. 키 값은 os.getenv로 참조만 하고 절대 출력하지 않는다.

벤더가 바뀌는 값(도메인·모델명·헤더 이름)은 전부 여기 모은다.
네이버 플랫폼 이관처럼 호출부만 바뀌는 변경은 앞으로도 또 온다.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RUN_MODE = os.getenv("RUN_MODE", "paper")
if RUN_MODE != "paper":
    raise ValueError(
        f"RUN_MODE={RUN_MODE!r}는 지원하지 않습니다. 실주문 경로는 존재하지 않습니다."
    )

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LOG_DIR = Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs"))

# ---- LLM (DeepSeek · 기획서 4절) ----
# 벤더 교체가 base_url·모델명 변경으로 끝나도록 여기 한 곳에만 둔다 (4.7절).
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL_FAST = os.getenv("LLM_MODEL_FAST", "deepseek-flash")
LLM_MODEL_DEEP = os.getenv("LLM_MODEL_DEEP", "deepseek-v4-pro")

# 재현성 규칙(4.3절): temperature 0 고정. 그래도 결정론은 아니므로 반복 실행으로 분산을 본다.
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "300"))

# ---- SEC EDGAR (US 공시) ----
# 키가 없다. User-Agent가 신원 표시이고 없으면 403이다.
SEC_DATA_BASE_URL = "https://data.sec.gov"
SEC_WWW_BASE_URL = "https://www.sec.gov"
# 공식 상한은 초당 10건. 여유를 두고 8로 둔다 — 넘기면 차단될 수 있다.
SEC_MAX_RPS = float(os.getenv("SEC_MAX_RPS", "8"))
SEC_TIMEOUT_SEC = int(os.getenv("SEC_TIMEOUT_SEC", "30"))

# ---- Finnhub (US 뉴스) ----
FINNHUB_BASE_URL = os.getenv("FINNHUB_BASE_URL", "https://finnhub.io/api/v1")

# ---- 네이버 검색 API (KR 뉴스) ----
# 발급처에 따라 도메인·경로·헤더가 전부 다르다. .env의 NAVER_API_FLAVOR로 고른다.
NAVER_API_FLAVOR = os.getenv("NAVER_API_FLAVOR", "apihub")
NAVER_ENDPOINTS: dict[str, dict[str, str]] = {
    "apihub": {
        "base_url": "https://naverapihub.apigw.ntruss.com",
        "news_path": "/search/v1/news",
        "id_header": "X-NCP-APIGW-API-KEY-ID",
        "secret_header": "X-NCP-APIGW-API-KEY",
    },
    "legacy": {
        "base_url": "https://openapi.naver.com",
        "news_path": "/v1/search/news.json",
        "id_header": "X-Naver-Client-Id",
        "secret_header": "X-Naver-Client-Secret",
    },
}


def naver_endpoint() -> dict[str, str]:
    """현재 설정된 네이버 플랫폼의 도메인·경로·헤더 이름."""
    try:
        return NAVER_ENDPOINTS[NAVER_API_FLAVOR]
    except KeyError:
        raise ValueError(
            f"NAVER_API_FLAVOR={NAVER_API_FLAVOR!r}는 지원하지 않습니다. "
            f"{sorted(NAVER_ENDPOINTS)} 중에서 고르세요."
        ) from None


def require(name: str) -> str:
    """키를 읽어 반환한다. 없으면 이름만 알리고 값은 절대 노출하지 않는다."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}이(가) 설정되지 않았습니다. .env를 확인하세요.")
    return value
