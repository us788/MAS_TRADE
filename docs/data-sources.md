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
| **US 공시·재무** | **SEC EDGAR** | `SEC_USER_AGENT` | **초당 10건** | ✅ **`filed` 기준 vintage** | 사용 |
| US 매크로 | FRED / ALFRED | `FRED_API_KEY` | 넉넉함 | ✅ vintage 조회 | 사용 |
| US 뉴스 | Finnhub `company-news` | `FINNHUB_API_KEY` | 분당 60콜 | ✅ 종목+날짜 범위 | 사용 |
| KR 가격·수급 | pykrx / FinanceDataReader | 없음 | 비공식 | ✅ 과거 시계열 | 사용 |
| KR 공시 | DART | `DART_API_KEY` | 일 2만 건 | ✅ 접수일자 | 사용 |
| KR 매크로 | 한국은행 ECOS | `ECOS_API_KEY` | 넉넉함 | ⚠️ vintage 없음 | 제한적 사용 |
| KR 뉴스 | 네이버 검색 API | `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 일 2.5만 (**한시적 무료**) | ⚠️ 날짜 필터 없음 → 직접 자름 | 사용 |
| 센티먼트 기준선 | Alpha Vantage `NEWS_SENTIMENT` | `ALPHAVANTAGE_API_KEY` | **일 25회** | ✅ `time_from`/`time_to` | 베이스라인 전용 |
| KR 뉴스 대체 | 빅카인즈 | 미정 | 승인제 | ✅ 아카이브 | **신청 권장** |
| US 펀더멘털 | Alpha Vantage `OVERVIEW` 등 | — | — | ❌ **현재 스냅샷만** | **미사용** |

---

## SEC EDGAR (US 공시·재무)

**키가 없다.** `User-Agent` 헤더로 신원을 밝히는 방식이고, 없으면 403이 떨어진다.

```
SEC_USER_AGENT=MAS_TRADE usang01@naver.com
```

속도 제한은 **초당 10건**. 넘기면 차단될 수 있어 어댑터는 8 rps로 제한한다
(`SEC_MAX_RPS`). 구현은 `src/data/sec_edgar.py`.

### 엔드포인트

| 용도 | URL |
|---|---|
| 티커 → CIK | `https://www.sec.gov/files/company_tickers.json` |
| 제출 이력 | `https://data.sec.gov/submissions/CIK##########.json` |
| 지표 시계열 | `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/{태그}.json` |
| 전체 재무 팩트 | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` |
| 횡단면 비교 | `https://data.sec.gov/api/xbrl/frames/us-gaap/{태그}/USD/CY2025Q2I.json` |

CIK는 **10자리 제로패딩**이다 (애플 → `CIK0000320193`).

### 왜 EDGAR가 이 프로젝트에 맞는가

XBRL 데이터 포인트마다 **`filed`(제출일)** 이 붙어 있다.

```json
{"start":"2025-01-01","end":"2025-03-31","val":95359000000,
 "accn":"0000320193-25-000xxx","form":"10-Q","filed":"2025-05-02"}
```

`filed <= as_of` 필터 하나로 그 시점에 공개돼 있던 값만 남는다.
**`end`(회계기간 종료일)로 자르면 룩어헤드다** — 3월 말 분기 실적은 5월에 공시된다.

정정 공시도 자동으로 처리된다. 같은 회계기간이 수정되면 `filed`가 다른 항목이 하나 더
쌓이므로, "`filed <= as_of` 중 가장 나중에 제출된 것"이 그 시점의 vintage가 된다.
ALFRED vintage와 같은 구조여서 **재무는 별도 vintage 소스가 필요 없다.**

### 실무 주의

- **`us-gaap` 태그가 회사마다 다르다.** 매출이 `Revenues`인 회사도 있고
  `RevenueFromContractWithCustomerExcludingAssessedTax`인 회사도 있다. 폴백 리스트를
  두지 않으면 종목 절반이 조용히 `None`으로 빠진다 (`CONCEPT_TAGS` 참고).
  **EDGAR 어댑터에서 손이 제일 많이 가는 부분이다.**
- **날짜가 아니라 타임스탬프로 자른다.** `submissions`의 `acceptanceDateTime`을 쓴다.
  ET 17:30 이후 접수분은 다음 영업일에 공시되므로, 제출일만 보면 장마감 후 실적이
  그날 장중 판단에 새어 들어간다.
- **티커 → CIK 매핑은 현재 스냅샷이다.** 티커는 재사용·변경되므로 과거 구간에서 잘못
  연결될 수 있다. 종목 마스터에 CIK를 박아두고 CIK로 조회한다.
- `companyfacts`는 종목당 수 MB다. 종목당 1회 받아 스냅샷으로 저장하고 `as_of` 필터는
  로컬에서 건다. 원본 응답 전량 저장은 어차피 재현성의 전제다.
- **8-K는 뉴스 성격의 이벤트 소스**다. 벤더를 거치지 않은 1차 정보라 타임스탬프가
  가장 신뢰할 만하다. Finnhub 뉴스와 별개로 쓴다.

---

## 뉴스 소스를 시장별로 나눈 이유

영어권 벤더는 한국 기사를 제대로 다루지 못하고, 한국 소스는 미국 종목을 다루지 못한다.
뉴스만은 시장별로 다른 소스를 쓰되 `get_news(symbol, as_of, lookback_days)` 인터페이스
뒤에 숨긴다. 상위 에이전트는 어느 벤더를 쓰는지 몰라야 한다.

**한도가 곧 선택 기준이다.** 과거 뉴스 아카이브는 무료로 구할 수 없어 매일 수집해 직접
쌓는 것이 전제다. 수집이 한도에 막히면 그날 구멍이 생기고 **그 구멍은 나중에 메울 수 없다.**
일 25회짜리를 메인에 두면 이 전제가 무너진다 — Alpha Vantage를 메인에서 뺀 이유가 이것이다.

---

## 네이버 검색 API (KR 뉴스 메인)

### 플랫폼 이관에 주의

기존 개발자센터(`developers.naver.com`)에서 **NAVER API HUB**(네이버 클라우드 플랫폼)로
이관 중이다. 뉴스 검색은 이관 대상이라 살아남는다 (쇼핑·책·전문자료 검색은 종료).

발급처에 따라 **도메인·경로·헤더가 전부 다르다.** `.env`의 `NAVER_API_FLAVOR`로 고른다.

| | `apihub` (신규 권장) | `legacy` (지원 종료 예정) |
|---|---|---|
| 도메인 | `naverapihub.apigw.ntruss.com` | `openapi.naver.com` |
| 경로 | `/search/v1/news` | `/v1/search/news.json` |
| ID 헤더 | `X-NCP-APIGW-API-KEY-ID` | `X-Naver-Client-Id` |
| Secret 헤더 | `X-NCP-APIGW-API-KEY` | `X-Naver-Client-Secret` |

상수는 `src/config.py`의 `NAVER_ENDPOINTS` 한 곳에 있다.

### 발급

**API HUB**: 네이버 클라우드 플랫폼 콘솔 → NAVER API HUB → 앱 등록 → 검색 API 신청 →
Client ID / Secret 확인.

**개발자센터(레거시)**: Application → 애플리케이션 등록 → 사용 API **검색** /
비로그인 오픈 API 환경 **WEB** (서버가 없으면 `http://localhost`로도 발급된다).

### 요금 — "한시적 무료"라는 점이 중요하다

NCP 문서 기준 검색 API는 **일 25,000건 / 월 775,000건 무료**다. 종목 15개 일간 수집에는
차고 넘친다. 다만 문서가 이를 **한시적 무료 제공**이라 명시하고 "정책은 추후 변경될 수
있다"고 적어두었다. 기존 개발자센터의 무료와 성격이 다르다 — **유료화가 예고된 상태다.**

→ 그래서 **빅카인즈 신청을 대비책이 아니라 병행 과제로 둔다.** 언론재단 아카이브는
과금 정책이 이렇게 흔들리지 않고, 승인되면 과거 구간까지 열려 오히려 상위 호환이다.

### 실무 제약 — KR 어댑터의 실제 난이도는 여기 있다

- **날짜 범위 파라미터가 없다.** `sort=date`로 받아 `pubDate`로 직접 자른다. 과거 구간
  조회는 불가능하다
- 한 검색어당 최대 1,000건 (`display` 100 × `start` 1000)
- `pubDate`는 KST. 저장은 UTC로 통일
- `title`·`description`에 `<b>` 태그와 HTML 엔티티가 섞여 온다. 언이스케이프 없이
  프롬프트에 넣지 않는다
- **종목코드로 검색되지 않는다.** 회사명으로 쿼리해야 해서 동명이의 잡음이 크다
  ("한화" → 야구단 기사). 매체 화이트리스트 + 종목명 재확인 필터가 사실상 필수
- 본문 크롤링은 하지 않는다. 제목·요약·링크·발행시각까지만 저장

---

## Finnhub (US 뉴스 메인)

1. [finnhub.io](https://finnhub.io) 가입 (카드 불필요)
2. 대시보드의 API 토큰 → `.env`의 `FINNHUB_API_KEY`
3. `company-news`를 종목 + `from`/`to` 날짜로 호출

무료 티어는 분당 60콜이며 **개인·비상업 용도 한정**이다. 기사 본문을 재배포하지 않는다.

---

## Alpha Vantage (센티먼트 베이스라인 전용)

1. [alphavantage.co](https://www.alphavantage.co/support/#api-key) 에서 이메일만 넣으면 즉시 발급
2. `.env`의 `ALPHAVANTAGE_API_KEY`

**수집기로 쓰지 않는다.** 무료 한도가 분당 5회 / 일 25회라 유니버스 전체 수집이 불가능하다.
`NEWS_SENTIMENT`가 주는 종목별 `sentiment_score`·`relevance_score`를
**LLM 센티먼트 에이전트와 겨룰 제3자 룰 기반 기준선**으로 쓴다. FinRL을 비LLM
베이스라인으로 둔 것과 같은 논리다. 상위 후보 종목에만 호출한다.

**펀더멘털 계열(`OVERVIEW`, `INCOME_STATEMENT`, `EARNINGS`)은 쓰지 않는다.**
현재 스냅샷만 주기 때문에 "2025-08-12에 알 수 있었던 PER"을 되돌려 받을 수 없다.
그대로 쓰면 데이터 레이어에서 룩어헤드가 샌다. 재무는 EDGAR `filed` 기준을 유지한다.

---

## 빅카인즈 (KR 뉴스 대체 — 신청 권장)

한국언론진흥재단 뉴스 아카이브. 데이터 제공 신청과 승인이 필요하다.
승인되면 매체가 정제돼 있고 과거 구간까지 열려 네이버 API를 대체할 수 있다.
네이버가 한시적 무료인 이상 **승인에 걸리는 시간을 감안해 미리 신청해 둔다.**

---

## 저장 규칙 (소스 공통)

- **`published_at`과 `collected_at`을 둘 다 저장한다.** 벤더가 주는 발행 시각에는
  재발행·수정 시각이 섞여 들어온다. 둘이 어긋나거나 발행 시각이 수상하면 **보수적으로
  `collected_at`을 쓴다.** 뉴스 쪽 룩어헤드의 대부분은 이 한 줄로 막힌다
- 모든 타임스탬프는 **UTC로 정규화**해 저장하고, 표시할 때만 현지 시간으로 바꾼다
- **원본 응답을 그대로 스냅샷에 남긴다.** 파싱 결과만 남기면 나중에 파서를 고쳤을 때
  재현이 안 된다
- **무료 티어는 대부분 재배포 금지다.** 시그널 로그 데이터셋을 공개할 때는 기사 본문을
  빼고 URL·발행시각·해시만 남긴다. 이 구조를 처음부터 잡아둔다
- 키는 `.env`에만 둔다. 값을 로그·주석·커밋·응답에 남기지 않는다

## 한도는 변한다

Alpha Vantage는 500/일 → 100/일 → 25/일로 계속 줄었고, 네이버는 플랫폼째 옮겨가며
"한시적 무료"가 됐다. **한도에 의존하는 설계를 하지 말고 수집 실패를 정상 경로로 다룬다** —
실패한 날짜를 기록하고 재시도한다. 과거 뉴스는 나중에 살 수 없어 거른 날은 영구 손실이다.
