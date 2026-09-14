"""API 인증 스모크 테스트.

각 API에 **1회씩만** 호출해 키가 동작하는지 확인한다 (Alpha Vantage는 일 25회 한도).
키 값은 출력하지 않고, 응답 본문에 섞여 나와도 마스킹한다.

    python scripts/smoke_test_apis.py
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")

SECRETS = [v for k, v in os.environ.items()
           if any(t in k for t in ("KEY", "SECRET", "TOKEN")) and v and len(v) > 7]

def scrub(text: str) -> str:
    for s in SECRETS:
        text = text.replace(s, "***")
    return text

def mask(name: str) -> str:
    v = os.getenv(name) or ""
    return f"설정됨({len(v)}자)" if v else "없음"

TIMEOUT = 12
results = []

def report(label, ok, detail=""):
    results.append((label, ok))
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {label:32s} {scrub(detail)[:110]}")

def get(url, **kw):
    return requests.get(url, timeout=TIMEOUT, **kw)

# ---- 1. DeepSeek ----
try:
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        report("DeepSeek", False, "DEEPSEEK_API_KEY 없음")
    else:
        base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
        model = os.getenv("LLM_MODEL_FAST", "deepseek-flash")
        r = requests.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model,
                                "messages": [{"role": "user", "content": "ping"}],
                                "max_tokens": 1, "temperature": 0},
                          timeout=TIMEOUT)
        if r.status_code == 200:
            body = r.json()
            report("DeepSeek chat/completions", True,
                   f"model={body.get('model')} tokens={body.get('usage',{}).get('total_tokens')}")
        else:
            report("DeepSeek chat/completions", False, f"HTTP {r.status_code} {r.text[:80]}")
except Exception as e:
    report("DeepSeek chat/completions", False, f"{type(e).__name__}: {e}")

# ---- 2. SEC EDGAR (키 없음, User-Agent 검증) ----
try:
    ua = os.getenv("SEC_USER_AGENT")
    if not ua:
        report("SEC EDGAR", False, "SEC_USER_AGENT 없음")
    else:
        r = get("https://data.sec.gov/submissions/CIK0000320193.json",
                headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        if r.status_code == 200:
            d = r.json()
            n = len(d.get("filings", {}).get("recent", {}).get("form", []))
            report("SEC EDGAR submissions", True, f"{d.get('name')} / recent {n}건")
        elif r.status_code == 403:
            report("SEC EDGAR submissions", False, "403 — User-Agent에 이름+이메일 필요")
        else:
            report("SEC EDGAR submissions", False, f"HTTP {r.status_code}")
except Exception as e:
    report("SEC EDGAR submissions", False, f"{type(e).__name__}: {e}")

# ---- 3. Finnhub ----
try:
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        report("Finnhub", False, "FINNHUB_API_KEY 없음")
    else:
        base = os.getenv("FINNHUB_BASE_URL", "https://finnhub.io/api/v1")
        to = date.today(); frm = to - timedelta(days=3)
        r = get(f"{base}/company-news",
                params={"symbol": "AAPL", "from": frm.isoformat(), "to": to.isoformat(),
                        "token": key})
        if r.status_code == 200:
            items = r.json()
            sample = items[0].get("datetime") if items else None
            report("Finnhub company-news", True, f"{len(items)}건, 최근 ts={sample}")
        elif r.status_code in (401, 403):
            report("Finnhub company-news", False, f"HTTP {r.status_code} — 키 인증 실패")
        elif r.status_code == 429:
            report("Finnhub company-news", False, "429 — 분당 한도 초과")
        else:
            report("Finnhub company-news", False, f"HTTP {r.status_code}")
except Exception as e:
    report("Finnhub company-news", False, f"{type(e).__name__}: {e}")

# ---- 4. 네이버 검색(뉴스) ----
try:
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    flavor = os.getenv("NAVER_API_FLAVOR", "apihub")
    EP = {"apihub": ("https://naverapihub.apigw.ntruss.com", "/search/v1/news",
                     "X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
          "legacy": ("https://openapi.naver.com", "/v1/search/news.json",
                     "X-Naver-Client-Id", "X-Naver-Client-Secret")}
    if not (cid and csec):
        report(f"네이버 뉴스({flavor})", False, "CLIENT_ID/SECRET 없음")
    elif flavor not in EP:
        report(f"네이버 뉴스({flavor})", False, f"알 수 없는 FLAVOR: {flavor}")
    else:
        base, path, h_id, h_sec = EP[flavor]
        r = get(base + path, params={"query": "삼성전자", "display": 1, "sort": "date"},
                headers={h_id: cid, h_sec: csec})
        if r.status_code == 200:
            d = r.json()
            items = d.get("items", [])
            pub = items[0].get("pubDate") if items else None
            report(f"네이버 뉴스({flavor})", True, f"total={d.get('total')} pubDate={pub}")
        elif r.status_code in (401, 403):
            report(f"네이버 뉴스({flavor})", False,
                   f"HTTP {r.status_code} — 헤더 방식이 발급처와 다를 수 있음")
        else:
            report(f"네이버 뉴스({flavor})", False, f"HTTP {r.status_code} {r.text[:70]}")
except Exception as e:
    report("네이버 뉴스", False, f"{type(e).__name__}: {e}")

# ---- 5. Alpha Vantage (1회만) ----
try:
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not key:
        report("Alpha Vantage", False, "ALPHAVANTAGE_API_KEY 없음")
    else:
        r = get("https://www.alphavantage.co/query",
                params={"function": "NEWS_SENTIMENT", "tickers": "AAPL",
                        "limit": 1, "apikey": key})
        d = r.json() if r.status_code == 200 else {}
        if "feed" in d:
            f = d["feed"][0] if d["feed"] else {}
            report("Alpha Vantage NEWS_SENTIMENT", True,
                   f"feed {len(d['feed'])}건, time={f.get('time_published')}")
        elif "Information" in d or "Note" in d:
            report("Alpha Vantage NEWS_SENTIMENT", False,
                   f"한도/키 안내: {str(d.get('Information') or d.get('Note'))[:80]}")
        elif "Error Message" in d:
            report("Alpha Vantage NEWS_SENTIMENT", False, str(d["Error Message"])[:80])
        else:
            report("Alpha Vantage NEWS_SENTIMENT", False, f"HTTP {r.status_code} 예상 밖 응답")
except Exception as e:
    report("Alpha Vantage NEWS_SENTIMENT", False, f"{type(e).__name__}: {e}")

# ---- 6. DART ----
try:
    key = os.getenv("DART_API_KEY")
    if not key:
        report("DART", False, "DART_API_KEY 없음 (건너뜀)")
    else:
        r = get("https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": key, "page_count": 1})
        d = r.json()
        st = d.get("status")
        ok = st == "000"
        report("DART list.json", ok, f"status={st} {d.get('message','')}")
except Exception as e:
    report("DART list.json", False, f"{type(e).__name__}: {e}")

# ---- 7. FRED ----
try:
    key = os.getenv("FRED_API_KEY")
    if not key:
        report("FRED", False, "FRED_API_KEY 없음 (건너뜀)")
    else:
        r = get("https://api.stlouisfed.org/fred/series",
                params={"series_id": "GDP", "api_key": key, "file_type": "json"})
        if r.status_code == 200 and "seriess" in r.json():
            report("FRED series", True, r.json()["seriess"][0]["title"][:50])
        else:
            report("FRED series", False, f"HTTP {r.status_code} {r.text[:70]}")
except Exception as e:
    report("FRED series", False, f"{type(e).__name__}: {e}")

# ---- 8. ECOS ----
try:
    key = os.getenv("ECOS_API_KEY")
    if not key:
        report("ECOS", False, "ECOS_API_KEY 없음 (건너뜀)")
    else:
        r = get(f"https://ecos.bok.or.kr/api/StatisticTableList/{key}/json/kr/1/1/")
        d = r.json()
        if "StatisticTableList" in d:
            report("ECOS StatisticTableList", True, "정상 응답")
        else:
            msg = json.dumps(d, ensure_ascii=False)[:90]
            report("ECOS StatisticTableList", False, msg)
except Exception as e:
    report("ECOS StatisticTableList", False, f"{type(e).__name__}: {e}")

print("\n--- 키 설정 상태 (값은 출력하지 않음) ---")
for n in ["DEEPSEEK_API_KEY", "SEC_USER_AGENT", "FINNHUB_API_KEY", "NAVER_CLIENT_ID",
          "NAVER_CLIENT_SECRET", "ALPHAVANTAGE_API_KEY", "DART_API_KEY",
          "FRED_API_KEY", "ECOS_API_KEY"]:
    print(f"  {n:24s} {mask(n)}")
print(f"  NAVER_API_FLAVOR         {os.getenv('NAVER_API_FLAVOR')}")

passed = sum(1 for _, ok in results if ok)
print(f"\n{passed}/{len(results)} 통과")

sys.exit(0 if passed == len(results) else 1)
