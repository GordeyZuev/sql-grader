from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from cabinet.domains.scoring import (
    Achievement,
    PenaltyPolicy,
    best_adjusted_by_task,
    best_adjusted_points,
    final_score,
    late_days,
)


def dt(value):
    return datetime.fromisoformat(value)


def test_started_late_day_boundaries():
    deadline = dt("2026-09-25T23:59:00+03:00")
    assert late_days(deadline, deadline) == 0
    assert late_days(deadline + timedelta(minutes=1), deadline) == 1
    assert late_days(deadline + timedelta(days=7), deadline) == 7
    assert late_days(deadline + timedelta(days=7, seconds=1), deadline) == 8


def test_formula_and_configured_maximum_penalty():
    policy = PenaltyPolicy(
        period_days=7,
        period_multiplier=Decimal("0.8"),
        formula="score * multiplier ** (late_days / period_days)",
        max_penalty_fraction=Decimal("0.4"),
        minimum_score_fraction=Decimal(0),
    )
    deadline = dt("2026-01-01T00:00:00+03:00")
    assert final_score(Decimal(1), deadline, deadline, policy) == Decimal("1.00")
    assert final_score(Decimal(1), deadline + timedelta(days=365), deadline, policy) == Decimal(
        "0.60"
    )


def test_formula_is_editable_but_not_arbitrary_python():
    policy = PenaltyPolicy(formula="score - late_days * 0.1", max_penalty_fraction=Decimal("0.5"))
    deadline = dt("2026-01-01T00:00:00+03:00")
    assert final_score(Decimal(1), deadline + timedelta(days=2), deadline, policy) == Decimal(
        "0.80"
    )
    with pytest.raises(ValueError):
        PenaltyPolicy(formula="__import__('os').system('id')")


def test_best_adjusted_milestone_can_be_an_earlier_raw_result():
    soft = dt("2026-01-01T00:00:00+03:00")
    hard = dt("2026-10-10T23:59:00+03:00")
    milestones = [
        Achievement("s@example.edu", "hw1", "Q01", Decimal("0.8"), soft, soft, hard),
        Achievement(
            "s@example.edu", "hw1", "Q01", Decimal(1), soft + timedelta(days=365), soft, hard
        ),
    ]
    assert best_adjusted_by_task(milestones)[("s@example.edu", "hw1", "Q01")] == Decimal("0.80")


def test_best_adjusted_points_keeps_the_earlier_better_mark():
    policy = PenaltyPolicy(
        formula="score - late_days * 0.1",
        max_penalty_fraction=Decimal("0.8"),
        minimum_score_fraction=Decimal(0),
    )
    soft = dt("2026-01-01T00:00:00+03:00")
    early = (Decimal("0.5"), soft + timedelta(days=1))
    late = (Decimal(1), soft + timedelta(days=7))
    assert final_score(*early, soft, policy) == Decimal("0.40")
    assert final_score(*late, soft, policy) == Decimal("0.30")
    assert best_adjusted_points([], soft, policy) == Decimal(0)
    assert best_adjusted_points([early], soft, policy) == Decimal("0.40")
    assert best_adjusted_points([early, late], soft, policy) == Decimal("0.40")
    assert best_adjusted_points([(Decimal(1), soft)], soft, policy) == Decimal("1.00")
