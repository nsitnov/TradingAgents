from datetime import date, timedelta

from tradingagents.dashboard.scanner import (
    CrossMarketScanner,
    DislocationRequest,
    ScannerEventRequest,
)
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


class FakeCloseProvider:
    def history(self, symbol, start, end):
        base = {
            "2330.TW": 100.0,
            "TSM": 50.0,
            "NVDA": 80.0,
            "AMD": 60.0,
            "AAPL": 180.0,
            "SMH": 120.0,
        }.get(symbol, 100.0)
        rows = []
        current = start
        value = base
        while current <= end:
            if current.weekday() < 5:
                if current == date(2026, 1, 5):
                    value *= 1.08 if symbol == "2330.TW" else 1.01
                else:
                    wiggle = 0.0005 if current.day % 2 == 0 else -0.0004
                    symbol_bias = 0.0002 if symbol == "2330.TW" else -0.0001
                    value *= 1.001 + wiggle + symbol_bias
                rows.append((current, value))
            current += timedelta(days=1)
        return rows


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


def test_dislocation_detector_scores_target_lag(tmp_path):
    storage = _storage(tmp_path)
    scanner = CrossMarketScanner(storage, price_provider=FakeCloseProvider())
    result = scanner.scan_event(
        ScannerEventRequest(
            title="TSMC raises guidance after strong AI chip demand",
            summary="Taiwan Semiconductor reports record growth.",
            source="manual",
            region="ASIA",
            url="https://example.test/tsmc-dislocation",
            published_at="2026-01-05T08:00:00+00:00",
        )
    )
    tsmc_signal = next(
        signal for signal in result["signals"] if signal["entity"] == "TSMC"
    )

    detection = scanner.detect_dislocations(
        DislocationRequest(
            signal_ids=[tsmc_signal["signal_id"]],
            lookback_days=20,
            z_threshold=1.0,
            min_abs_gap_pct=0.005,
        )
    )

    assert detection["errors"] == []
    assert detection["dislocations"]
    nvda = next(
        row for row in detection["dislocations"] if row["target_symbol"] == "NVDA"
    )
    assert nvda["reference_symbol"] == "2330.TW"
    assert nvda["gap_pct"] > 0.05
    assert nvda["is_dislocated"] is True
    assert storage.scanner_dislocations()[0]["is_dislocated"] is True
