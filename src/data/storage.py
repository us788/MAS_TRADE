"""스냅샷 저장소.

원칙 두 가지 (기획서 8절):
1. **원본 응답을 그대로 남긴다.** 파싱 결과만 저장하면 나중에 파서를 고쳤을 때
   재현이 안 된다. 원본은 raw/ 아래에 따로 쌓는다.
2. **수집 실패는 예외가 아니라 정상 경로다.** 실패한 (날짜, 시장, 종목)을 기록해
   다음 실행에서 재시도한다. 과거 뉴스는 나중에 살 수 없어 거른 날은 영구 손실이다.

레이아웃:
    data/snapshots/news/{market}/{YYYY-MM-DD}/{source}.jsonl   파싱된 기사
    data/snapshots/raw/{source}/{YYYY-MM-DD}/{symbol}_{ts}.json 원본 응답
    logs/collection_gaps.jsonl                                   실패 기록
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.data.news import NewsItem


@dataclass
class WriteResult:
    written: int
    skipped_duplicate: int

    @property
    def total(self) -> int:
        return self.written + self.skipped_duplicate


class SnapshotStore:
    def __init__(self, root: Path | None = None, log_dir: Path | None = None) -> None:
        self.root = root or config.SNAPSHOT_DIR
        self.log_dir = log_dir or config.LOG_DIR

    # ---- 경로 ----

    def news_path(self, market: str, day: datetime, source: str) -> Path:
        return self.root / "news" / market / day.strftime("%Y-%m-%d") / f"{source}.jsonl"

    def raw_path(self, source: str, day: datetime, symbol: str) -> Path:
        stamp = day.strftime("%Y%m%dT%H%M%SZ")
        safe = symbol.replace("/", "_")
        return self.root / "raw" / source / day.strftime("%Y-%m-%d") / f"{safe}_{stamp}.json"

    # ---- 쓰기 ----

    def save_raw(self, source: str, symbol: str, payload, collected_at: datetime) -> Path:
        path = self.raw_path(source, collected_at, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def append_news(self, items: list[NewsItem]) -> WriteResult:
        """기사를 날짜·시장·소스별 JSONL에 덧붙인다. content_hash로 중복을 막는다.

        같은 기사가 여러 날 검색에 걸리는 일이 흔하다. 중복을 그대로 쌓으면
        센티먼트 집계가 기사 수에 끌려간다.
        """
        written = duplicate = 0
        by_file: dict[Path, list[NewsItem]] = {}
        for item in items:
            path = self.news_path(item.market, item.collected_at, item.source)
            by_file.setdefault(path, []).append(item)

        for path, group in by_file.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            seen = self._existing_hashes(path)
            lines = []
            for item in group:
                key = (item.symbol, item.content_hash)
                if key in seen:
                    duplicate += 1
                    continue
                seen.add(key)
                lines.append(json.dumps(item.to_record(), ensure_ascii=False))
                written += 1
            if lines:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")

        return WriteResult(written=written, skipped_duplicate=duplicate)

    def record_gap(self, market: str, source: str, symbol: str, reason: str,
                   at: datetime | None = None) -> None:
        """수집 실패를 남긴다. 조용히 넘어가면 그날 구멍을 영영 모른다."""
        at = at or datetime.now(timezone.utc)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "at": at.isoformat(), "date": at.strftime("%Y-%m-%d"),
            "market": market, "source": source, "symbol": symbol,
            "reason": reason[:300], "resolved": False,
        }
        with (self.log_dir / "collection_gaps.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- 읽기 ----

    def read_news(self, market: str, day: datetime, source: str | None = None) -> list[dict]:
        folder = self.root / "news" / market / day.strftime("%Y-%m-%d")
        if not folder.exists():
            return []
        files = [folder / f"{source}.jsonl"] if source else sorted(folder.glob("*.jsonl"))
        rows = []
        for path in files:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def open_gaps(self) -> list[dict]:
        path = self.log_dir / "collection_gaps.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if not r.get("resolved")]

    # ---- 내부 ----

    def _existing_hashes(self, path: Path) -> set[tuple[str, str]]:
        if not path.exists():
            return set()
        seen = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            seen.add((row.get("symbol", ""), row.get("content_hash", "")))
        return seen
