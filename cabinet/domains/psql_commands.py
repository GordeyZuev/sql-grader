"""Small, read-only subset of psql backslash commands for schema discovery."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass


class PsqlCommandError(ValueError):
    """A psql-style command is unsupported or malformed."""


@dataclass(frozen=True)
class PsqlCommand:
    name: str
    detailed: bool = False
    pattern: str | None = None


_COMMAND = re.compile(
    r"\\(?P<name>dt|dv|dm|di|ds|dn|d)(?P<plus>\+)?(?:\s+(?P<pattern>[A-Za-z0-9_$.*]+(?:\.[A-Za-z0-9_$.*]+)?))?\s*",
    re.IGNORECASE,
)


def parse_psql_command(source: str) -> PsqlCommand | None:
    """Parse a single supported command; return None when input is ordinary SQL."""
    stripped = source.strip()
    if not stripped.startswith("\\"):
        return None
    match = _COMMAND.fullmatch(stripped)
    if not match:
        raise PsqlCommandError(
            "Команды: \\d [имя], \\d+ [имя], \\dt [маска], \\dv [маска], \\dm [маска], \\di [маска], \\ds, \\dn"
        )
    name = match.group("name").lower()
    if match.group("plus") and name not in {"d", "dt", "dv", "dn"}:
        raise PsqlCommandError("Для этой psql-команды расширенный режим не поддерживается")
    return PsqlCommand(name=name, detailed=bool(match.group("plus")), pattern=match.group("pattern"))


def relation_kinds(command: PsqlCommand) -> tuple[str, ...]:
    if command.name == "dt":
        return ("r", "p", "f")
    if command.name == "dv":
        return ("v",)
    if command.name == "dm":
        return ("m",)
    if command.name == "di":
        return ("i", "I")
    if command.name == "ds":
        return ("S",)
    return ("r", "p", "v", "m", "f", "S", "i", "I")


def matches_pattern(value: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return fnmatch.fnmatchcase(value, pattern)
