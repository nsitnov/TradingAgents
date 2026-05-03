from tradingagents.dashboard.costs import parse_costs_payload


def test_parse_openai_costs_payload():
    costs = parse_costs_payload(
        {
            "data": [
                {
                    "start_time": 1777776000,
                    "end_time": 1777862400,
                    "results": [
                        {
                            "amount": {"value": 1.25, "currency": "usd"},
                            "line_item": "Responses API",
                            "project_id": "proj_123",
                        }
                    ],
                }
            ]
        }
    )

    assert costs == [
        {
            "start_time": 1777776000,
            "end_time": 1777862400,
            "amount": 1.25,
            "currency": "usd",
            "line_item": "Responses API",
            "project_id": "proj_123",
            "payload": {
                "amount": {"value": 1.25, "currency": "usd"},
                "line_item": "Responses API",
                "project_id": "proj_123",
            },
        }
    ]
