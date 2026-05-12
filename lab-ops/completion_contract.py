def validate_contract(contract: dict) -> dict:
    before = contract.get("target_metric_before") or {}
    after = contract.get("target_metric_after") or {}
    metric_changed = before != after
    errors = []
    status = contract.get("status")
    if status == "closed" and not metric_changed:
        errors.append("closed_without_metric_change")
    if status == "partial" and not contract.get("next_owner"):
        errors.append("partial_missing_next_owner")
    return {"valid": not errors, "errors": errors, "metric_changed": metric_changed}
