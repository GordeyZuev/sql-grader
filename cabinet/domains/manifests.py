"""Strict JSON homework manifest validation and legacy course conversion."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cabinet.domains.scoring import PenaltyPolicy, parse_deadline
from cabinet.domains.sql_safety import inspect_sql


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    statement: str
    points: Decimal
    required: tuple[str, ...]
    hints: tuple[str, ...]
    reference_sql: str


@dataclass(frozen=True)
class Homework:
    id: str
    title: str
    order: int
    soft_deadline: str
    hard_deadline: str
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    timezone: str
    hard_deadline: str | None
    policy: PenaltyPolicy
    homeworks: tuple[Homework, ...]


def _required(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ManifestError(f"{where}: missing {key}")
    return obj[key]


def _task_title(task: dict[str, Any], task_id: str, statement: str) -> str:
    title = str(task.get("title", task_id)).strip()
    if title and title != task_id:
        return title
    first_line = next((line for line in statement.splitlines() if line.strip()), task_id)
    plain_text = re.sub(r"`([^`]+)`", r"\1", first_line)
    plain_text = plain_text.replace("**", "").replace("__", "")
    plain_text = re.sub(r"\s+", " ", plain_text).strip()
    if not plain_text:
        return task_id
    return plain_text if len(plain_text) <= 78 else f"{plain_text[:75].rstrip()}…"


def validate_manifest(raw: Any) -> Manifest:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be an object")
    version = _required(raw, "schema_version", "manifest")
    if version != 1:
        raise ManifestError("schema_version must be 1")
    timezone = _required(raw, "timezone", "manifest")
    hard = raw.get("hard_deadline")
    if hard is not None:
        if not isinstance(hard, str):
            raise ManifestError("manifest.hard_deadline must be an ISO-8601 string")
        try:
            parse_deadline(hard, timezone)
        except (ValueError, KeyError) as exc:
            raise ManifestError(f"invalid hard_deadline/timezone: {exc}") from exc

    p = raw.get("penalty", {})
    if not isinstance(p, dict):
        raise ManifestError("penalty must be an object")
    try:
        policy = PenaltyPolicy(
            period_days=int(p.get("period_days", 7)),
            period_multiplier=Decimal(str(p.get("period_multiplier", "0.8"))),
            minimum_score_fraction=Decimal(str(p.get("minimum_score_fraction", "0.6"))),
            round_digits=int(p.get("round_digits", 2)),
            formula=str(p.get("formula", "score * multiplier ** (late_days / period_days)")),
            max_penalty_fraction=Decimal(str(p.get("max_penalty_fraction", "0.4"))),
            rounding_mode=str(p.get("rounding_mode", "half_up")),
        )
    except (ValueError, InvalidOperation) as exc:
        raise ManifestError(f"invalid penalty: {exc}") from exc

    raw_hws = _required(raw, "homeworks", "manifest")
    if not isinstance(raw_hws, list) or not raw_hws:
        raise ManifestError("homeworks must be a non-empty array")
    hws: list[Homework] = []
    ids: set[str] = set()
    for index, hw in enumerate(raw_hws):
        where = f"homeworks[{index}]"
        if not isinstance(hw, dict):
            raise ManifestError(f"{where} must be an object")
        hw_id = str(_required(hw, "id", where))
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}", hw_id):
            raise ManifestError(f"{where}.id must use letters, digits, dot, underscore or hyphen")
        if hw_id in ids:
            raise ManifestError(f"duplicate homework id: {hw_id}")
        ids.add(hw_id)
        try:
            soft = _required(hw, "soft_deadline", where)
            soft_at = parse_deadline(soft, timezone)
        except (ValueError, KeyError) as exc:
            raise ManifestError(f"{where}: invalid soft_deadline: {exc}") from exc
        try:
            hw_hard_deadline = hw.get("hard_deadline", hard)
            if hw_hard_deadline is None:
                raise ValueError("set a homework hard_deadline or a manifest hard_deadline")
            if not isinstance(hw_hard_deadline, str):
                raise ValueError("must be an ISO-8601 string")
            hw_hard_at = parse_deadline(hw_hard_deadline, timezone)
        except (ValueError, KeyError, TypeError) as exc:
            raise ManifestError(f"{where}: invalid hard_deadline: {exc}") from exc
        if soft_at > hw_hard_at:
            raise ManifestError(f"{where}.soft_deadline must not be after its hard_deadline")
        raw_tasks = _required(hw, "tasks", where)
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ManifestError(f"{where}.tasks must be a non-empty array")
        tasks: list[Task] = []
        task_ids: set[str] = set()
        for ti, task in enumerate(raw_tasks):
            tw = f"{where}.tasks[{ti}]"
            if not isinstance(task, dict):
                raise ManifestError(f"{tw} must be an object")
            tid = str(_required(task, "id", tw))
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}", tid):
                raise ManifestError(f"{tw}.id must use letters, digits, dot, underscore or hyphen")
            if tid in task_ids:
                raise ManifestError(f"{where}: duplicate task id {tid}")
            task_ids.add(tid)
            try:
                points = Decimal(str(_required(task, "points", tw)))
            except InvalidOperation as exc:
                raise ManifestError(f"{tw}.points must be numeric") from exc
            if not points.is_finite() or points <= 0:
                raise ManifestError(f"{tw}.points must be positive and finite")
            statement = _required(task, "statement", tw)
            ref = _required(task, "reference_sql", tw)
            if not isinstance(statement, str) or not statement.strip():
                raise ManifestError(f"{tw}.statement must be non-empty Markdown")
            if not isinstance(ref, str):
                raise ManifestError(f"{tw}.reference_sql must be text")
            _, sql_error = inspect_sql(ref)
            if sql_error:
                raise ManifestError(f"{tw}.reference_sql is unsafe: {sql_error}")
            required, hints = task.get("required", []), task.get("hints", [])
            if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
                raise ManifestError(f"{tw}.required must be an array of strings")
            if any(
                x
                not in {
                    "cte",
                    "subquery",
                    "subquery_from",
                    "join",
                    "inner_join",
                    "left_join",
                    "right_join",
                    "full_join",
                    "cross_join",
                    "exists",
                    "is_null",
                    "using",
                    "except",
                    "except_all",
                    "except_distinct",
                    "intersect_distinct",
                    "intersect_all",
                    "union_distinct",
                    "union_all",
                    "two_joins",
                    "two_left_joins",
                    "three_inner_joins",
                    "over",
                    "partition_by",
                    "rows_between",
                    "row_number",
                    "rank",
                    "dense_rank",
                    "lag",
                    "first_value",
                    "last_value",
                    "ntile",
                    "percent_rank",
                    "cume_dist",
                    "avg_over",
                }
                for x in required
            ):
                raise ManifestError(f"{tw}.required contains an unknown SQL structure key")
            if not isinstance(hints, list) or not all(isinstance(x, str) for x in hints):
                raise ManifestError(f"{tw}.hints must be an array of strings")
            tasks.append(
                Task(
                    tid,
                    _task_title(task, tid, statement),
                    statement,
                    points,
                    tuple(required),
                    tuple(hints),
                    ref,
                )
            )
        hws.append(
            Homework(
                hw_id,
                str(hw.get("title", hw_id)),
                int(hw.get("order", index)),
                soft,
                str(hw_hard_deadline),
                tuple(tasks),
            )
        )
    return Manifest(version, timezone, hard, policy, tuple(sorted(hws, key=lambda x: x.order)))


def load_manifest(path: Path) -> Manifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(str(exc)) from exc
    return validate_manifest(raw)


def convert_legacy_zip(
    data: bytes, timezone: str, hard_deadline: str, soft_deadline: str
) -> dict[str, Any]:
    """Convert a ZIP containing hwN/{README.md,gold.sql,structure.json} to v1."""
    if len(data) > 20_000_000:
        raise ManifestError("ZIP archive exceeds the 20 MB upload limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ManifestError("file is not a valid ZIP archive") from exc
    files: dict[str, bytes] = {}
    total = 0
    if len(archive.infolist()) > 5000:
        raise ManifestError("ZIP contains more than 5000 files")
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise ManifestError("ZIP contains an unsafe path")
        total += info.file_size
        if total > 100_000_000:
            raise ManifestError("ZIP expands beyond the 100 MB limit")
        files[name] = archive.read(info)
    roots = sorted(
        {name.split("/", 1)[0] for name in files if re.fullmatch(r"hw\d+", name.split("/", 1)[0])}
    )
    if not roots:
        raise ManifestError("ZIP must contain hwN/ folders")
    homeworks = []
    for order, root in enumerate(roots, 1):
        prefix = root + "/"
        try:
            readme = files[prefix + "README.md"].decode("utf-8-sig")
            gold = files[prefix + "gold.sql"].decode("utf-8-sig")
        except (KeyError, UnicodeDecodeError) as exc:
            raise ManifestError(f"{root}: requires UTF-8 README.md and gold.sql") from exc
        structure = {}
        if prefix + "structure.json" in files:
            try:
                structure = json.loads(files[prefix + "structure.json"])
            except json.JSONDecodeError as exc:
                raise ManifestError(f"{root}/structure.json is invalid JSON") from exc
        blocks: dict[str, str] = {}
        markers = list(re.finditer(r"(?m)^--\s*>>>\s*Q(\d+)\s*$", gold))
        for index, marker in enumerate(markers):
            tid = f"{int(marker.group(1)):02d}"
            end = markers[index + 1].start() if index + 1 < len(markers) else len(gold)
            block = gold[marker.end() : end].strip()
            block = re.sub(r"\A(?:\s|--[^\n]*(?:\n|$))*", "", block)
            if block:
                blocks[tid] = block.rstrip().rstrip(";").strip()
        tasks = []
        headings = list(re.finditer(r"(?m)^###\s+Q(\d+)(?:\s*,\s*([^\n]+))?\s*$", readme))
        for index, heading in enumerate(headings):
            tid = f"{int(heading.group(1)):02d}"
            end = headings[index + 1].start() if index + 1 < len(headings) else len(readme)
            body = readme[heading.end() : end].strip()
            if tid not in blocks:
                raise ManifestError(f"{root}: gold.sql has no query marker for Q{tid}")
            heading_points = re.search(
                r"(\d+(?:[,.]\d+)?)\s*(?:балл|бонус)", heading.group(2) or "", re.I
            )
            raw_points = heading_points.group(1).replace(",", ".") if heading_points else "1"
            requirement = structure.get(tid, structure.get(int(tid), {}))
            hints = (
                [requirement["hint"]]
                if isinstance(requirement, dict) and requirement.get("hint")
                else []
            )
            tasks.append(
                {
                    "id": f"Q{tid}",
                    "title": f"Q{tid}",
                    "statement": body,
                    "points": raw_points,
                    "required": requirement.get("require", [])
                    if isinstance(requirement, dict)
                    else [],
                    "hints": hints,
                    "reference_sql": blocks[tid],
                }
            )
        if not tasks:
            raise ManifestError(f"{root}: no ### QNN task headings found")
        homeworks.append(
            {
                "id": root,
                "title": f"Домашнее задание {root[2:]}",
                "order": order,
                "soft_deadline": soft_deadline,
                "tasks": tasks,
            }
        )
    result = {
        "schema_version": 1,
        "timezone": timezone,
        "hard_deadline": hard_deadline,
        "penalty": {
            "period_days": 7,
            "period_multiplier": 0.8,
            "minimum_score_fraction": 0.6,
            "round_digits": 2,
        },
        "homeworks": homeworks,
    }
    validate_manifest(result)
    return result
