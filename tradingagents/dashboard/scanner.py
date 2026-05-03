from __future__ import annotations

import hashlib
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

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


RULES = [
    CrossMarketRule("ASML", ["asml", "eindhoven", "lithography"], "EU", ["ASML", "SMH", "NVDA", "AMD"], "semiconductors"),
    CrossMarketRule("TSMC", ["tsmc", "taiwan semiconductor", "2330.tw"], "ASIA", ["TSM", "NVDA", "AMD", "AAPL", "SMH"], "semiconductors"),
    CrossMarketRule("Toyota", ["toyota", "7203.t"], "ASIA", ["TM", "CARZ"], "autos"),
    CrossMarketRule("Sony", ["sony", "6758.t"], "ASIA", ["SONY", "EWJ"], "consumer_electronics"),
    CrossMarketRule("SAP", ["sap ", "sap.de"], "EU", ["SAP", "IGV"], "software"),
    CrossMarketRule("Novo Nordisk", ["novo nordisk", "novob"], "EU", ["NVO", "XLV"], "healthcare"),
    CrossMarketRule("OPEC/Oil", ["opec", "saudi", "brent", "wti", "oil output"], "ME", ["XLE", "XOP", "OIH", "USO"], "energy"),
    CrossMarketRule("BoJ/JPY", ["boj", "bank of japan", "yen", "jpy"], "ASIA", ["FXY", "EWJ", "SPY", "QQQ"], "macro_fx"),
    CrossMarketRule("ECB/EUR", ["ecb", "european central bank", "euro", "eur/usd"], "EU", ["FXE", "VGK", "FEZ"], "macro_fx"),
    CrossMarketRule("China Tech", ["pboc", "china", "hong kong", "hang seng", "alibaba", "tencent"], "ASIA", ["FXI", "KWEB", "BABA", "JD"], "china"),
    CrossMarketRule("Global Semis", ["chip", "chips", "semiconductor", "semiconductors", "foundry"], "GLOBAL", ["SMH", "SOXX", "NVDA", "AMD"], "semiconductors"),
]


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


class CrossMarketScanner:
    def __init__(self, storage: DashboardStorage) -> None:
        self.storage = storage

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
                }
                for rule in RULES
            ],
            "positive_terms": sorted(POSITIVE_TERMS),
            "negative_terms": sorted(NEGATIVE_TERMS),
        }

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
