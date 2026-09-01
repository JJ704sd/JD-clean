"""Infer local-only candidate metadata from trusted filename conventions."""

from __future__ import annotations

import re
from pathlib import Path

# Longest and most explicit labels come first.  These are filename prefixes,
# not keyword matches in arbitrary positions: a resume whose name merely
# contains a role word remains unlabeled and is not auto-routed.
ROLE_HINTS = (
    ("全栈开发实习生", "fullstack-development-intern"),
    ("全栈开发实习", "fullstack-development-intern"),
    ("全栈实习生", "fullstack-development-intern"),
    ("全栈实习", "fullstack-development-intern"),
    ("full-stack intern", "fullstack-development-intern"),
    ("fullstack intern", "fullstack-development-intern"),
    ("资深全栈工程师", "senior-fullstack-engineer"),
    ("资深全栈", "senior-fullstack-engineer"),
    ("高级全栈工程师", "senior-fullstack-engineer"),
    ("高级全栈", "senior-fullstack-engineer"),
    ("全栈工程师", "senior-fullstack-engineer"),
    ("全栈开发工程师", "senior-fullstack-engineer"),
    ("senior full-stack", "senior-fullstack-engineer"),
    ("senior fullstack", "senior-fullstack-engineer"),
    ("ai产品经理", "ai-product-manager"),
    ("ai 产品经理", "ai-product-manager"),
    ("ai product manager", "ai-product-manager"),
)
BOSS_NAME_RE = re.compile(
    r"^【[^】]+】(?P<name>.+?)(?:\s+(?:\d+年(?:以上)?|应届生))?$",
    re.IGNORECASE,
)
GENERIC_RESUME_RE = re.compile(
    r"^(?P<name>[\u4e00-\u9fffA-Za-z·]{2,40})的简历(?:[-_].*)?$",
    re.IGNORECASE,
)
COPY_SUFFIX_RE = re.compile(r"\(\d+\)$")


def infer_role(path: str | Path) -> str | None:
    """Return a role only when the filename starts with an approved role label."""

    value = Path(path).stem.strip().casefold()
    value = re.sub(r"^[\s【\[\(（《<]+", "", value)
    for hint, role in ROLE_HINTS:
        if value.startswith(hint.casefold()):
            return role
    return None


def infer_candidate_name(path: str | Path) -> str | None:
    """Extract a local display name without treating arbitrary documents as resumes."""

    stem = COPY_SUFFIX_RE.sub("", Path(path).stem).strip()
    for pattern in (BOSS_NAME_RE, GENERIC_RESUME_RE):
        match = pattern.fullmatch(stem)
        if match:
            value = " ".join(match.group("name").split()).strip("-_ ")
            return value[:80] if value else None
    return None
