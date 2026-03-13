"""
v0.2.5: Pre-flight cost estimation.

Two-layer estimation:
1. Static estimator — deterministic, free, based on model pricing + max_turns + prompt
2. Model-assisted — optional Haiku call for gray-zone tasks (within 80% of budget cap)

Safety rule: effective_estimate = max(static, model_max) — model can only make
the system more cautious, never less.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Model pricing (USD per 1M tokens, as of 2026-03) ─────────────────────

_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}

# Fallback for unknown models — use Sonnet pricing as a safe default
_DEFAULT_PRICING = {"input": 3.00, "output": 15.00}

# ── Average token counts per turn (empirical estimates) ───────────────────

_AVG_INPUT_TOKENS_PER_TURN = 4000  # context grows over turns
_AVG_OUTPUT_TOKENS_PER_TURN = 1500
_UTILIZATION_FACTOR = 0.6  # most tasks don't use all turns

# ── Addon costs (Haiku calls for AI features) ────────────────────────────

_HAIKU_CALL_COST_USD = 0.002  # ~500 input + 200 output tokens at Haiku pricing


@dataclass
class BudgetEstimate:
    """Result of a pre-flight cost estimation."""

    predicted_input_cost_usd: float = 0.0
    predicted_output_cost_usd: float = 0.0
    predicted_total_usd: float = 0.0
    confidence: str = "medium"  # "low", "medium", "high"
    reason_codes: list[str] = field(default_factory=list)
    mode: str = "static"  # "static" or "model_assisted"
    # Model-assisted fields (populated only when model is used)
    model_predicted_total_usd: float | None = None
    model_min_usd: float | None = None
    model_max_usd: float | None = None


def estimate_static(
    *,
    model: str,
    max_turns: int,
    prompt_length: int,
    has_review: bool = False,
    has_risk_summary: bool = False,
    has_ai_pr_title: bool = False,
    has_ai_pr_summary: bool = False,
) -> BudgetEstimate:
    """Static cost estimator using model pricing and task metadata.

    Uses only information available at task submission time:
    - Model → exact per-token pricing
    - max_turns → strongest cost predictor
    - Prompt length → first-turn input tokens
    - Addon calls → review, AI PR title/summary (each is a Haiku call)
    """
    pricing = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
    input_price_per_token = pricing["input"] / 1_000_000
    output_price_per_token = pricing["output"] / 1_000_000

    # Base cost: per-turn cost * expected turns used
    effective_turns = max_turns * _UTILIZATION_FACTOR
    input_cost = input_price_per_token * _AVG_INPUT_TOKENS_PER_TURN * effective_turns
    output_cost = output_price_per_token * _AVG_OUTPUT_TOKENS_PER_TURN * effective_turns

    # Add first-turn prompt cost (above the average)
    prompt_tokens = prompt_length // 4  # rough chars-to-tokens
    if prompt_tokens > _AVG_INPUT_TOKENS_PER_TURN:
        extra_input = (prompt_tokens - _AVG_INPUT_TOKENS_PER_TURN) * input_price_per_token
        input_cost += extra_input

    # Addon costs (each is a separate Haiku API call)
    addon_count = 0
    reason_codes: list[str] = []

    if has_review:
        addon_count += 1
        reason_codes.append("review_mode_enabled")
    if has_ai_pr_title:
        addon_count += 1
        reason_codes.append("ai_pr_title_enabled")
    if has_ai_pr_summary:
        addon_count += 1
        reason_codes.append("ai_pr_summary_enabled")
    # Risk summary is local computation, no API cost
    if has_risk_summary:
        reason_codes.append("risk_summary_enabled")

    addon_cost = addon_count * _HAIKU_CALL_COST_USD

    total = input_cost + output_cost + addon_cost

    # Confidence based on predictability
    if max_turns <= 10 and prompt_tokens < 2000:
        confidence = "high"
    elif max_turns <= 30:
        confidence = "medium"
    else:
        confidence = "low"
        reason_codes.append("high_turn_count")

    if prompt_tokens > 10000:
        reason_codes.append("prompt_large")

    if model in _MODEL_PRICING:
        reason_codes.append(f"model_{model.split('-')[1]}")
    else:
        reason_codes.append("model_unknown_pricing")
        confidence = "low"

    return BudgetEstimate(
        predicted_input_cost_usd=round(input_cost, 4),
        predicted_output_cost_usd=round(output_cost, 4),
        predicted_total_usd=round(total, 4),
        confidence=confidence,
        reason_codes=reason_codes,
        mode="static",
    )


async def estimate_model_assisted(
    *,
    prompt: str,
    model: str,
    max_turns: int,
    static_estimate: BudgetEstimate,
    anthropic_api_key: str,
) -> BudgetEstimate | None:
    """Model-assisted estimation using Haiku to reason about task complexity.

    Only called when the static estimate is in the "gray zone" (within 80%
    of the budget cap). Returns None on any failure — callers fall back to
    the static estimate.

    The safety rule is enforced by the caller:
    effective = max(static.predicted_total_usd, model.model_max_usd)
    """
    if not anthropic_api_key:
        return None

    try:
        import httpx

        prompt_excerpt = prompt[:2000]
        if len(prompt) > 2000:
            prompt_excerpt += "\n... (truncated)"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "You are a cost estimator for an AI coding agent. "
                                "Estimate how many turns (API round-trips) this task "
                                "will actually use, given a maximum of "
                                f"{max_turns} turns.\n\n"
                                "Respond in JSON only:\n"
                                '{"estimated_turns": N, "min_turns": N, '
                                '"max_turns": N, "confidence": "low|medium|high", '
                                '"reasoning": "one sentence"}\n\n'
                                f"Model: {model}\n"
                                f"Max turns allowed: {max_turns}\n"
                                f"Task prompt: {prompt_excerpt}"
                            ),
                        }
                    ],
                },
            )

        if resp.status_code != 200:
            logger.info("Model-assisted estimation failed: HTTP %d", resp.status_code)
            return None

        data = resp.json()
        content = data.get("content", [])
        if not content or content[0].get("type") != "text":
            return None

        import json
        import re

        text = content[0]["text"].strip()
        # Handle markdown code fences
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)

        result = json.loads(text)

        est_turns = result.get("estimated_turns", max_turns)
        min_turns = result.get("min_turns", est_turns)
        max_turns_est = result.get("max_turns", est_turns)
        model_confidence = result.get("confidence", "low")

        # Calculate costs using the model's turn estimates
        pricing = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
        input_price = pricing["input"] / 1_000_000
        output_price = pricing["output"] / 1_000_000

        def _cost_for_turns(turns: float) -> float:
            return (
                input_price * _AVG_INPUT_TOKENS_PER_TURN * turns
                + output_price * _AVG_OUTPUT_TOKENS_PER_TURN * turns
            )

        model_total = _cost_for_turns(est_turns)
        model_min = _cost_for_turns(min_turns)
        model_max = _cost_for_turns(max_turns_est)

        # Build the model-assisted estimate
        # Use the static estimate's addon costs (model doesn't know about those)
        addon_cost = static_estimate.predicted_total_usd - (
            static_estimate.predicted_input_cost_usd + static_estimate.predicted_output_cost_usd
        )

        estimate = BudgetEstimate(
            predicted_input_cost_usd=round(input_price * _AVG_INPUT_TOKENS_PER_TURN * est_turns, 4),
            predicted_output_cost_usd=round(
                output_price * _AVG_OUTPUT_TOKENS_PER_TURN * est_turns, 4
            ),
            predicted_total_usd=round(model_total + addon_cost, 4),
            confidence=model_confidence,
            reason_codes=static_estimate.reason_codes + ["model_assisted"],
            mode="model_assisted",
            model_predicted_total_usd=round(model_total + addon_cost, 4),
            model_min_usd=round(model_min + addon_cost, 4),
            model_max_usd=round(model_max + addon_cost, 4),
        )

        return estimate

    except Exception as exc:
        logger.info("Model-assisted estimation failed: %s", exc)
        return None


def apply_safety_rule(static: BudgetEstimate, model: BudgetEstimate | None) -> BudgetEstimate:
    """Apply the safety rule: effective = max(static, model_max).

    If model-assisted estimation produced a result, the effective estimate
    uses the more conservative (higher) of the two. The model can only make
    the system more cautious, never less safe.
    """
    if model is None or model.model_max_usd is None:
        return static

    if model.model_max_usd > static.predicted_total_usd:
        # Model says it could cost more — use the model's max
        return BudgetEstimate(
            predicted_input_cost_usd=model.predicted_input_cost_usd,
            predicted_output_cost_usd=model.predicted_output_cost_usd,
            predicted_total_usd=round(model.model_max_usd, 4),
            confidence=model.confidence,
            reason_codes=model.reason_codes + ["safety_rule_model_max"],
            mode="model_assisted",
            model_predicted_total_usd=model.model_predicted_total_usd,
            model_min_usd=model.model_min_usd,
            model_max_usd=model.model_max_usd,
        )
    else:
        # Static is more conservative — keep it
        return BudgetEstimate(
            predicted_input_cost_usd=static.predicted_input_cost_usd,
            predicted_output_cost_usd=static.predicted_output_cost_usd,
            predicted_total_usd=static.predicted_total_usd,
            confidence=static.confidence,
            reason_codes=static.reason_codes + ["safety_rule_static_higher"],
            mode="model_assisted",
            model_predicted_total_usd=model.model_predicted_total_usd,
            model_min_usd=model.model_min_usd,
            model_max_usd=model.model_max_usd,
        )


async def run_budget_check(
    *,
    model: str,
    max_turns: int,
    prompt: str,
    max_budget_usd: float | None,
    budget_fail_policy: str = "reject",
    anthropic_api_key: str = "",
    has_review: bool = False,
    has_ai_pr_title: bool = False,
    has_ai_pr_summary: bool = False,
) -> dict[str, Any]:
    """Run the full pre-flight budget check pipeline.

    Returns a budget_check dict to include in the task creation response.
    If max_budget_usd is None, returns a minimal "skipped" result.
    """
    if max_budget_usd is None:
        return {"status": "skipped", "reason": "no budget cap set"}

    # Step 1: Static estimate (includes addon costs when flags are set)
    static = estimate_static(
        model=model,
        max_turns=max_turns,
        prompt_length=len(prompt),
        has_review=has_review,
        has_ai_pr_title=has_ai_pr_title,
        has_ai_pr_summary=has_ai_pr_summary,
    )

    effective = static

    # Step 2: Model-assisted if in the gray zone (within 80% of cap)
    gray_zone_threshold = max_budget_usd * 0.8
    if static.predicted_total_usd >= gray_zone_threshold and anthropic_api_key:
        logger.info(
            "Static estimate $%.4f is within 80%% of budget $%.4f — "
            "running model-assisted estimation",
            static.predicted_total_usd,
            max_budget_usd,
        )
        model_estimate = await estimate_model_assisted(
            prompt=prompt,
            model=model,
            max_turns=max_turns,
            static_estimate=static,
            anthropic_api_key=anthropic_api_key,
        )
        effective = apply_safety_rule(static, model_estimate)

    # Step 3: Decision
    from dataclasses import asdict

    estimate_data = asdict(effective)

    if effective.predicted_total_usd <= max_budget_usd:
        return {
            "status": "passed",
            "max_budget_usd": max_budget_usd,
            **estimate_data,
        }

    # Budget exceeded — apply fail policy
    if budget_fail_policy == "warn":
        return {
            "status": "warning",
            "max_budget_usd": max_budget_usd,
            "message": (
                f"Predicted cost ${effective.predicted_total_usd:.4f} "
                f"exceeds budget ${max_budget_usd:.2f} — proceeding with warning"
            ),
            **estimate_data,
        }
    elif budget_fail_policy == "require_approval":
        return {
            "status": "requires_approval",
            "max_budget_usd": max_budget_usd,
            "message": (
                f"Predicted cost ${effective.predicted_total_usd:.4f} "
                f"exceeds budget ${max_budget_usd:.2f} — approval required"
            ),
            **estimate_data,
        }
    else:  # "reject"
        return {
            "status": "rejected",
            "max_budget_usd": max_budget_usd,
            "message": (
                f"Predicted cost ${effective.predicted_total_usd:.4f} "
                f"exceeds budget ${max_budget_usd:.2f}"
            ),
            **estimate_data,
        }
