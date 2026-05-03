from __future__ import annotations

import hashlib
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import sqrt
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from pydantic import BaseModel, Field, field_validator

from tradingagents.dashboard.storage import DashboardStorage, now_iso


POSITIVE_TERMS = {
    "beat",
    "beats",
    "raise",
    "raises",
    "raised",
    "upgrade",
    "upgrades",
    "approval",
    "approved",
    "surge",
    "jumps",
    "record",
    "strong",
    "growth",
    "buyback",
}
NEGATIVE_TERMS = {
    "miss",
    "misses",
    "cut",
    "cuts",
    "downgrade",
    "downgrades",
    "warning",
    "probe",
    "sanction",
    "sanctions",
    "recall",
    "falls",
    "plunge",
    "weak",
    "lawsuit",
    "ban",
}


@dataclass(frozen=True)
class CrossMarketRule:
    entity: str
    keywords: List[str]
    region: str
    us_targets: List[str]
    category: str
    default_direction: str = "watch"
    reference_symbol: Optional[str] = None


RULES = [
    CrossMarketRule("ASML", ["asml", "eindhoven", "lithography"], "EU", ["ASML", "SMH", "NVDA", "AMD"], "semiconductors", reference_symbol="ASML.AS"),
    CrossMarketRule("TSMC", ["tsmc", "taiwan semiconductor", "2330.tw"], "ASIA", ["TSM", "NVDA", "AMD", "AAPL", "SMH"], "semiconductors", reference_symbol="2330.TW"),
    CrossMarketRule("Toyota", ["toyota", "7203.t"], "ASIA", ["TM", "CARZ"], "autos", reference_symbol="7203.T"),
    CrossMarketRule("Sony", ["sony", "6758.t"], "ASIA", ["SONY", "EWJ"], "consumer_electronics", reference_symbol="6758.T"),
    CrossMarketRule("SAP", ["sap ", "sap.de"], "EU", ["SAP", "IGV"], "software", reference_symbol="SAP.DE"),
    CrossMarketRule("Novo Nordisk", ["novo nordisk", "novob"], "EU", ["NVO", "XLV"], "healthcare", reference_symbol="NOVO-B.CO"),
    CrossMarketRule("OPEC/Oil", ["opec", "saudi", "brent", "wti", "oil output"], "ME", ["XLE", "XOP", "OIH", "USO"], "energy", reference_symbol="USO"),
    CrossMarketRule("BoJ/JPY", ["boj", "bank of japan", "yen", "jpy"], "ASIA", ["FXY", "EWJ", "SPY", "QQQ"], "macro_fx", reference_symbol="FXY"),
    CrossMarketRule("ECB/EUR", ["ecb", "european central bank", "euro", "eur/usd"], "EU", ["FXE", "VGK", "FEZ"], "macro_fx", reference_symbol="FXE"),
    CrossMarketRule("China Tech", ["pboc", "china", "hong kong", "hang seng", "alibaba", "tencent"], "ASIA", ["FXI", "KWEB", "BABA", "JD"], "china", reference_symbol="2800.HK"),
    CrossMarketRule("Global Semis", ["chip", "chips", "semiconductor", "semiconductors", "foundry"], "GLOBAL", ["SMH", "SOXX", "NVDA", "AMD"], "semiconductors", reference_symbol="SMH"),
]

RULE_BY_ENTITY = {rule.entity: rule for rule in RULES}


class ScannerEventRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=5000)
    source: str = Field(default="manual", max_length=120)
    region: str = Field(default="GLOBAL", max_length=24)
    url: Optional[str] = Field(default=None, max_length=1000)
    published_at: Optional[str] = None
    language: str = Field(default="en", max_length=16)

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value: str) -> str:
        return value.strip().upper() or "GLOBAL"


class RSSIngestRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=20)
    source: str = Field(default="rss", max_length=120)
    region: str = Field(default="GLOBAL", max_length=24)
    limit_per_feed: int = Field(default=20, ge=1, le=100)

    @field_validator("region")
    @classmethod
    def normalize_region(cls, value: str) -> str:
        return value.strip().upper() or "GLOBAL"


class DislocationRequest(BaseModel):
    signal_ids: Optional[List[str]] = None
    lookback_days: int = Field(default=60, ge=10, le=500)
    z_threshold: float = Field(default=1.5, ge=0.0, le=10.0)
    min_abs_gap_pct: float = Field(default=0.005, ge=0.0, le=1.0)
    limit: int = Field(default=50, ge=1, le=250)


class HistoricalCloseProvider(Protocol):
    def history(self, symbol: str, start: date, end: date) -> List[Tuple[date, float]]:
        ...


class YFinanceCloseProvider:
    def history(self, symbol: str, start: date, end: date) -> List[Tuple[date, float]]:
        import yfinance as yf

        frame = yf.Ticker(symbol).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
        )
        if frame.empty or "Close" not in frame:
            raise ValueError(f"No historical close prices for {symbol}")
        closes = frame["Close"].dropna()
        return [(index.date(), float(value)) for index, value in closes.items()]


class CrossMarketScanner:
    def __init__(
        self,
        storage: DashboardStorage,
        price_provider: Optional[HistoricalCloseProvider] = None,
    ) -> None:
        self.storage = storage
        self.price_provider = price_provider or YFinanceCloseProvider()

    def scan_event(self, request: ScannerEventRequest) -> Dict[str, Any]:
        event = _event_from_request(request)
        event_id = self.storage.upsert_scanner_event(event)
        event["event_id"] = event_id
        signals = [self._signal_for_match(event, match) for match in _extract_matches(event)]
        for signal in signals:
            self.storage.upsert_scanner_signal(signal)
        return {"event": event, "signals": signals}

    def ingest_rss(self, request: RSSIngestRequest) -> Dict[str, Any]:
        results = []
        for url in request.urls:
            for item in _fetch_rss(url, limit=request.limit_per_feed):
                event_request = ScannerEventRequest(
                    title=item["title"],
                    summary=item.get("summary", ""),
                    source=request.source,
                    region=request.region,
                    url=item.get("url") or url,
                    published_at=item.get("published_at"),
                )
                results.append(self.scan_event(event_request))
        signal_count = sum(len(item["signals"]) for item in results)
        return {"event_count": len(results), "signal_count": signal_count, "results": results}

    def signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.storage.scanner_signals(limit=limit)

    def events(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.storage.scanner_events(limit=limit)

    def config(self) -> Dict[str, Any]:
        return {
            "rules": [
                {
                    "entity": rule.entity,
                    "region": rule.region,
                    "category": rule.category,
                    "us_targets": rule.us_targets,
                    "keywords": rule.keywords,
                    "reference_symbol": rule.reference_symbol,
                }
                for rule in RULES
            ],
            "positive_terms": sorted(POSITIVE_TERMS),
            "negative_terms": sorted(NEGATIVE_TERMS),
        }

    def detect_dislocations(self, request: DislocationRequest) -> Dict[str, Any]:
        signals = self._signals_for_detection(request)
        rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for signal in signals:
            event = self.storage.scanner_event_detail(int(signal["event_id"]))
            if not event:
                errors.append({"signal_id": signal["signal_id"], "error": "Missing scanner event"})
                continue
            try:
                rows.extend(self._detect_signal_dislocations(signal, event, request))
            except Exception as exc:
                errors.append({"signal_id": signal["signal_id"], "error": str(exc)})
        for row in rows:
            self.storage.upsert_scanner_dislocation(row)
        return {"dislocations": rows, "errors": errors}

    def dislocations(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.storage.scanner_dislocations(limit=limit)

    def _signal_for_match(self, event: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, Any]:
        sentiment = _sentiment(event["title"], event.get("summary", ""))
        direction = _direction(sentiment, match["rule"].default_direction)
        score = _score(match["confidence"], sentiment)
        target = ",".join(match["rule"].us_targets)
        signal_id = _stable_id("signal", event["event_hash"], match["rule"].entity, target)
        return {
            "signal_id": signal_id,
            "event_id": event["event_id"],
            "event_hash": event["event_hash"],
            "entity": match["rule"].entity,
            "region": match["rule"].region,
            "category": match["rule"].category,
            "us_targets": match["rule"].us_targets,
            "direction": direction,
            "score": score,
            "confidence": match["confidence"],
            "reason": match["reason"],
            "created_at": now_iso(),
        }

    def _signals_for_detection(self, request: DislocationRequest) -> List[Dict[str, Any]]:
        if not request.signal_ids:
            return self.storage.scanner_signals(limit=request.limit)
        selected = []
        for signal_id in request.signal_ids:
            signal = self.storage.scanner_signal_detail(signal_id)
            if signal:
                selected.append(signal)
        return selected

    def _detect_signal_dislocations(
        self,
        signal: Dict[str, Any],
        event: Dict[str, Any],
        request: DislocationRequest,
    ) -> List[Dict[str, Any]]:
        rule = RULE_BY_ENTITY.get(signal["entity"])
        if not rule:
            return []
        reference = rule.reference_symbol or signal["us_targets"][0]
        event_day = _date_from_iso(event.get("published_at") or signal.get("created_at"))
        start = event_day - timedelta(days=request.lookback_days * 2 + 10)
        reference_history = self.price_provider.history(reference, start, event_day)
        reference_returns = _daily_returns(reference_history)
        reference_move = _latest_return_on_or_before(reference_returns, event_day)
        rows = []
        for target in signal.get("us_targets", []):
            if target == reference:
                continue
            target_history = self.price_provider.history(target, start, event_day)
            target_returns = _daily_returns(target_history)
            target_move = _latest_return_on_or_before(target_returns, event_day)
            spread_history = _aligned_spreads(reference_returns, target_returns, event_day)
            lookback = spread_history[-request.lookback_days :]
            spread_mean = _mean(lookback)
            spread_std = _stdev(lookback)
            gap = reference_move - target_move
            z_score = (gap - spread_mean) / spread_std if spread_std else 0.0
            is_dislocated = (
                abs(z_score) >= request.z_threshold
                and abs(gap) >= request.min_abs_gap_pct
            )
            dislocation_id = _stable_id("dislocation", signal["signal_id"], reference, target)
            rows.append(
                {
                    "dislocation_id": dislocation_id,
                    "signal_id": signal["signal_id"],
                    "event_id": signal["event_id"],
                    "entity": signal["entity"],
                    "reference_symbol": reference,
                    "target_symbol": target,
                    "reference_move_pct": reference_move,
                    "target_move_pct": target_move,
                    "gap_pct": gap,
                    "z_score": z_score,
                    "spread_mean": spread_mean,
                    "spread_std": spread_std,
                    "lookback_days": len(lookback),
                    "is_dislocated": is_dislocated,
                    "direction": _dislocation_direction(gap, signal.get("direction", "watch")),
                    "created_at": now_iso(),
                }
            )
        return rows


def _event_from_request(request: ScannerEventRequest) -> Dict[str, Any]:
    published_at = request.published_at or now_iso()
    event_hash = _stable_id(
        "event",
        request.source,
        request.url or "",
        request.title,
        published_at[:10],
    )
    return {
        "event_hash": event_hash,
        "source": request.source.strip() or "manual",
        "region": request.region,
        "title": request.title.strip(),
        "summary": request.summary.strip(),
        "url": request.url,
        "published_at": _normalize_datetime(published_at),
        "language": request.language,
        "created_at": now_iso(),
    }


def _extract_matches(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = _normalize_text(f"{event.get('title', '')} {event.get('summary', '')}")
    matches = []
    for rule in RULES:
        hits = [keyword for keyword in rule.keywords if _keyword_hit(text, keyword)]
        if not hits:
            continue
        confidence = min(1.0, 0.45 + 0.15 * len(hits))
        matches.append(
            {
                "rule": rule,
                "confidence": confidence,
                "reason": f"Matched {rule.entity}: {', '.join(hits[:5])}",
            }
        )
    return matches


def _sentiment(title: str, summary: str) -> float:
    words = set(re.findall(r"[a-z0-9./-]+", _normalize_text(f"{title} {summary}")))
    positive = len(words & POSITIVE_TERMS)
    negative = len(words & NEGATIVE_TERMS)
    if positive == negative:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / max(positive + negative, 1)))


def _direction(sentiment: float, default: str) -> str:
    if sentiment > 0.15:
        return "bullish"
    if sentiment < -0.15:
        return "bearish"
    return default


def _score(confidence: float, sentiment: float) -> float:
    return round(min(1.0, max(0.0, confidence + abs(sentiment) * 0.25)), 4)


def _fetch_rss(url: str, *, limit: int) -> List[Dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=10) as response:
        raw = response.read(2_000_000)
    root = ET.fromstring(raw)
    items = []
    nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for node in nodes[:limit]:
        title = _node_text(node, "title")
        if not title:
            continue
        link = _node_text(node, "link") or _node_attr(node, "link", "href")
        published = (
            _node_text(node, "pubDate")
            or _node_text(node, "published")
            or _node_text(node, "updated")
        )
        items.append(
            {
                "title": title,
                "summary": _node_text(node, "description") or _node_text(node, "summary"),
                "url": link,
                "published_at": _normalize_datetime(published) if published else now_iso(),
            }
        )
    return items


def _node_text(node: ET.Element, name: str) -> str:
    child = node.find(name) or node.find(f"{{http://www.w3.org/2005/Atom}}{name}")
    return "".join(child.itertext()).strip() if child is not None else ""


def _node_attr(node: ET.Element, name: str, attr: str) -> str:
    child = node.find(name) or node.find(f"{{http://www.w3.org/2005/Atom}}{name}")
    return child.attrib.get(attr, "").strip() if child is not None else ""


def _normalize_datetime(raw: str) -> str:
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except Exception:
        return now_iso()


def _date_from_iso(raw: Optional[str]) -> date:
    if not raw:
        return datetime.now(timezone.utc).date()
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()


def _daily_returns(history: Iterable[Tuple[date, float]]) -> Dict[date, float]:
    rows = sorted((day, float(close)) for day, close in history if float(close) > 0)
    returns: Dict[date, float] = {}
    for (previous_day, previous_close), (current_day, current_close) in zip(rows, rows[1:]):
        if previous_close:
            returns[current_day] = (current_close - previous_close) / previous_close
    return returns


def _latest_return_on_or_before(returns: Dict[date, float], day: date) -> float:
    candidates = [return_day for return_day in returns if return_day <= day]
    if not candidates:
        raise ValueError(f"No return data on or before {day}")
    return returns[max(candidates)]


def _aligned_spreads(
    reference_returns: Dict[date, float],
    target_returns: Dict[date, float],
    before_day: date,
) -> List[float]:
    days = sorted(
        day
        for day in set(reference_returns) & set(target_returns)
        if day < before_day
    )
    return [reference_returns[day] - target_returns[day] for day in days]


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance)


def _dislocation_direction(gap: float, signal_direction: str) -> str:
    if signal_direction == "bearish":
        return "target_lagging_downside" if gap < 0 else "target_overreacted"
    if signal_direction == "bullish":
        return "target_lagging_upside" if gap > 0 else "target_overreacted"
    return "target_lagging_reference" if gap > 0 else "target_ahead_of_reference"


def _keyword_hit(text: str, keyword: str) -> bool:
    normalized = _normalize_text(keyword)
    if normalized.endswith(" "):
        return normalized in text
    return re.search(rf"(^|[^a-z0-9]){re.escape(normalized)}([^a-z0-9]|$)", text) is not None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _stable_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
