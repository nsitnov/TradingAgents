from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from tradingagents.dashboard.storage import DashboardStorage, now_iso


LIQUID_PROXY_SYMBOLS = {
    "AAPL",
    "AMD",
    "ASML",
    "BABA",
    "FXE",
    "FXI",
    "FXY",
    "IGV",
    "JD",
    "NVDA",
    "NVO",
    "OIH",
    "QQQ",
    "SAP",
    "SMH",
    "SONY",
    "SOXX",
    "SPY",
    "TM",
    "TSM",
    "USO",
    "VGK",
    "XLE",
    "XLV",
    "XOP",
}


class ConfluenceRequest(BaseModel):
    dislocation_ids: Optional[List[str]] = None
    limit: int = Field(default=50, ge=1, le=250)
    min_z_score: float = Field(default=1.5, ge=0.0, le=10.0)
    min_abs_gap_pct: float = Field(default=0.005, ge=0.0, le=1.0)
    min_total_score: float = Field(default=0.65, ge=0.0, le=1.0)


class ScannerConfluenceReviewer:
    def __init__(self, storage: DashboardStorage) -> None:
        self.storage = storage

    def review(self, request: ConfluenceRequest) -> Dict[str, Any]:
        dislocations = self._selected_dislocations(request)
        reviews = []
        errors = []
        for dislocation in dislocations:
            signal = self.storage.scanner_signal_detail(dislocation["signal_id"])
            event = self.storage.scanner_event_detail(int(dislocation["event_id"]))
            if not signal or not event:
                errors.append(
                    {
                        "dislocation_id": dislocation["dislocation_id"],
                        "error": "Missing scanner signal or event",
                    }
                )
                continue
            review = self._review_one(dislocation, signal, event, request)
            self.storage.upsert_scanner_confluence_review(review)
            reviews.append(review)
        return {"reviews": reviews, "errors": errors}

    def reviews(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.storage.scanner_confluence_reviews(limit=limit)

    def _selected_dislocations(self, request: ConfluenceRequest) -> List[Dict[str, Any]]:
        if not request.dislocation_ids:
            return self.storage.scanner_dislocations(limit=request.limit)
        selected = []
        for dislocation_id in request.dislocation_ids:
            item = self.storage.scanner_dislocation_detail(dislocation_id)
            if item:
                selected.append(item)
        return selected

    def _review_one(
        self,
        dislocation: Dict[str, Any],
        signal: Dict[str, Any],
        event: Dict[str, Any],
        request: ConfluenceRequest,
    ) -> Dict[str, Any]:
        agent_reviews = [
            _quant_validator(dislocation, request),
            _news_mapper(signal, event),
            _liquidity_proxy(dislocation),
            _risk_gate(dislocation),
        ]
        total_score = sum(item["score"] for item in agent_reviews) / len(agent_reviews)
        blockers = [item for item in agent_reviews if not item["passed"] and item["blocking"]]
        action = _candidate_action(dislocation)
        status = "paper_candidate"
        if blockers:
            status = "rejected"
            action = "none"
        elif total_score < request.min_total_score:
            status = "watch"
            action = "watch"
        elif action == "watch":
            status = "watch"

        review_id = _stable_id("confluence", dislocation["dislocation_id"], dislocation["target_symbol"])
        return {
            "review_id": review_id,
            "dislocation_id": dislocation["dislocation_id"],
            "signal_id": dislocation["signal_id"],
            "event_id": dislocation["event_id"],
            "target_symbol": dislocation["target_symbol"],
            "entity": dislocation["entity"],
            "status": status,
            "action": action,
            "total_score": round(total_score, 4),
            "agent_reviews": agent_reviews,
            "candidate": {
                "ticker": dislocation["target_symbol"],
                "action": action,
                "source": "scanner_confluence",
                "paper_only": True,
                "reason": f"{dislocation['entity']} {dislocation['direction']} z={float(dislocation['z_score']):.2f}",
            },
            "created_at": now_iso(),
        }


def _quant_validator(dislocation: Dict[str, Any], request: ConfluenceRequest) -> Dict[str, Any]:
    z_score = abs(float(dislocation.get("z_score", 0.0)))
    gap = abs(float(dislocation.get("gap_pct", 0.0)))
    passed = bool(dislocation.get("is_dislocated")) and z_score >= request.min_z_score and gap >= request.min_abs_gap_pct
    score = min(1.0, (z_score / max(request.min_z_score, 0.1)) * 0.45 + (gap / max(request.min_abs_gap_pct, 0.001)) * 0.20)
    return {
        "agent": "Quantitative Validator",
        "passed": passed,
        "blocking": True,
        "score": round(score, 4),
        "reason": f"z={z_score:.2f}, gap={gap:.2%}",
    }


def _news_mapper(signal: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    confidence = float(signal.get("confidence", 0.0))
    event_text = f"{event.get('title', '')} {event.get('summary', '')}".strip()
    passed = bool(event_text) and confidence >= 0.45
    return {
        "agent": "News Mapper",
        "passed": passed,
        "blocking": False,
        "score": round(min(1.0, confidence), 4),
        "reason": signal.get("reason", "Scanner mapping confidence"),
    }


def _liquidity_proxy(dislocation: Dict[str, Any]) -> Dict[str, Any]:
    target = str(dislocation.get("target_symbol", "")).upper()
    passed = target in LIQUID_PROXY_SYMBOLS
    return {
        "agent": "Liquidity Proxy",
        "passed": passed,
        "blocking": False,
        "score": 0.9 if passed else 0.55,
        "reason": "Known liquid proxy" if passed else "Unknown liquidity profile",
    }


def _risk_gate(dislocation: Dict[str, Any]) -> Dict[str, Any]:
    target = str(dislocation.get("target_symbol", "")).upper()
    forbidden = {
        item.strip().upper()
        for item in os.getenv("TRADINGAGENTS_FORBIDDEN_TICKERS", "").split(",")
        if item.strip()
    }
    passed = target not in forbidden and str(dislocation.get("direction")) != "target_overreacted"
    reason = "Risk gate passed"
    if target in forbidden:
        reason = "Target is forbidden by risk config"
    elif str(dislocation.get("direction")) == "target_overreacted":
        reason = "Overreaction signals require manual review"
    return {
        "agent": "Hard Risk Gate",
        "passed": passed,
        "blocking": True,
        "score": 1.0 if passed else 0.0,
        "reason": reason,
    }


def _candidate_action(dislocation: Dict[str, Any]) -> str:
    direction = str(dislocation.get("direction", ""))
    if direction == "target_lagging_upside":
        return "Buy"
    if direction == "target_lagging_downside":
        return "Sell"
    return "watch"


def _stable_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
