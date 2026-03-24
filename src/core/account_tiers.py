"""Account tier and model capability helpers."""

from typing import Optional


PAYGATE_TIER_NOT_PAID = "PAYGATE_TIER_NOT_PAID"
PAYGATE_TIER_ONE = "PAYGATE_TIER_ONE"
PAYGATE_TIER_TWO = "PAYGATE_TIER_TWO"


def normalize_user_paygate_tier(user_paygate_tier: Optional[str]) -> str:
    """Normalize an account tier, defaulting unknown values to free tier."""
    normalized = (user_paygate_tier or "").strip()
    if normalized in {PAYGATE_TIER_NOT_PAID, PAYGATE_TIER_ONE, PAYGATE_TIER_TWO}:
        return normalized
    return PAYGATE_TIER_NOT_PAID


def get_paygate_tier_rank(user_paygate_tier: Optional[str]) -> int:
    """Map account tier to a comparable rank."""
    normalized = normalize_user_paygate_tier(user_paygate_tier)
    if normalized == PAYGATE_TIER_TWO:
        return 2
    if normalized == PAYGATE_TIER_ONE:
        return 1
    return 0


def get_paygate_tier_label(user_paygate_tier: Optional[str]) -> str:
    """Return a readable account tier label."""
    normalized = normalize_user_paygate_tier(user_paygate_tier)
    if normalized == PAYGATE_TIER_TWO:
        return "Ult"
    if normalized == PAYGATE_TIER_ONE:
        return "Pro"
    return "Free"


def get_required_paygate_tier_for_model(model_name: Optional[str]) -> str:
    """Infer the minimum required account tier from a model name."""
    normalized = (model_name or "").strip().lower()
    if not normalized:
        return PAYGATE_TIER_NOT_PAID

    if normalized.endswith("-4k") or normalized.endswith("_4k") or "_ultra" in normalized:
        return PAYGATE_TIER_TWO

    if normalized.endswith("-2k") or normalized.endswith("_1080p"):
        return PAYGATE_TIER_ONE

    return PAYGATE_TIER_NOT_PAID


def supports_model_for_tier(model_name: Optional[str], user_paygate_tier: Optional[str]) -> bool:
    """Check whether the current account tier can use the given model."""
    required_tier = get_required_paygate_tier_for_model(model_name)
    return get_paygate_tier_rank(user_paygate_tier) >= get_paygate_tier_rank(required_tier)
