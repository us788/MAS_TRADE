"""SEC EDGAR 403 진단 — 전송 계층 비교.

헤더 변형이 전부 막혔다면 원인은 UA 문자열이 아니라 **클라이언트 지문**이다.
SEC 앞단(Akamai)은 TLS 핸드셰이크와 HTTP/2 특성으로 봇을 가린다.
그래서 여기서는 헤더가 아니라 **무엇으로 보내는가**를 바꿔가며 본다.

    python scripts/diagnose_sec.py

전부 막히면 10분쯤 기다렸다 다시 실행한다 (IP 단위 임시 차단이 풀리는 시간).
"""
import os
import re
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")

UA = os.getenv("SEC_USER_AGENT", "")
if not UA:
    raise SystemExit("SEC_USER_AGENT가 .env에 없습니다.")

URL = "https://data.sec.gov/submissions/CIK0000320193.json"
EMAIL = (re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", UA) or [None])[0] if "@" in UA else None

# 브라우저가 실제로 보내는 헤더 일습. UA만 브라우저로 위장하고 나머지가 비면
# 오히려 불일치로 걸린다.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

results = []


def note(label, ok, detail=""):
    results.append((label, ok))
    if EMAIL:
        detail = detail.replace(EMAIL, "***")
    print(f"{'OK  ' if ok else 'FAIL'} {label:38s} {detail[:90]}")


def looks_like_json(text: str) -> bool:
    return text.lstrip().startswith("{")


# ---- 1. requests + 브라우저 헤더 일습 ----
try:
    import requests
    r = requests.get(URL, headers=BROWSER_HEADERS, timeout=15)
    note("requests + 브라우저 헤더 일습", r.status_code == 200, f"HTTP {r.status_code}")
except Exception as e:
    note("requests + 브라우저 헤더 일습", False, f"{type(e).__name__}: {e}")
time.sleep(2)

# ---- 2. curl (TLS 지문이 파이썬과 다르다) ----
try:
    out = subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
         "-H", f"User-Agent: {UA}", "--compressed", URL],
        capture_output=True, text=True, timeout=30)
    code = out.stdout.strip()
    note("curl", code == "200", f"HTTP {code} {out.stderr.strip()[:60]}")
except Exception as e:
    note("curl", False, f"{type(e).__name__}: {e}")
time.sleep(2)

# ---- 3. httpx + HTTP/2 ----
try:
    import httpx
    with httpx.Client(http2=True, timeout=15, headers={"User-Agent": UA}) as c:
        r = c.get(URL)
    note("httpx (HTTP/2)", r.status_code == 200, f"HTTP {r.status_code}")
except ImportError:
    note("httpx (HTTP/2)", False, "미설치 — pip install 'httpx[http2]'")
except Exception as e:
    note("httpx (HTTP/2)", False, f"{type(e).__name__}: {e}")
time.sleep(2)

# ---- 4. curl_cffi (브라우저 TLS 지문 자체를 흉내낸다) ----
try:
    from curl_cffi import requests as cffi
    r = cffi.get(URL, headers={"User-Agent": UA}, impersonate="chrome", timeout=15)
    note("curl_cffi (chrome 지문)", r.status_code == 200, f"HTTP {r.status_code}")
except ImportError:
    note("curl_cffi (chrome 지문)", False, "미설치 — pip install curl_cffi")
except Exception as e:
    note("curl_cffi (chrome 지문)", False, f"{type(e).__name__}: {e}")

print()
win = [n for n, ok in results if ok]
if win:
    print(f"통과: {win[0]} → 이 전송 방식을 어댑터에 반영합니다.")
else:
    print("전부 실패. IP 단위 차단이 아직 안 풀렸을 수 있습니다.")
    print("10분 기다렸다가 다시 실행해 보세요. 그래도 같으면 전송 계층을 더 바꿔야 합니다.")
