# 데이터 소스

어떤 소스를 왜 골랐고, 한도가 얼마고, **시점 정합 조회가 되는지**를 한곳에 모은 문서.
설계 근거는 [`plan.md`](plan.md) 8절.

판단 기준은 하나다 — **그 시점에 실제로 알 수 있었던 값만 남길 수 있는가.**
이게 안 되는 소스는 무료여도 쓰지 않는다.

---

## 한눈에

| 용도 | 소스 | 환경변수 | 한도 | 시점 정합 | 상태 |
|---|---|---|---|---|---|
| US 가격 | yfinance | 없음 | 비공식 | ✅ 과거 시계열 | 사용 |
| US 공시 | SEC EDGAR | `SEC_USER_AGENT` | User-Agent 필수 | ✅ filing date | 사용 |
| US 매크로 | FRED / ALFRED | `FRED_API_KEY` | 넉넉함 | ✅ **vintage 조회** | 사용 |
| **US 뉴스** | **Finnhub `company-news`** | `FINNHUB_API_KEY` | **분당 60콜** | ✅ 종목+날짜 범위 | 사용 |
| KR 가격·수급 | pykrx / FinanceDataReader | 없음 | 비공식 | ✅ 과거 시계열 | 사용 |
| KR 공시 | DART | `DART_API_KEY` | 일 2만 건 | ✅ 접수일자 | 사용 |
| KR 매크로 | 한국은행 ECOS | `ECOS_API_KEY` | 넉넉함 | ⚠️ vintage 없음 | 제한적 사용 |
| **KR 뉴스** | **네이버 검색 API** | `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 일 2.5만 회 수준 | ⚠️ 날짜 필터 없음 → 직접 자름 | 사용 |
| 센티먼트 기준선 | Alpha Vantage `NEWS_SENTIMENT` | `ALPHAVANTAGE_API_KEY` | **일 25회** | ✅ `time_from`/`time_to` | 베이스라인 전용 |
| KR 뉴스 확장 | 빅카인즈 | 미정 | 승인제 | ✅ 아카이브 | 신청 대기 |
| US 펀더멘털 | Alpha Vantage `OVERVIEW` 등 | — | — | ❌ **현재 스냅샷만** | **미사용** |

---

## 뉴스 소스를 시장별로 나눈 이유

영어권 벤더는 한국 기사를 제대로 다루지 못하고, 한국 소스는 미국 종목을 다루지 못한다.
뉴스만은 시장별로 다른 소스를 쓰되 `get_news(symbol, as_of, lookback_days)` 인터페이스 뒤에 숨긴다.
상위 에이전트는 어느 벤더를 쓰는지 몰라야 한다.

**한도가 곧 선택 기준이다.** 과거 뉴스 아카이브는 무료로 구할 수 없어 매일 수집해 직접 쌓는 것이 전제다.
수집이 한도에 막히면 그날 구멍이 생기고 **그 구멍은 나중에 메울 수 없다.**
일 25회짜리를 메인에 두면 이 전제가 무너진다 — Alpha Vantage를 메인에서 뺀 이유가 이것이다.

---

## 발급 절차

### 네이버 검색 API (KR 뉴스 메인)

심사 없이 즉시 발급된다.

1. [네이버 개발자센터](https://developers.naver.com) 로그인
2. **Application → 애플리케이션 등록**
3. 설정:
   - 애플리케이션 이름: `MAS_TRADE` (사용량 화면에서 이 이름으로 구분된다)
   - **사용 API: 검색**
   - **비로그인 오픈 API 서비스 환경: WEB 설정** — 서버가 없으면 `http://localhost`로 넣어도 발급된다.
     서버 사이드 호출이라 이 값은 실제로 검증되지 않는다
4. 발급된 **Client ID** / **Client Secret**을 `.env`에 넣는다 (Secret은 "보기" 버튼을 눌러야 보인다)
5. 실제 한도는 **내 애플리케이션 → 사용량**에서 확인한다

호출 형태:

```
GET https://openapi.naver.com/v1/search/news.json?query={검색어}&display=100&sort=date
X-Naver-Client-Id: {ID}
X-Naver-Client-Secret: {SECRET}
```

응답 `items[]`: `title`, `originallink`, `link`, `description`, `pubDate`

**실무 제약 — KR 어댑터의 실제 난이도는 여기 있다**

- **날짜 범위 파라미터가 없다.** `sort=date`로 받아 `pubDate`로 직접 자른다. 과거 구간 조회는 불가능
- 한 검색어당 최대 1,000건 (`display` 100 × `start` 1000)
- `pubDate`는 KST. 저장은 UTC로 통일
- `title`·`description`에 `<b>` 태그와 HTML 엔티티가 섞여 온다. 언이스케이프 없이 프롬프트에 넣지 않는다
- **종목코드로 검색되지 않는다.** 회사명으로 쿼리해야 해서 동명이의 잡음이 크다 ("한화" → 야구단 기사).
  매체 화이트리스트 + 종목명 재확인 필터가 사실상 필수
- 본문 크롤링은 하지 않는다. 제목·요약·링크·발행시각까지만 저장

### Finnhub (US 뉴스 메인)

1. [finnhub.io](https://finnhub.io) 가입 (카드 불필요)
2. 대시보드에서 API 토큰 확인 → `.env`의 `FINNHUB_API_KEY`
3. `company-news` 엔드포인트를 종목 + `from`/`to` 날짜로 호출

무료 티어는 **개인·비상업 용도 한정**이다. 수집한 기사 본문을 재배포하지 않는다.

### Alpha Vantage (센티먼트 베이스라인 전용)

1. [alphavantage.co](https://www.alphavantage.co/support/#api-key) 에서 이메일만 넣으면 즉시 발급
2. `.env`의 `ALPHAVANTAGE_API_KEY`

**수집기로 쓰지 않는다.** 무료 한도가 분당 5회 / 일 25회라 유니버스 전체 수집이 불가능하다.
`NEWS_SENTIMENT`가 주는 종목별 `sentiment_score`·`relevance_score`를
**LLM 센티먼트 에이전트와 겨룰 제3자 룰 기반 기준선**으로 쓴다.
FinRL을 비LLM 베이스라인으로 둔 것과 같은 논리다. 상위 후보 종목에만 호출한다.

**펀더멘털 계열(`OVERVIEW`, `INCOME_STATEMENT`, `EARNINGS`)은 쓰지 않는다.**
현재 스냅샷만 주기 때문에 "2025-08-12에 알 수 있었던 PER"을 되돌려 받을 수 없다.
그대로 쓰면 데이터 레이어에서 룩어헤드가 샌다. 재무는 EDGAR filing date 기준을 유지한다.

### 빅카인즈 (KR 뉴스 확장 — 신청 대기)

한국언론진흥재단 뉴스 아카이브. 데이터 제공 신청과 승인이 필요하다.
승인되면 매체가 정제돼 있고 과거 구간까지 열려 네이버 API를 대체할 수 있다.
신청해 두고 승인 여부에 따라 교체를 판단한다.

---

## 저장 규칙 (소스 공통)

- **`published_at`과 `collected_at`을 둘 다 저장한다.** 벤더가 주는 발행 시각에는 재발행·수정 시각이
  섞여 들어온다. 둘이 어긋나거나 발행 시각이 수상하면 **보수적으로 `collected_at`을 쓴다.**
  뉴스 쪽 룩어헤드의 대부분은 이 한 줄로 막힌다
- 모든 타임스탬프는 **UTC로 정규화**해 저장하고, 표시할 때만 현지 시간으로 바꾼다
- 원본 응답을 그대로 스냅샷에 남긴다. 파싱 결과만 남기면 나중에 파서를 고쳤을 때 재현이 안 된다
- **무료 티어는 대부분 재배포 금지다.** 시그널 로그 데이터셋을 공개할 때는 기사 본문을 빼고
  URL·발행시각·해시만 남긴다. 이 구조를 처음부터 잡아둔다
- 키는 `.env`에만 둔다. 값을 로그·주석·커밋·응답에 남기지 않는다

## 한도 요약 (2026-09 기준, 변동됨)

숫자는 벤더가 바꾼다. Alpha Vantage는 500/일 → 100/일 → 25/일로 계속 줄었다.
**한도에 의존하는 설계를 하지 말고, 수집 실패를 정상 경로로 다룬다** — 실패한 날짜를 기록하고 재시도한다.
