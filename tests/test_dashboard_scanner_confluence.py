from tradingagents.dashboard.scanner_confluence import (
    ConfluenceRequest,
    ScannerConfluenceReviewer,
)
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def _seed_dislocation(storage):
    event = {
        "event_hash": "event-1",
        "source": "manual",
        "region": "ASIA",
        "title": "TSMC raises guidance",
        "summary": "Strong AI chip demand.",
        "url": "https://example.test/tsmc",
        "published_at": "2026-01-05T08:00:00+00:00",
        "language": "en",
        "created_at": "2026-01-05T08:00:00+00:00",
    }
    event_id = storage.upsert_scanner_event(event)
    signal = {
        "signal_id": "signal-1",
        "event_id": event_id,
        "event_hash": "event-1",
        "entity": "TSMC",
        "region": "ASIA",
        "category": "semiconductors",
        "us_targets": ["NVDA"],
        "direction": "bullish",
        "score": 0.85,
        "confidence": 0.75,
        "reason": "Matched TSMC",
        "created_at": "2026-01-05T08:00:01+00:00",
    }
    storage.upsert_scanner_signal(signal)
    dislocation = {
        "dislocation_id": "dislocation-1",
        "signal_id": "signal-1",
        "event_id": event_id,
        "entity": "TSMC",
        "reference_symbol": "2330.TW",
        "target_symbol": "NVDA",
        "reference_move_pct": 0.08,
        "target_move_pct": 0.01,
        "gap_pct": 0.07,
        "z_score": 2.5,
        "spread_mean": 0.0,
        "spread_std": 0.02,
        "lookback_days": 20,
        "is_dislocated": True,
        "direction": "target_lagging_upside",
        "created_at": "2026-01-05T08:00:02+00:00",
    }
    storage.upsert_scanner_dislocation(dislocation)
    return dislocation


def test_confluence_reviewer_creates_paper_candidate(tmp_path):
    storage = _storage(tmp_path)
    dislocation = _seed_dislocation(storage)
    reviewer = ScannerConfluenceReviewer(storage)

    result = reviewer.review(
        ConfluenceRequest(dislocation_ids=[dislocation["dislocation_id"]])
    )

    assert result["errors"] == []
    review = result["reviews"][0]
    assert review["status"] == "paper_candidate"
    assert review["action"] == "Buy"
    assert review["candidate"]["paper_only"] is True
    assert {item["agent"] for item in review["agent_reviews"]} == {
        "Quantitative Validator",
        "News Mapper",
        "Liquidity Proxy",
        "Hard Risk Gate",
    }
    assert storage.scanner_confluence_reviews()[0]["status"] == "paper_candidate"


def test_confluence_reviewer_rejects_non_dislocation(tmp_path):
    storage = _storage(tmp_path)
    dislocation = _seed_dislocation(storage)
    stored = storage.scanner_dislocation_detail(dislocation["dislocation_id"])
    stored["is_dislocated"] = False
    stored["z_score"] = 0.2
    storage.upsert_scanner_dislocation(stored)

    result = ScannerConfluenceReviewer(storage).review(
        ConfluenceRequest(dislocation_ids=[dislocation["dislocation_id"]])
    )

    assert result["reviews"][0]["status"] == "rejected"
    assert result["reviews"][0]["action"] == "none"
