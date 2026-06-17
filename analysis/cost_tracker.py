"""
Cost tracker — accumulates token usage across all Claude calls in a run.
Raises CostCeilingExceeded if the configured USD ceiling is breached.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_ANALYSIS_CFG = _CFG["analysis"]
_PRICING = _ANALYSIS_CFG["pricing"]
DEFAULT_CEILING = _ANALYSIS_CFG["cost_ceiling_per_run"]


class CostCeilingExceeded(RuntimeError):
    pass


def _get_pricing(model: str) -> dict:
    """Return pricing dict for the model, falling back to sonnet rates."""
    for key, rates in _PRICING.items():
        if key in model:
            return rates
    # default fallback
    return _PRICING.get("claude-sonnet-4-6", {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_write_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    })


def _cost_usd(usage: anthropic.types.Usage, model: str) -> float:
    rates = _get_pricing(model)
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read  = getattr(usage, "cache_read_input_tokens", 0) or 0
    regular_in  = usage.input_tokens
    out         = usage.output_tokens

    cost = (
        regular_in  / 1_000_000 * rates["input_per_mtok"]
        + out       / 1_000_000 * rates["output_per_mtok"]
        + cache_write / 1_000_000 * rates["cache_write_per_mtok"]
        + cache_read  / 1_000_000 * rates["cache_read_per_mtok"]
    )
    return cost


@dataclass
class CostTracker:
    ceiling: float = DEFAULT_CEILING
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    call_count: int = 0
    _call_log: list = field(default_factory=list)

    def record(self, usage: anthropic.types.Usage, model: str, label: str = ""):
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read  = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = _cost_usd(usage, model)

        self.total_input_tokens       += usage.input_tokens
        self.total_output_tokens      += usage.output_tokens
        self.total_cache_write_tokens += cache_write
        self.total_cache_read_tokens  += cache_read
        self.total_cost_usd           += cost
        self.call_count               += 1

        self._call_log.append({
            "label": label,
            "model": model,
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_write": cache_write,
            "cache_read": cache_read,
            "cost_usd": round(cost, 6),
        })

        logger.debug(
            f"[cost] {label} ${cost:.4f} "
            f"(in={usage.input_tokens} out={usage.output_tokens} "
            f"cw={cache_write} cr={cache_read}) "
            f"running=${self.total_cost_usd:.4f}"
        )

        if self.total_cost_usd > self.ceiling:
            raise CostCeilingExceeded(
                f"Cost ceiling ${self.ceiling:.2f} exceeded: "
                f"actual ${self.total_cost_usd:.4f} after {self.call_count} calls"
            )

    def estimate_cost(self, input_tokens: int, model: str) -> float:
        """Rough cost estimate for a planned call (output assumed ~500 tokens)."""
        rates = _get_pricing(model)
        return (
            input_tokens / 1_000_000 * rates["input_per_mtok"]
            + 500         / 1_000_000 * rates["output_per_mtok"]
        )

    def summary(self) -> dict:
        return {
            "call_count":              self.call_count,
            "total_input_tokens":      self.total_input_tokens,
            "total_output_tokens":     self.total_output_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cost_usd":          round(self.total_cost_usd, 4),
            "ceiling_usd":             self.ceiling,
            "remaining_usd":           round(self.ceiling - self.total_cost_usd, 4),
        }

    def log_summary(self):
        s = self.summary()
        logger.info(
            f"Cost summary: {s['call_count']} calls | "
            f"${s['total_cost_usd']:.4f} / ${s['ceiling_usd']:.2f} ceiling | "
            f"cache_hits={s['total_cache_read_tokens']:,} tokens saved"
        )
