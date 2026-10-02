"""Work-item type hierarchy, modelled on Jira Software.

    Epic                                  level 1: groups work, never planned into a sprint
    Story, Task, Bug, Requirement         level 0: "standard" issues, ranked and planned
    Sub-task                              level -1: always the child of one standard issue

Task (a standard issue) and Sub-task (a piece of a standard issue) are
different types. A Sub-task has no rank or sprint of its own: it follows its
parent (app.agile.integrity keeps that true on every write path).
"""
from __future__ import annotations

EPIC = "Epic"
SUBTASK = "Sub-task"
STANDARD_TYPES: tuple[str, ...] = ("Story", "Task", "Bug", "Requirement")


def is_standard(item_type: str | None) -> bool:
    return (item_type or "Bug") in STANDARD_TYPES


def is_subtask(item_type: str | None) -> bool:
    return item_type == SUBTASK


def is_epic(item_type: str | None) -> bool:
    return item_type == EPIC

