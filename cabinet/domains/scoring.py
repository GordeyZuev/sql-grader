"""Deterministic grading, deadline and milestone primitives.

The cabinet stores raw achievements. Late penalties are calculated only for
exports, so a later result cannot erase an earlier, more valuable achievement.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class PenaltyPolicy:
    period_days: int = 7
    period_multiplier: Decimal = Decimal("0.8")
    minimum_score_fraction: Decimal = Decimal("0.6")
    round_digits: int = 2
    formula: str = "score * multiplier ** (late_days / period_days)"
    max_penalty_fraction: Decimal = Decimal("0.4")
    rounding_mode: str = "half_up"

    def __post_init__(self) -> None:
        if self.period_days < 1:
            raise ValueError("period_days must be positive")
        if not Decimal(0) < self.period_multiplier <= Decimal(1):
            raise ValueError("period_multiplier must be in (0, 1]")
        if not Decimal(0) <= self.minimum_score_fraction <= Decimal(1):
            raise ValueError("minimum_score_fraction must be in [0, 1]")
        if not Decimal(0) <= self.max_penalty_fraction <= Decimal(1):
            raise ValueError("max_penalty_fraction must be in [0, 1]")
        if not self.formula.strip() or len(self.formula) > 300:
            raise ValueError("formula must contain 1 to 300 characters")
        try:
            formula_tree = ast.parse(self.formula, mode="eval")
        except SyntaxError as exc:
            raise ValueError("formula is not a valid arithmetic expression") from exc
        allowed_nodes = (
            ast.Expression,
            ast.Constant,
            ast.Name,
            ast.BinOp,
            ast.UnaryOp,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Pow,
            ast.UAdd,
            ast.USub,
            ast.Load,
        )
        variables = {"score", "late_days", "period_days", "multiplier", "max_penalty_fraction"}
        if any(not isinstance(node, allowed_nodes) for node in ast.walk(formula_tree)):
            raise ValueError("formula supports arithmetic and named scoring variables only")
        if any(
            isinstance(node, ast.Name) and node.id not in variables
            for node in ast.walk(formula_tree)
        ):
            raise ValueError("formula refers to an unknown variable")
        if any(
            isinstance(node, ast.Constant) and type(node.value) not in (int, float)
            for node in ast.walk(formula_tree)
        ):
            raise ValueError("formula numeric constants must be finite numbers")
        if self.round_digits < 0:
            raise ValueError("round_digits cannot be negative")
        if self.rounding_mode not in {"half_up", "half_even", "down"}:
            raise ValueError("rounding_mode must be half_up, half_even, or down")


def late_days(achieved_at: datetime, soft_deadline: datetime) -> int:
    """Count every started Moscow calendar day after the soft deadline."""
    achieved = achieved_at.astimezone(MOSCOW)
    deadline = soft_deadline.astimezone(MOSCOW)
    if achieved <= deadline:
        return 0
    seconds = (achieved - deadline).total_seconds()
    return int((seconds + 86_399) // 86_400)


def final_score(
    score: Decimal,
    achieved_at: datetime,
    soft_deadline: datetime,
    policy: PenaltyPolicy | None = None,
) -> Decimal:
    policy = policy or PenaltyPolicy()
    days = late_days(achieved_at, soft_deadline)
    raw = _formula_value(
        policy.formula,
        {
            "score": score,
            "late_days": Decimal(days),
            "period_days": Decimal(policy.period_days),
            "multiplier": policy.period_multiplier,
            "max_penalty_fraction": policy.max_penalty_fraction,
        },
    )
    quantum = Decimal(1).scaleb(-policy.round_digits)
    rounding = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN, "down": ROUND_DOWN}[
        policy.rounding_mode
    ]
    penalized = raw.quantize(quantum, rounding=rounding)
    floor = score * max(policy.minimum_score_fraction, Decimal(1) - policy.max_penalty_fraction)
    return max(penalized, floor).quantize(quantum, rounding=rounding)


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _formula_value(expression: str, values: dict[str, Decimal]) -> Decimal:
    """Evaluate arithmetic over named variables with a tiny allowlisted AST."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("formula is not a valid arithmetic expression") from exc

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("formula exponent is too large")
            return _BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](evaluate(node.operand))
        raise ValueError(
            "formula supports only arithmetic and score/late_days/period_days/multiplier/max_penalty_fraction"
        )

    value = evaluate(tree)
    if not value.is_finite() or value < 0:
        raise ValueError("formula must produce a non-negative finite score")
    return value


def grading_open(now: datetime, hard_deadline: datetime) -> bool:
    return now.astimezone(MOSCOW) <= hard_deadline.astimezone(MOSCOW)


@dataclass(frozen=True)
class Achievement:
    student_email: str
    homework: str
    task: str
    points: Decimal
    achieved_at: datetime
    soft_deadline: datetime
    hard_deadline: datetime
    policy: PenaltyPolicy = PenaltyPolicy()

    @property
    def adjusted_points(self) -> Decimal:
        return final_score(self.points, self.achieved_at, self.soft_deadline, self.policy)


def best_adjusted_by_task(
    achievements: Iterable[Achievement],
) -> dict[tuple[str, str, str], Decimal]:
    """Choose the best penalized achievement per student/homework/task."""
    best: dict[tuple[str, str, str], Decimal] = {}
    for item in achievements:
        key = (item.student_email, item.homework, item.task)
        best[key] = max(best.get(key, Decimal(0)), item.adjusted_points)
    return best


def parse_deadline(value: str, timezone: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values use the manifest timezone."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(MOSCOW)
