# ruff: noqa: I001
"""HTTP endpoints for this domain."""

from cabinet.domains.scoring import (
    Achievement,
    PenaltyPolicy,
    best_adjusted_by_task,
)
from datetime import (
    datetime,
)
from decimal import (
    Decimal,
)
from fastapi.exceptions import (
    HTTPException,
)
from fastapi.param_functions import (
    Depends,
)
from cabinet.api import (
    ScorePreviewRequest,
    admin,
)
from cabinet.routers.scoring import router as score_router

@score_router.post("/preview")
def score_preview(request: ScorePreviewRequest, _: str = Depends(admin)):
    try:
        p = request.penalty
        policy = PenaltyPolicy(
            period_days=int(p.get("period_days", 7)),
            period_multiplier=Decimal(str(p.get("period_multiplier", "0.8"))),
            minimum_score_fraction=Decimal(str(p.get("minimum_score_fraction", "0.6"))),
            round_digits=int(p.get("round_digits", 2)),
            formula=str(p.get("formula", "score * multiplier ** (late_days / period_days)")),
            max_penalty_fraction=Decimal(str(p.get("max_penalty_fraction", "0.4"))),
            rounding_mode=str(p.get("rounding_mode", "half_up")),
        )
        rows = [
            Achievement(
                x.student_email,
                x.homework,
                x.task,
                x.points,
                x.achieved_at,
                x.soft_deadline,
                datetime.max.replace(tzinfo=x.achieved_at.tzinfo),
                policy,
            )
            for x in request.achievements
        ]
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    best = best_adjusted_by_task(rows)
    return {
        "milestones": [
            {
                "student_email": x.student_email,
                "homework": x.homework,
                "task": x.task,
                "raw_points": str(x.points),
                "achieved_at": x.achieved_at.isoformat(),
                "soft_deadline": x.soft_deadline.isoformat(),
                "final_points": str(x.adjusted_points),
            }
            for x in rows
        ],
        "best_by_task": [
            {"student_email": k[0], "homework": k[1], "task": k[2], "points": str(v)}
            for k, v in sorted(best.items())
        ],
    }
