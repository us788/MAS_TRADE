"""환경 설정. 키 값은 os.getenv로 참조만 하고 절대 출력하지 않는다."""
import os

from dotenv import load_dotenv

load_dotenv()

RUN_MODE = os.getenv("RUN_MODE", "paper")
if RUN_MODE != "paper":
    raise ValueError(
        f"RUN_MODE={RUN_MODE!r}는 지원하지 않습니다. 실주문 경로는 존재하지 않습니다."
    )

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---- LLM (DeepSeek · 기획서 4절) ----
# 벤더 교체가 base_url·모델명 변경으로 끝나도록 여기 한 곳에만 둔다 (4.7절).
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL_FAST = os.getenv("LLM_MODEL_FAST", "deepseek-flash")
LLM_MODEL_DEEP = os.getenv("LLM_MODEL_DEEP", "deepseek-v4-pro")

# 재현성 규칙(4.3절): temperature 0 고정. 그래도 결정론은 아니므로 반복 실행으로 분산을 본다.
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "300"))


def require(name: str) -> str:
    """키를 읽어 반환한다. 없으면 이름만 알리고 값은 절대 노출하지 않는다."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}이(가) 설정되지 않았습니다. .env를 확인하세요.")
    return value
