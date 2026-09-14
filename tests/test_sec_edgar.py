"""EDGAR 어댑터의 시점 정합 로직 검증. 네트워크를 타지 않는다.

여기서 지키는 것은 하나다 — as_of 시점에 공개돼 있지 않던 값이 새어 들어오지 않는 것.
"""
from datetime import date, datetime, timezone

from src.data.sec_edgar import (
    Filing,
    _is_known_by,
    _iter_recent,
    _row_to_filing,
    pick_fact_as_of,
)

UTC = timezone.utc


def _concept_payload(rows):
    return {"units": {"USD": rows}}


# 3월 말 분기 실적이 5월에 공시되고, 이후 8월에 정정된 상황
Q1 = {"start": "2025-01-01", "end": "2025-03-31", "val": 100, "filed": "2025-05-02",
      "form": "10-Q", "accn": "a-1"}
Q1_RESTATED = {"start": "2025-01-01", "end": "2025-03-31", "val": 95, "filed": "2025-08-01",
               "form": "10-Q/A", "accn": "a-2"}
Q2 = {"start": "2025-04-01", "end": "2025-06-30", "val": 120, "filed": "2025-08-01",
      "form": "10-Q", "accn": "a-3"}

ROWS = [Q1, Q1_RESTATED, Q2]


def _pick(as_of: str):
    return pick_fact_as_of(
        _concept_payload(ROWS),
        concept="revenue",
        tag="Revenues",
        as_of=datetime.fromisoformat(as_of).replace(tzinfo=UTC),
    )


def test_공시_전에는_값이_없다():
    # 회계기간은 3월에 끝났지만 공시는 5월 2일이다. 4월에는 알 수 없었다.
    assert _pick("2025-04-15T00:00:00") is None


def test_공시일_이후에만_보인다():
    fact = _pick("2025-05-02T00:00:00")
    assert fact is not None
    assert fact.value == 100
    assert fact.period_end == date(2025, 3, 31)


def test_정정_전에는_원래_값을_쓴다():
    # 6월 시점에서는 8월 정정본을 알 수 없다.
    fact = _pick("2025-06-15T00:00:00")
    assert fact.value == 100
    assert fact.accession == "a-1"


def test_정정_후에는_최신_제출본을_쓴다():
    # 9월 시점: Q2가 가장 최근 기간이고, 그것을 쓴다.
    fact = _pick("2025-09-15T00:00:00")
    assert fact.period_end == date(2025, 6, 30)
    assert fact.value == 120


def test_같은_기간이_여러번_제출되면_나중_제출본():
    only_q1 = pick_fact_as_of(
        _concept_payload([Q1, Q1_RESTATED]),
        concept="revenue",
        tag="Revenues",
        as_of=datetime(2025, 9, 15, tzinfo=UTC),
    )
    assert only_q1.value == 95
    assert only_q1.accession == "a-2"


def test_회계기간_종료일로_자르면_안된다는_것의_반증():
    # end(3/31) 기준이면 4월에 보였어야 한다. filed(5/2) 기준이라 안 보이는 것이 정답.
    assert _pick("2025-04-01T00:00:00") is None
    assert _pick("2025-05-03T00:00:00") is not None


# ---- 접수 타임스탬프 ----

SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-25-000001", "0000320193-25-000002"],
            "filingDate": ["2025-05-02", "2025-05-02"],
            "reportDate": ["2025-03-31", ""],
            "acceptanceDateTime": ["2025-05-02T13:00:00.000Z", "2025-05-02T21:45:00.000Z"],
            "form": ["10-Q", "8-K"],
            "primaryDocument": ["aapl-20250331.htm", "aapl-8k.htm"],
            "items": ["", "2.02"],
        }
    }
}


def test_columnar_구조를_행으로_편다():
    rows = _iter_recent(SUBMISSIONS)
    assert len(rows) == 2
    assert rows[1]["form"] == "8-K"


def test_장마감후_접수분은_당일_장중_판단에_들어가지_않는다():
    rows = [_row_to_filing(r) for r in _iter_recent(SUBMISSIONS)]
    장중, 장마감후 = rows

    # 5월 2일 20:00 UTC = 16:00 ET. 아직 장중이다.
    as_of = datetime(2025, 5, 2, 20, 0, tzinfo=UTC)
    assert _is_known_by(장중, as_of) is True      # 13:00Z 접수분은 이미 공개됨
    assert _is_known_by(장마감후, as_of) is False  # 21:45Z(17:45 ET) 접수분은 아직


def test_접수시각이_없으면_제출일_자정_기준():
    filing = Filing(
        accession="x", form="10-K", filing_date=date(2025, 5, 2), accepted_at=None,
        report_date=None, primary_document="", items="",
    )
    assert _is_known_by(filing, datetime(2025, 5, 1, 23, 59, tzinfo=UTC)) is False
    assert _is_known_by(filing, datetime(2025, 5, 2, 0, 0, tzinfo=UTC)) is True
