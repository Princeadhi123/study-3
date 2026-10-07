"""Serialize active views without publishing archived calibration outputs."""
import copy


def active_payload(value):
    """Filter a copy, leaving saved historical evidence untouched."""
    if isinstance(value, dict):
        return {key: active_payload(item) for key, item in value.items()
                if "conformal" not in key.lower()
                and "calibration" not in key.lower()
                and key not in ("midpoint_coverage", "end_coverage")}
    if isinstance(value, list):
        return [active_payload(item) for item in value]
    return copy.deepcopy(value)
