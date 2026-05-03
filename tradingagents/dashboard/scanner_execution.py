from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.oms import OrderIntent, PaperOrderService, RiskConfig
from tradingagents.dashboard.storage import DashboardStorage, now_iso


class ScannerExecutionRequest(BaseModel):
    review_ids: Optional[List[str]] = None
    limit: int = Field(default=25, ge=1, le=100)
    trade_date: Optional[date] = None
    allow_reexecute: bool = False


class ScannerPaperExecutor:
    def __init__(
        self,
        *,
        storage: DashboardStorage,
        ledger: PaperLedger,
        risk_config: Optional[RiskConfig] = None,
    ) -> None:
        self.storage = storage
        self.ledger = ledger
        self.risk_config = risk_config

    def execute(self, request: ScannerExecutionRequest) -> Dict[str, Any]:
        reviews = self._selected_reviews(request)
        order_service = PaperOrderService(
            ledger=self.ledger,
            storage=self.storage,
            risk_config=self.risk_config,
        )
        results = []
        errors = []
        for review in reviews:
            try:
                results.append(self._execute_one(review, request, order_service))
            except Exception as exc:
                errors.append(
                    {
                        "review_id": review.get("review_id"),
                        "target_symbol": review.get("target_symbol"),
                        "error": str(exc),
                    }
                )
        return {"executions": results, "errors": errors}

    def _selected_reviews(self, request: ScannerExecutionRequest) -> List[Dict[str, Any]]:
        if not request.review_ids:
            return [
                review
                for review in self.storage.scanner_confluence_reviews(limit=request.limit)
                if review.get("status") == "paper_candidate"
            ]
        selected = []
        for review_id in request.review_ids:
            review = self.storage.scanner_confluence_review_detail(review_id)
            if review:
                selected.append(review)
        return selected

    def _execute_one(
        self,
        review: Dict[str, Any],
        request: ScannerExecutionRequest,
        order_service: PaperOrderService,
    ) -> Dict[str, Any]:
        if review.get("status") != "paper_candidate":
            return self._skipped(review, f"Review status is {review.get('status')}")
        if review.get("execution") and not request.allow_reexecute:
            return self._skipped(review, "Review already has a paper execution")

        decision = str(review.get("action", "")).strip()
        if decision not in {"Buy", "Sell", "Overweight", "Underweight"}:
            return self._skipped(review, f"Action {decision or 'none'} is not executable")

        trade_date = (
            request.trade_date.isoformat()
            if request.trade_date
            else _review_trade_date(review)
        )
        result = order_service.submit_decision(
            OrderIntent(
                run_id=f"scanner-confluence-{review['review_id']}",
                ticker=review["target_symbol"],
                decision=decision,
                trade_date=trade_date,
                source="scanner_confluence",
            )
        )
        execution = {
            "status": result["order"]["status"],
            "order_id": result["order"]["order_id"],
            "risk_status": (result.get("risk") or {}).get("status"),
            "risk_reason": (result.get("risk") or {}).get("reason"),
            "trade_date": trade_date,
            "executed_at": now_iso(),
        }
        updated = dict(review)
        updated["execution"] = execution
        updated["execution_status"] = execution["status"]
        self.storage.upsert_scanner_confluence_review(updated)
        return {
            "review_id": review["review_id"],
            "target_symbol": review["target_symbol"],
            "action": decision,
            "execution": execution,
            "order": result["order"],
            "risk": result.get("risk"),
        }

    def _skipped(self, review: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "review_id": review.get("review_id"),
            "target_symbol": review.get("target_symbol"),
            "action": review.get("action"),
            "execution": {"status": "skipped", "reason": reason},
        }


def _review_trade_date(review: Dict[str, Any]) -> str:
    created_at = str(review.get("created_at") or "")
    if len(created_at) >= 10:
        return created_at[:10]
    return date.today().isoformat()
