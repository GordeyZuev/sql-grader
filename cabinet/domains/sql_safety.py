"""Conservative SQL statement gate for the read-only learning connection."""

from __future__ import annotations

import re

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|copy|grant|revoke|call|do|vacuum|analyze|refresh|comment|prepare|execute)\b",
    re.IGNORECASE,
)


def inspect_sql(sql: str) -> tuple[str, str | None]:
    """Return executable SQL with literals/comments masked and a validation error."""
    chars = list(sql)
    i, n = 0, len(sql)
    semicolons: list[int] = []
    state = "code"
    dollar_end = ""
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "'":
                chars[i] = " "
                state = "single"
            elif ch == '"':
                chars[i] = " "
                state = "double"
            elif ch == "-" and nxt == "-":
                chars[i : i + 2] = [" ", " "]
                i += 1
                state = "line"
            elif ch == "/" and nxt == "*":
                chars[i : i + 2] = [" ", " "]
                i += 1
                state = "block"
            elif ch == "$":
                match = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
                if match:
                    dollar_end = match.group()
                    chars[i : i + len(dollar_end)] = [" "] * len(dollar_end)
                    i += len(dollar_end) - 1
                    state = "dollar"
            elif ch == ";":
                semicolons.append(i)
        elif state == "single":
            chars[i] = " "
            if ch == "'":
                if nxt == "'":
                    chars[i + 1] = " "
                    i += 1
                else:
                    state = "code"
            elif ch == "\\" and nxt:
                chars[i + 1] = " "
                i += 1
        elif state == "double":
            chars[i] = " "
            if ch == '"':
                if nxt == '"':
                    chars[i + 1] = " "
                    i += 1
                else:
                    state = "code"
        elif state == "line":
            chars[i] = " "
            if ch == "\n":
                state = "code"
        elif state == "block":
            chars[i] = " "
            if ch == "*" and nxt == "/":
                chars[i + 1] = " "
                i += 1
                state = "code"
        elif state == "dollar":
            if sql.startswith(dollar_end, i):
                chars[i : i + len(dollar_end)] = [" "] * len(dollar_end)
                i += len(dollar_end) - 1
                state = "code"
            else:
                chars[i] = " "
        i += 1

    code = "".join(chars).strip()
    if state in {"single", "double", "block", "dollar"}:
        return code, "SQL содержит незакрытую строку или комментарий"
    if not code:
        return code, "Запрос пустой"
    if semicolons:
        tail = "".join(chars[semicolons[-1] + 1 :]).strip()
        if len(semicolons) > 1 or tail:
            return code, "Разрешён один SQL-запрос за раз"
    if not re.match(r"\A(?:select|with)\b", code, re.IGNORECASE):
        return code, "Разрешены только запросы SELECT"
    if FORBIDDEN.search(code):
        return code, "В запросе обнаружена запрещённая команда"
    return code, None
