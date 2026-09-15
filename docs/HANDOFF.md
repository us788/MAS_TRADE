# 인수인계 — 2026-09-16

다음 세션이 **가장 먼저 읽을 문서**. 지금 어디까지 왔고, 무엇부터 해야 하고,
무엇을 건드리면 안 되는지.

---

## 한 줄

데이터 레이어의 **뉴스 수집 파이프라인까지 동작하고, 이제 자동으로 돈다.**
저장소를 `~/projects/MAS_TRADE`로 옮기고 **launchd로 매시 수집을 걸었다.**
에이전트·백테스트 하네스는 아직 없다.

---

## 지금 상태

| 항목 | 상태 |
|---|---|
| 저장소 | `~/projects/MAS_TRADE` — **Desktop에서 이사 완료 (2026-09-16)** |
| 기사 | 5,944건 (KR 4,967 · US 977) |
| 수집 실행 | 70회, 열린 gap 0건 |
| 최초 관측 | 2026-09-15 09:24 UTC — **그 이전 뉴스는 영원히 없다** |
| 보유 최古 기사 | 발행 기준 2026-09-13 09:41 UTC (첫 수집의 lookback이 닿은 지점) |
| 테스트 | 67건 통과 |
| 정기 실행 | **launchd 등록됨** (`com.ys.mastrade.collect`, 매시 5분 + 로그인 시) |
| SEC EDGAR | 403으로 보류 |

> 이전 핸드오프는 기록 시작을 `03:26 UTC`로 적었는데 DB와 맞지 않는다.
> `min(collected_at)`과 `min(ran_at)` 모두 `09:24 UTC`다. 원인 미상 —
> 이 문서의 값이 실측이다.

### 상태 확인 방법

```bash
python scripts/collect_news.py --status     # 저장 현황, 등급 교차표, 열린 gap
python scripts/collect_news.py --dry-run    # 지금 돌면 어떤 종목이 대상인지
python -m pytest tests/ -q                  # 67건

scripts/install_scheduler.sh --status       # launchd 등록 상태 + 마지막 종료 코드
tail -20 logs/collect.log                   # 최근 실행 결과
```

**`last exit code = 0`이고 `logs/collect.log`에 매시 기록이 쌓이면 정상이다.**

---

## 1순위 — 첫 정시 실행 확인

**이사와 launchd 등록은 끝났다.** 남은 건 실제 수집이 자동으로 되는지 확인하는 것뿐이다.

검증 실행이 주기상 대상 0건이었던 탓에 **launchd를 통한 실제 수집 쓰기는 아직 확인되지
않았다.** 다음 정시(매시 5분) 실행에서 이것부터 본다.

```bash
tail -30 logs/collect.log
```

- `수집 N건`이 찍혀 있으면 끝. 더 할 일 없다.
- `대상 0`만 계속 나오면 정상일 수도 있다 (직전에 수동으로 돌렸으면 주기가 안 됐다).
  `scripts/collect_news.py --dry-run`으로 남은 시간을 본다.
- **파일에 새 줄이 아예 안 늘면** launchd가 실행되지 않은 것이다.
  ```bash
  scripts/install_scheduler.sh --status      # exit code 확인
  cat logs/launchd.err.log                   # 래퍼가 시작조차 못 했을 때만 찍힌다
  ```
- `Operation not permitted` / `exit code 126`이면 TCC다. 저장소가 다시
  `~/Desktop`·`~/Documents`·`~/Downloads` 아래로 들어갔는지 본다.

### 스케줄러 다루기

```bash
scripts/install_scheduler.sh              # 등록 / 재등록 (저장소를 옮겼으면 이것만 다시)
scripts/install_scheduler.sh --status     # 상태와 마지막 종료 코드
scripts/install_scheduler.sh --run-now    # 즉시 1회 (launchd 경유 — 권한 조건이 같다)
scripts/install_scheduler.sh --uninstall  # 해제
```

plist는 `~/Library/LaunchAgents/com.ys.mastrade.collect.plist`에 있다. 저장소 밖이라
git과 무관하고, 경로가 박혀 있으므로 **이사하면 반드시 재등록해야 한다.**

`scripts/run_collect.sh`는 자기 위치로 저장소 루트를 찾으므로 옮겨도 안 깨진다.

### 또 이사할 일이 생기면

1. **`mv`로 옮긴다. 다시 clone하지 않는다.**
   `data/mas_trade.sqlite3`와 `data/snapshots/`는 gitignore 대상이라 clone에 딸려오지
   않는다. 새로 받으면 **5,944건과 기록 시작점을 잃는다.** 다시 받을 수 없는 데이터다.
2. **venv를 다시 만든다.** shebang에 절대경로가 박혀 있다.
   ```bash
   rm -rf .venv && python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. **`.env`가 따라왔는지 확인한다.** gitignore 대상이다. 없으면 수집이 전부 실패한다.
   ```bash
   python scripts/smoke_test_apis.py    # 아래 주의 참고
   ```
   > **주의 — 새벽에는 DART가 거짓 실패한다.** 날짜 없이 당일 공시를 조회하는데
   > 공시가 없는 시간대면 `status=013`이 와서 FAIL로 찍힌다. 키 문제가 아니다.
   > 아직 안 고쳤다 (미결 3번). 낮에 돌리면 8개 중 SEC만 실패하는 게 정상이다.
4. **`scripts/install_scheduler.sh`를 다시 돌린다.** plist 경로가 갱신된다.
5. 경로가 박힌 문서를 고친다 (`README.md`, 이 문서).

### 왜 급한가

피크(KST 16시경) 유입이 200건/h인데 한 번에 1,000건까지만 받을 수 있다.
→ **장중에 5시간 넘게 멈추면 그 앞은 영구 손실이다.**
야간 복구는 쉽고 주간 복구는 불가능하다. 장애 감지는 한국 장중을 기준으로 봐야 한다.

**launchd도 이걸 완전히 막지는 못한다.** 잠든 사이 밀린 실행은 깨어날 때 **한 번으로
합쳐진다.** 뚜껑 닫고 반나절 나가 있으면 그 사이 유입분은 1,000건 한도에 잘린다.
노트북으로 24시간 수집하는 건 구조적 한계다 (미결 2번).

---

## 건드리기 전에 알아야 할 설계 결정

근거는 `docs/journal/2026-09-15.md`에 있다. **모르고 뒤집으면 데이터가 망가진다.**

| 결정 | 이유 |
|---|---|
| `INSERT OR IGNORE` (REPLACE 아님) | `collected_at`이 최초 관측 시각으로 고정돼야 `known_at`의 보수적 판정이 유지된다 |
| as-of 필터는 항상 `known_at` | `published_at`으로 거르면 벤더 시각이 깨졌을 때 룩어헤드가 샌다 |
| 실패한 수집은 `collection_runs`에 기록하지 않는다 | 기록하면 "방금 돌았다"로 판정돼 다음 주기까지 재시도하지 않는다 |
| lookback = 마지막 수집 이후 × 1.5 | 고정값은 평소 과하게 받고 장기 중단 뒤엔 못 메운다 |
| `relevance='summary'`도 버리지 않는다 | "스쳐 언급도 예측력이 있나"를 추가 호출 없이 사후 재채점하기 위해 |
| 영문은 단어 경계 + 대소문자 무시, 한국어는 부분 문자열 | 한국어는 조사가 바로 붙어 경계가 성립하지 않는다 (`SKT가`) |
| 원본 응답은 수집 1회 = 파일 1개 | 페이지별로 쓰면 파일명이 같아져 서로를 덮어쓴다 |
| 파괴적 스크립트는 dry-run이 기본 | `reclassify.py`의 잘못된 규칙이 5,009건에 적용되기 전에 잡혔다 |
| 정기 실행은 cron이 아니라 launchd | cron은 자는 사이의 실행을 건너뛰고 따라잡지 않는다. 노트북에서는 그게 곧 데이터 손실이다 |
| 저장소는 Desktop/Documents/Downloads 밖 | TCC가 launchd 실행을 거부한다(`exit 126`). 사용자 권한 LaunchAgent도 막힌다 — 2026-09-16 실측 |

---

## 미결 (급한 순)

1. **첫 정시 실행 확인** — 위 1순위. `logs/collect.log`에 `수집 N건`이 찍히면 끝
2. **잠자기 한계** — launchd는 밀린 실행을 **한 번**으로 합친다. 반나절 닫아두면 그 사이
   유입분은 1,000건 한도에 잘린다. 상시 전원 기기(라즈베리파이·소형 VPS)로 옮기는 게
   정답이지만 지금은 범위 밖. 그때까지는 **장중에 노트북을 열어두는 것이 사실상의 대책**
3. **DART 스모크 테스트 거짓 실패** — 날짜 없이 당일을 조회해 새벽엔 `013`이 온다.
   `013`을 통과로 보거나 과거 날짜를 조회하게 고친다. 안 고치면 다음 사람이 또 같은 데
   시간을 쓴다 (이번에 썼다)
4. **디스크 여유 27GB / 87%** — 이 프로젝트는 연 3.5GB라 몇 년 가지만, 디스크가 차면
   SQLite 쓰기가 실패하고 그 시간은 영구 손실이다. 래퍼가 10GB 미만에서 WARN,
   1GB 미만에서 ABORT한다. 정리는 ys가 판단할 몫
5. **감시자 자동화** — launchd가 조용히 죽는 걸 잡는 층. 하루 1회 `--status`와
   `collect.log`를 읽어 이상을 알린다. **이건 해석 작업이라 LLM이 값을 더하는 자리다**
   (수집 자체는 아니다 — 근거는 `journal/2026-09-16.md`의 기각 표)
6. **SEC EDGAR 403** — IP 차단이 풀린 뒤 `scripts/diagnose_sec.py`로 전송 계층 비교.
   넷 다 실패하면 SEC 분기별 재무제표 벌크 데이터셋으로 전환 검토 (`filed` 컬럼이 있어
   시점 정합은 동일). 가설과 확인한 사실은 `journal/2026-09-15.md`에 있다
7. **유니버스 시총 순위 검증** — 네트워크 차단으로 실시간 확인을 못 했다
8. **US 1차 자료 판정** — 지금은 벤더 태깅이면 전부 1차다. 그런데 NVDA 250건 중 제목에
   회사명이 있는 건 17%뿐이다. KR(14%)과 기준이 달라 두 시장의 입력 품질이 어긋난다.
   데이터를 더 모아 보고 제목 기준으로 통일할지 결정한다
9. **네이버 매체 화이트리스트** — 동명이의 필터만으로는 연예·광고성 기사가 남는다
10. **가격 어댑터** (yfinance / pykrx) — 시그널 채점에 필요하다. 과거 조회가 되니 급하지 않다
11. **백테스트 하네스** — 기획서가 1순위 산출물로 꼽은 것. 수집이 안정되면 여기로

---

## 읽을 순서

1. `CLAUDE.md` — 코드 작성 규칙
2. `docs/plan.md` — 전체 설계. 특히 0절(원칙), 4절(LLM), 8절(아키텍처)
3. `docs/data-sources.md` — 소스별 한도와 시점 정합 가능 여부
4. `docs/journal/2026-09-16.md` — 스케줄러 선택과 이사. **기각한 선택지 표가 여기 있다**
5. `docs/journal/2026-09-15.md` — 수집 파이프라인 설계의 근거. **길지만 여기에 왜가 다 있다**

---

## 사람에게 물어봐야 하는 것

- `logs/collect.log`에 매시 기록이 쌓이고 있는지 (첫 정시 실행 확인 — 1순위)
- 디스크를 정리했는지 (여유 27GB / 87%)
- SEC EDGAR가 여전히 403인지
- 노트북을 장시간 닫아두는 패턴이 있는지 — 있다면 상시 전원 기기 이전을 앞당겨야 한다
