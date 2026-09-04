"""Version matrix for newly written screening tasks.

The queue deliberately keeps older rows readable, but every new worker task must
match one of the contracts declared here.  Keeping this matrix in one module
prevents the CLI, queue, and pipeline from silently disagreeing about what
"current" means.
"""

from __future__ import annotations

from .cleaning import PARSER_VERSION

SCORING_VERSION = "evidence-score-2026-09-01-v2"
PROMPT_VERSION = "resume-screening-prompt-2026-09-04-v6"

ROLE_VERSIONS: dict[str, tuple[str, str]] = {
    "ai-product-manager": ("ai-pm-2026-08-v2", "ai-pm-rubric-2026-08-18-v3"),
    "senior-fullstack-engineer": (
        "senior-fullstack-2026-08-14-v1",
        "senior-fullstack-2026-09-04-v11",
    ),
    "fullstack-development-intern": (
        "fullstack-intern-2026-08-14-v1",
        "fullstack-intern-2026-08-24-v4",
    ),
}

# A tuple is (jd_version, rubric_version, parser_version, scoring_version,
# prompt_version).  It is intentionally JSON-friendly because the health
# command and the worker lease persist this exact matrix for inspection.
ACTIVE_CONTRACTS: dict[str, tuple[str, str, str, str, str]] = {
    role: (jd, rubric, PARSER_VERSION, SCORING_VERSION, PROMPT_VERSION)
    for role, (jd, rubric) in ROLE_VERSIONS.items()
}


def contract_matches(
    *,
    role: str,
    jd_version: str,
    rubric_version: str,
    parser_version: str,
    scoring_version: str,
    prompt_version: str,
    active_contracts: dict[str, tuple[str, str, str, str, str]] | None = None,
) -> bool:
    contracts = active_contracts or ACTIVE_CONTRACTS
    return contracts.get(role) == (
        jd_version,
        rubric_version,
        parser_version,
        scoring_version,
        prompt_version,
    )
