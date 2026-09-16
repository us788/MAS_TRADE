"""시그널 저장소 — 전량 로깅.

기획서 11절이 **"시간이 지날수록 가치가 커지는 유일한 자산"**으로 꼽은 것이
시그널 로그 데이터셋이다. 여기에 빠진 것은 나중에 복원할 수 없다.

남기는 것 (기획서 4.3절·8절):

- 시그널과 **에이전트별 개별 의견** — 부분집합 사후 재채점(무료 ablation)의 전제다.
  최종 의견만 남기면 "펀더멘털만 썼으면 어땠나"를 영영 못 묻는다
- **프롬프트와 응답 원문** — 프롬프트를 고치면 그 전후 성적을 섞으면 안 된다
- **모델 버전·토큰·캐시 적중·추정 비용** — 별칭 뒤에서 모델이 갱신되면 여기서 드러난다
- 참조한 데이터 스냅샷 식별자

고정 프리픽스는 `prompt_texts`에 해시로 한 번만 저장한다. 호출마다 2KB를 복사하면
DB가 이유 없이 불어난다.

스키마 버전은 `Store`·`PriceStore`와 따로 둔다 — 같은 파일을 쓰되 서로 독립이다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from src import config

SIGNAL_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_schema_version (version INTEGER NOT NULL);

-- 실행 1회 = 한 시장의 한 시점. 성적을 구간별로 자를 때 기준이 된다.
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,      -- baseline | agents | ...
    market            TEXT NOT NULL,
    as_of             TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    universe_version  TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    attempted         INTEGER NOT NULL DEFAULT 0,
    succeeded         INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id          TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    market             TEXT NOT NULL,
    as_of              TEXT NOT NULL,
    reference_date     TEXT NOT NULL,
    reference_price    REAL NOT NULL,
    direction          TEXT NOT NULL,
    confidence         REAL NOT NULL,
    dispersion         REAL NOT NULL DEFAULT 0,
    risk_approved      INTEGER NOT NULL DEFAULT 1,
    risk_reasons       TEXT,
    proposed_weight    REAL,
    model_versions     TEXT,
    estimated_cost_usd REAL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_asof ON signals(symbol, as_of);

-- 에이전트별 개별 의견. 부분집합 재채점의 전제라 최종 의견과 별도로 남긴다.
CREATE TABLE IF NOT EXISTS opinions (
    opinion_id        TEXT PRIMARY KEY,
    signal_id         TEXT NOT NULL,
    agent             TEXT NOT NULL,
    direction         TEXT NOT NULL,
    confidence        REAL NOT NULL,
    rationale         TEXT,
    counter_rationale TEXT,
    data_refs         TEXT,
    llm_call_ids      TEXT
);
CREATE INDEX IF NOT EXISTS idx_opinions_signal ON opinions(signal_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id            TEXT PRIMARY KEY,
    run_id             TEXT,
    signal_id          TEXT,
    agent              TEXT,
    tier               TEXT NOT NULL,
    requested_model    TEXT NOT NULL,
    reported_model     TEXT NOT NULL,
    thinking           INTEGER NOT NULL,
    called_at          TEXT NOT NULL,
    latency_ms         INTEGER NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    cached_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    off_peak           INTEGER NOT NULL,
    estimated_cost_usd REAL,
    finish_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_run ON llm_calls(run_id);

-- 고정 프리픽스는 해시로 한 번만. 호출마다 복사하지 않는다.
CREATE TABLE IF NOT EXISTS prompt_texts (
    prompt_hash TEXT PRIMARY KEY,
    market      TEXT NOT NULL,
    text        TEXT NOT NULL,
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_payloads (
    call_id      TEXT PRIMARY KEY,
    prompt_hash  TEXT NOT NULL,
    user_payload TEXT NOT NULL,
    response     TEXT NOT NULL
);
"""


def _json(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class RunHandle:
    run_id: str
    kind: str
    market: str
    as_of: datetime


class SignalStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else config.DATA_DIR / "mas_trade.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM signal_schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO signal_schema_version (version) VALUES (?)",
                             (SIGNAL_SCHEMA_VERSION,))
            elif row["version"] != SIGNAL_SCHEMA_VERSION:
                raise RuntimeError(
                    f"시그널 스키마 버전 불일치: DB {row['version']} vs 코드 "
                    f"{SIGNAL_SCHEMA_VERSION}. 마이그레이션이 필요합니다."
                )

    # ---- 실행 ----

    def start_run(self, kind: str, market: str, as_of: datetime,
                  prompt_version: str, universe_version: str) -> RunHandle:
        run_id = new_id()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, kind, market, as_of, prompt_version,"
                " universe_version, started_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, kind, market, as_of.isoformat(), prompt_version,
                 universe_version, datetime.now(timezone.utc).isoformat()),
            )
        return RunHandle(run_id, kind, market, as_of)

    def has_successful_run(self, kind: str, market: str, as_of: datetime) -> bool:
        """같은 (종류, 시장, 시점)을 이미 성공적으로 돌렸는가.

        launchd는 잠든 사이 밀린 실행을 깨어날 때 한 번 돌리고, `RunAtLoad`도 있어서
        같은 시점이 두 번 불릴 수 있다. 시그널이 중복되면 표본 수가 부풀고 채점이
        오염되므로 실행 전에 확인한다.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE kind = ? AND market = ? AND as_of = ?"
                " AND succeeded > 0 LIMIT 1",
                (kind, market, as_of.isoformat()),
            ).fetchone()
        return row is not None

    def finish_run(self, run: RunHandle, attempted: int, succeeded: int,
                   notes: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, attempted = ?, succeeded = ?,"
                " notes = ? WHERE run_id = ?",
                (datetime.now(timezone.utc).isoformat(), attempted, succeeded,
                 notes, run.run_id),
            )

    # ---- 프롬프트 ----

    def register_prompt(self, market: str, text: str) -> str:
        """고정 프리픽스를 해시로 저장하고 해시를 돌려준다. 같은 것은 한 번만 들어간다."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO prompt_texts (prompt_hash, market, text,"
                " first_seen) VALUES (?,?,?,?)",
                (digest, market, text, datetime.now(timezone.utc).isoformat()),
            )
        return digest

    # ---- 시그널 ----

    def add_signal(self, run: RunHandle, *, symbol: str, market: str,
                   as_of: datetime, reference_date, reference_price: float,
                   direction: str, confidence: float, dispersion: float = 0.0,
                   risk_approved: bool = True, risk_reasons: Sequence[str] = (),
                   proposed_weight: float | None = None,
                   model_versions: Sequence[str] = (),
                   estimated_cost_usd: float | None = None) -> str:
        signal_id = new_id()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO signals (signal_id, run_id, symbol, market, as_of,"
                " reference_date, reference_price, direction, confidence, dispersion,"
                " risk_approved, risk_reasons, proposed_weight, model_versions,"
                " estimated_cost_usd, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (signal_id, run.run_id, symbol, market, as_of.isoformat(),
                 reference_date.isoformat(), reference_price, direction, confidence,
                 dispersion, int(risk_approved), _json(list(risk_reasons)),
                 proposed_weight, _json(list(model_versions)), estimated_cost_usd,
                 datetime.now(timezone.utc).isoformat()),
            )
        return signal_id

    def add_opinion(self, signal_id: str, *, agent: str, direction: str,
                    confidence: float, rationale: str = "",
                    counter_rationale: str = "", data_refs: Sequence[str] = (),
                    llm_call_ids: Sequence[str] = ()) -> str:
        opinion_id = new_id()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO opinions (opinion_id, signal_id, agent, direction,"
                " confidence, rationale, counter_rationale, data_refs, llm_call_ids)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (opinion_id, signal_id, agent, direction, confidence, rationale,
                 counter_rationale, _json(list(data_refs)), _json(list(llm_call_ids))),
            )
        return opinion_id

    def add_call(self, call, *, run_id: str | None = None,
                 signal_id: str | None = None, agent: str | None = None,
                 prompt_hash: str | None = None, user_payload: str | None = None,
                 response: str | None = None) -> None:
        """`LLMCall`과 원문을 함께 남긴다. 원문이 없으면 재현이 안 된다."""
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_calls (call_id, run_id, signal_id, agent,"
                " tier, requested_model, reported_model, thinking, called_at,"
                " latency_ms, prompt_tokens, cached_tokens, completion_tokens,"
                " reasoning_tokens, off_peak, estimated_cost_usd, finish_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call.call_id, run_id, signal_id, agent, call.tier,
                 call.requested_model, call.reported_model, int(call.thinking),
                 call.called_at.isoformat(), call.latency_ms, call.prompt_tokens,
                 call.cached_tokens, call.completion_tokens, call.reasoning_tokens,
                 int(call.off_peak), call.estimated_cost_usd, call.finish_reason),
            )
            if prompt_hash is not None and user_payload is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_payloads (call_id, prompt_hash,"
                    " user_payload, response) VALUES (?,?,?,?)",
                    (call.call_id, prompt_hash, user_payload, response or ""),
                )

    # ---- 조회 ----

    def signals_for(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM signals WHERE run_id = ? ORDER BY symbol", (run_id,))]

    def all_signals(self, market: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signals"
        params: list = []
        if market:
            sql += " WHERE market = ?"
            params.append(market)
        sql += " ORDER BY as_of, symbol"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def counts(self) -> dict:
        with self.connect() as conn:
            q = lambda s: conn.execute(s).fetchone()[0]
            cost = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM llm_calls").fetchone()[0]
            tokens = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(cached_tokens),0),"
                " COALESCE(SUM(completion_tokens),0) FROM llm_calls").fetchone()
            by_dir = {r["direction"]: r["n"] for r in conn.execute(
                "SELECT direction, COUNT(*) AS n FROM signals GROUP BY direction")}
            return {
                "runs": q("SELECT COUNT(*) FROM runs"),
                "signals": q("SELECT COUNT(*) FROM signals"),
                "opinions": q("SELECT COUNT(*) FROM opinions"),
                "llm_calls": q("SELECT COUNT(*) FROM llm_calls"),
                "by_direction": by_dir,
                "prompt_tokens": tokens[0], "cached_tokens": tokens[1],
                "completion_tokens": tokens[2],
                "estimated_cost_usd": cost,
            }
