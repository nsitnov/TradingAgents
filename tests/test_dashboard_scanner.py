from tradingagents.dashboard.scanner import CrossMarketScanner, ScannerEventRequest
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def test_scanner_maps_foreign_event_to_us_targets(tmp_path):
    scanner = CrossMarketScanner(_storage(tmp_path))

    result = scanner.scan_event(
        ScannerEventRequest(
            title="TSMC raises guidance after strong AI chip demand",
            summary="Taiwan Semiconductor reports record growth.",
            source="manual",
            region="ASIA",
            url="https://example.test/tsmc",
            published_at="2026-01-05T08:00:00+00:00",
        )
    )

    entities = {signal["entity"] for signal in result["signals"]}
    assert "TSMC" in entities
    assert "Global Semis" in entities
    tsmc = next(signal for signal in result["signals"] if signal["entity"] == "TSMC")
    assert tsmc["direction"] == "bullish"
    assert "NVDA" in tsmc["us_targets"]


def test_scanner_event_ingest_is_idempotent(tmp_path):
    storage = _storage(tmp_path)
    scanner = CrossMarketScanner(storage)
    request = ScannerEventRequest(
        title="OPEC cuts oil output",
        source="rss",
        region="ME",
        url="https://example.test/opec",
        published_at="2026-01-05T08:00:00+00:00",
    )

    first = scanner.scan_event(request)
    second = scanner.scan_event(request)

    assert first["event"]["event_id"] == second["event"]["event_id"]
    assert len(storage.scanner_events()) == 1
    assert len(storage.scanner_signals()) == len(first["signals"])
    assert storage.scanner_signals()[0]["score"] >= 0.45


def test_scanner_ignores_unmapped_event(tmp_path):
    scanner = CrossMarketScanner(_storage(tmp_path))

    result = scanner.scan_event(
        ScannerEventRequest(title="Local sports team wins final", source="manual")
    )

    assert result["signals"] == []
