# 아키텍처 명세 (2026-09-16 기준)

**지금 실제로 돌아가는 것**을 적는다. 설계 의도는 `docs/plan.md`, 거기까지 온 경로는
`docs/journal/`에 있다. 이 문서는 **현재 스펙**이다.

수치는 전부 실측이다. 코드를 고치면 이 문서도 같이 고친다.

---

## 0. 한 장 요약

```
 [수집]  매시 5분 · launchd
   네이버(KR) · Finnhub(US) ──► news 11,036행
   yfinance(US) · FDR(KR)   ──► prices 23,664행 (32계열, 3년)
        │
        ▼
 [as-of 게이트]  ◄── 룩어헤드를 막는 유일한 창구
   Store.news_for(known_at ≤ as_of)
   PriceStore.close_at / series(장마감 +30분)
        │
        ▼
 [계산]  src/compute/  ── 모든 수치는 여기서만 나온다
   indicators : 수익률·변동성·이격도·베타·상대강도
   metrics    : 적중률·상관·CAGR·MDD·샤프·소르티노
        │
        ▼
 [컨텍스트]  src/agents/context.py
   지표 JSON + 뉴스 25건(제목·요약·해시) ──► 종목당 약 8KB
        │
        ▼
 [판단]  ── LLM은 여기서만 호출된다 (3절)
   현재: 베이스라인 단일 호출 1회/시그널
   계획: 스크리닝 → 분석 5종 → 논쟁 → 요약
        │
        ▼
 [저장]  signals · opinions · llm_calls · llm_payloads (전량 로깅)
        │
        ▼
 [채점]  src/eval/  ── forward_bar를 부르는 유일한 곳
   Scorer : 5·20거래일 뒤 지수 대비 초과수익
   비교군 : 지수 buy&hold · 랜덤 · 룰 6종
```

---

## 1. 레이어와 모듈

| 레이어 | 모듈 | 행 | 책임 |
|---|---|---|---|
| **설정** | `src/config.py` | 82 | 벤더 도메인·모델명·키 참조. 키 값은 절대 출력하지 않는다 |
| | `src/data/universe.py` | 82 | `config/universe.json` 로더. 유니버스 변경은 changelog 필수 |
| **수집** | `src/data/naver_news.py` | 188 | KR 뉴스. KST→UTC, `<b>` 태그·엔티티·보이지 않는 문자 정규화 |
| | `src/data/finnhub_news.py` | 115 | US 뉴스. 벤더 티커 태깅 보존 |
| | `src/data/news.py` | 306 | `NewsItem`, `known_at` 판정, 관련성 등급, 모지바케 복구 |
| | `src/data/collector.py` | 153 | 종목별 주기 판정, lookback 계산, gap 기록 |
| | `src/data/price_sources.py` | 145 | yfinance(US) / FDR(KR) + 벤치마크 지수 |
| | `src/data/sec_edgar.py` | 335 | **미사용 — 403 차단 중** |
| **저장** | `src/data/storage.py` | 334 | 기사·수집이력·gap. `(symbol, content_hash)` PK |
| | `src/data/prices.py` | 414 | 봉·수정이력·gap. as-of 조회와 forward 조회 |
| | `src/agents/store.py` | 329 | 시그널·의견·LLM 호출·프롬프트 원문 |
| **계산** | `src/compute/indicators.py` | 143 | as-of 지표. **LLM에 가는 모든 수치의 출처** |
| | `src/compute/metrics.py` | 192 | 순수 함수만. DB도 네트워크도 안 탄다 |
| **LLM** | `src/llm/client.py` | 186 | 유일한 벤더 접점. 전량 로깅, 프리픽스 캐싱 |
| | `src/llm/pricing.py` | 47 | 단가·오프피크 판정 |
| **에이전트** | `src/agents/context.py` | 148 | as-of 컨텍스트 조립. **에이전트가 보는 세계 전부** |
| | `src/agents/prompts.py` | 119 | 시장별 고정 프리픽스. 문자열 상수만 |
| | `src/agents/baseline.py` | 169 | 단일 호출 → 스키마 검증 → Signal |
| | `src/agents/schema.py` | 63 | `AgentOpinion` · `RiskVerdict` · `Signal` |
| **평가** | `src/eval/scoring.py` | 255 | 층 1 채점. `forward_bar`를 부르는 유일한 곳 |
| | `src/eval/benchmarks.py` | 136 | 지수 buy&hold, 동일가중 랜덤, 저울 검정 |
| | `src/eval/strategies.py` | 206 | 룰 기반 비LLM 비교군 6종 |
| | `src/eval/portfolio.py` | 228 | 층 2 가상 포트폴리오. **체결가는 시그널 당일 종가** |

테스트 178건. 전부 네트워크를 타지 않는다.

---

## 2. 데이터 저장소

`data/mas_trade.sqlite3` **14.1 MB**, `data/snapshots/` **12 MB**.
스키마 버전이 셋으로 나뉜다 — 서로 독립이라 한쪽 변경이 다른 쪽 마이그레이션을
강요하지 않는다.

| 테이블 | 행 | 소유 | 비고 |
|---|---|---|---|
| `news` | 11,036 | `Store` | `INSERT OR IGNORE` — `collected_at`이 최초 관측으로 고정 |
| `collection_runs` | 137 | | **실패는 기록하지 않는다** → 다음 주기에 재시도 |
| `collection_gaps` | 3 | | 열림 3건 (아래 5.3절) |
| `prices` | 23,664 | `PriceStore` | 32계열(종목 30 + 지수 2), 3년 |
| `price_revisions` | 0 | | `close_px` 변경은 보류, `close_tr`만 덮는다 |
| `price_gaps` | 0 | | |
| `signals` | 30 | `SignalStore` | |
| `opinions` | 30 | | **에이전트별 개별 의견** — 부분집합 재채점의 전제 |
| `llm_calls` | 30 | | 모델 버전·토큰·캐시·비용 |
| `llm_payloads` | 30 | | 프롬프트 가변부 + 응답 원문 |
| `prompt_texts` | 2 | | 고정 프리픽스를 해시로 한 번만 |
| `runs` | 2 | | 프롬프트 버전·유니버스 버전 |

---

## 3. LLM 호출 명세

### 3.1 현재 — 시그널 1건당 **호출 1회**

전부 `src/llm/client.py`를 통한다. 에이전트가 벤더 SDK를 직접 부르는 코드는 없다.

| 역할 | 에이전트 | 모델 | 모드 | 호출/시그널 |
|---|---|---|---|---|
| 종목 판단 | `baseline` | `deepseek-v4-pro` | **thinking** | **1** |

실측 (30건 평균, 2026-09-16)

```
입력 토큰    4,684   (그중 캐시 적중 1,067 = 23%)
추론 토큰    2,570   ← thinking 모드가 쓰는 양. max_tokens에 포함된다
출력 토큰    2,973
지연           72초
비용       $0.0166 / 시그널   (30종목 1회 = $0.498)
```

**한 바퀴(30종목) = 호출 30회.** 주 1회 실행이므로 연 약 1,560회, $26.

### 3.2 각 파라미터가 왜 그 값인가

| 항목 | 값 | 근거 |
|---|---|---|
| 모델 | `deepseek-v4-pro` 고정 | 다른 벤더를 섞으면 "구조가 이겼나 모델이 좋았나"가 분리되지 않는다 (기획서 4.5절) |
| thinking | 켬 | 기획서 4.5절이 베이스라인을 "thinking, 도구·에이전트 없음"으로 고정 |
| temperature | 0 | 재현성 (4.3절). 올리려면 이유를 남긴다 |
| `max_tokens` | 8,000 | **추론 토큰을 포함한 총량이다.** 1,600으로 뒀을 때 전부 추론에 쓰여 본문이 빈 문자열로 왔다 |
| `response_format` | `json_object` | 스키마 강제 |
| 프리픽스 | 시장별 고정 문자열 | 캐시 히트 단가가 30~50배 싸다. 날짜·종목명을 넣으면 매번 깨진다 |

**thinking 스위치는 반드시 명시한다.** `extra_body={"thinking": {"type": "enabled"|"disabled"}}`.
보내지 않으면 **벤더 기본값이 enabled**라, `thinking=False` 의도가 조용히 무시된다.
실제로 어느 모드로 돌았는지는 `llm_calls.reasoning_tokens`로 확인한다.

### 3.3 계획 — 기획서 8절의 전체 구조

**아직 하나도 구현되지 않았다.** 베이스라인 표본이 쌓인 뒤에 붙인다.

| 단계 | 모델 | 모드 | 호출 수 | 상태 |
|---|---|---|---|---|
| 1차 스크리닝 (유니버스 전 종목) | `deepseek-flash` | non-thinking | 종목당 1 | ❌ |
| 펀더멘털 에이전트 | `deepseek-v4-pro` | thinking | 후보당 1 | ❌ |
| 기술적 에이전트 | 〃 | 〃 | 후보당 1 | ❌ |
| 뉴스·센티먼트 에이전트 | 〃 | 〃 | 후보당 1 | ❌ |
| 거시 에이전트 | 〃 | 〃 | 후보당 1 | ❌ |
| 수급 에이전트 (KR 비중 ↑) | 〃 | 〃 | 후보당 1 | ❌ |
| 논쟁 레이어 (강세 ↔ 약세) | 〃 | 〃 | 후보당 2 × N라운드 | ❌ |
| 요약·포맷 정리 | `deepseek-flash` | non-thinking | 후보당 1 | ❌ |
| **리스크 판정** | **LLM 아님 — 룰 엔진** | — | 0 | ❌ |

N=2라면 **후보 1종목당 약 11회**가 된다 (현재 1회). 비용은 단순히 11배가 아니다 —
스크리닝은 `flash` non-thinking이라 훨씬 싸고, 분석 에이전트는 각자 입력의 일부만
본다. 착수 시점에 실측한다.

**리스크 레이어에 LLM을 쓰지 않는 것이 설계다.** LLM이 제안하고 룰 엔진이 허가하는
비대칭 구조다 (기획서 8절). 기각 사유는 코드로 판정하고 기록한다.

### 3.4 무엇을 프롬프트에 넣지 않는가

- **계좌 정보·개인 식별 정보·타 서비스 키** — 공개 시장 데이터와 공시 원문만 보낸다
- **기사 본문** — API가 준 제목·요약까지다. 무료 티어는 대부분 재배포 금지라
  `id`에 해시를 박아 로그 공개 시 본문 없이 참조만 남긴다
- **산술 요청** — 수치는 `src/compute/`가 계산해 넘긴다. 프롬프트에 "주어진 수치를
  다시 계산하지 말라"고 명시했다. (실례: `v4-pro` non-thinking이 `17 × 23`을
  491이라고 답한 적이 있다. 정답 391)

---

## 4. 시점 정합 방어선

룩어헤드를 막는 지점이 어디인지가 이 프로젝트의 핵심이다. **여섯 군데다.**

| # | 지점 | 무엇을 막나 |
|---|---|---|
| 1 | `Store.news_for(as_of)` | `known_at ≤ as_of`. `published_at`이 아니다 — 벤더 시각이 깨져도 샌다 |
| 2 | `NewsItem.known_at` | 발행 시각이 수집 시각보다 미래면 **수집 시각**을 쓴다 (보수적) |
| 3 | `PriceStore.close_at / series` | 장마감 **+30분** 이후에만 그날 종가를 안다. 미국은 `zoneinfo`로 서머타임 처리 |
| 4 | `ContextBuilder` | 에이전트가 보는 **유일한 창구.** 원본 소스 직접 조회 코드를 에이전트에 넣지 않는다 |
| 5 | **모듈 분리** | `forward_bar`는 `src/eval/`만 부른다. 에이전트에는 as-of 인터페이스만 주입 |
| 6 | `Scorer`의 상태값 | `pending`·`stale`·`misaligned`·`no_base` — **못 잰 것을 숫자로 채우지 않는다** |
| 7 | `VirtualPortfolio`의 체결가 | 판단은 전날 종가로 하고 **체결은 당일 종가**다. 기준가로 체결하면 실행 불가능한 가격으로 성적을 내게 된다 |

5번이 특히 중요하다. 같은 객체에 as-of 조회와 미래 조회가 함께 있으면 에이전트가
실수로 미래를 본다. 모듈을 나눠 구조적으로 막았다.

---

## 5. 정기 실행

### 5.1 스케줄 (launchd, 시스템 현지 시간 = KST)

| 작업 | 시계 | 네트워크 연결 시 | RunAtLoad | 래퍼 |
|---|---|---|---|---|
| `com.ys.mastrade.collect` | 매시 5분 | — | true | `scripts/run_collect.sh` |
| `com.ys.mastrade.baseline` | 매주 수 07:00 | ✅ | **false** | `scripts/run_baseline.sh` |
| `com.ys.mastrade.health` | 매일 08:30 | ✅ | true | `scripts/run_health.sh` |

**노트북은 정해진 시각에 켜져 있지 않다.** 시계만 믿으면 그날 몫을 통째로 거르므로,
`LaunchEvents`의 `com.apple.system.config.network_change`로 **뚜껑을 열어 Wi-Fi가
붙는 순간**에도 발화시킨다. 수집(매시)에는 붙이지 않는다 — 네트워크는 하루에도 수십 번
바뀌고, 매시 주기가 이미 그 역할을 한다.

이 트리거는 **Wi-Fi가 끊길 때도 발화한다.** 그래서 두 겹의 방어가 필요하다.

1. **DNS 대기** — 래퍼가 벤더 호스트를 최대 30초까지 확인하고, 안 되면 조용히
   건너뛴다. 건너뛰면 수집 기록이 안 남으므로 다음 회차 lookback이 늘어 메운다
2. **하루(주) 1회 가드** — 점검은 스탬프 파일, 베이스라인은
   `has_successful_run()`으로 같은 시점 중복을 막는다

**실행 중 잠자기 차단**: 모든 래퍼가 `caffeinate -im`으로 감싼다. launchd는 DarkWake로
맥을 깨워 작업을 발화시키지만 **DarkWake는 1분도 안 돼 다시 잠들어** 실행 도중
네트워크가 끊긴다(2026-09-17 실측). 배터리에서는 시스템 잠자기를 막을 수 없어
뚜껑을 닫으면 여전히 취약하다.

cron이 아니라 launchd인 이유는 **노트북은 잠들고 cron은 따라잡지 않기 때문**이다.
`StartCalendarInterval`은 깨어날 때 밀린 실행을 한 번 돌린다.

베이스라인이 `RunAtLoad=false`인 이유는 **돈이 나가기 때문**이다(1회 약 $0.5).
밀린 실행 따라잡기는 `StartCalendarInterval`이 이미 해 준다.

**수요일 07:00 KST**인 이유는 둘이다. US/KR 기준가가 대칭이 되는 시각이고
(`harness.md` 2절), DeepSeek 오프피크다.

### 5.2 실패 처리 — 예외가 아니라 정상 경로

- 수집 실패는 `collection_runs`에 **기록하지 않는다.** 기록하면 "방금 돌았다"로
  판정돼 다음 주기까지 재시도하지 않는다
- lookback은 마지막 수집 이후 × 1.5. 고정값은 평소 과하게 받고 장기 중단 뒤엔
  못 메운다
- 베이스라인은 같은 (종류·시장·시점)을 이미 성공했으면 건너뛴다 — 따라잡기와
  수동 실행이 겹쳐도 비용이 두 번 나가지 않는다

**실제로 작동한 사례 (2026-09-16 09:09 UTC)**: DNS 실패로 KR 1시간 주기 3종목이
실패해 gap에 기록됐고, 다음 주기(10:05 UTC)에 lookback이 늘어 평소 15건 대신
355건을 받아 구멍을 메웠다.

### 5.3 알려진 문제 — gap이 자동으로 닫히지 않는다

`resolve_gap()`은 있지만 **아무도 부르지 않는다.** 위 사례처럼 복구돼도
`open_gaps()`에 남아 있다. `--status`의 "열린 gap N건"이 상태 지표인데
복구돼도 줄지 않으면 지표가 쓸모없어진다. → 미결.

---

## 6. 구현 상태

| | |
|---|---|
| ✅ **동작** | 뉴스·가격 수집, as-of 인터페이스, 지표, 컨텍스트, 베이스라인 단일 호출, 시그널 저장, 층 1 채점, **층 2 가상 포트폴리오**, 비교군(지수·랜덤·룰 6종), 저울 검정, gap 자동 해소, launchd 3작업 |
| 🟡 **부분** | SEC EDGAR 어댑터(코드는 있으나 403) |
| ❌ **미구현** | 분석 에이전트 5종, 논쟁 레이어, **리스크 엔진**, 리플렉션, DART 어댑터, 매크로(ALFRED) 어댑터, 오픈소스 프레임워크 비교 |
| 🚫 **만들지 않음** | 실주문 경로(`RUN_MODE`는 `paper` 외 불가), 주문 체결 시뮬레이션, 통화 환산 합산 |

**지금 LLM이 보는 자연어는 뉴스 제목·요약뿐이다.** 공시·재무는 어댑터가 없어
컨텍스트에 들어가지 않는다.

---

## 7. 외부 의존

| 용도 | 벤더 | 한도 | 키 |
|---|---|---|---|
| KR 뉴스 | 네이버 검색 API (apihub) | 일 25,000 | `NAVER_CLIENT_ID` / `_SECRET` |
| US 뉴스 | Finnhub `company-news` | 분당 60 | `FINNHUB_API_KEY` |
| US 가격·지수 | yfinance | 비공식 | 없음 |
| KR 가격·지수 | FinanceDataReader | 비공식 | 없음 |
| LLM | DeepSeek (OpenAI 호환) | — | `DEEPSEEK_API_KEY` |

**pykrx는 가격 경로에서 뺐다** — 지수 조회가 깨졌고 KRX 로그인을 요구한다.
수급 데이터 용도로만 남긴다.

벤더 교체는 `src/config.py`의 `base_url`·모델명 변경으로 끝나야 한다 (기획서 4.7절).
