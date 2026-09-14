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


def require(name: str) -> str:
    """키를 읽어 반환한다. 없으면 이름만 알리고 값은 절대 노출하지 않는다."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}이(가) 설정되지 않았습니다. .env를 확인하세요.")
    return value
