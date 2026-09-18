"""Version matrix for newly written screening tasks and desktop releases.

The queue deliberately keeps older rows readable, but every new worker task must
match one of the contracts declared here.  Keeping this matrix in one module
prevents the CLI, queue, and pipeline from silently disagreeing about what
"current" means.
"""

from __future__ import annotations

from .cleaning import PARSER_VERSION

# Keep the preview version stable until the external 0.3.0 delivery gates in
# docs/next-stage-ai-workbench-spec.md have actually been completed.  Desktop
# code and build scripts import this value instead of carrying their own copy.
APP_VERSION = "0.2.0-preview"
TARGET_APP_VERSION = "0.3.0-preview"
DESKTOP_SCHEMA_VERSION = 2
AI_OUTPUT_CONTRACT_VERSION = "resumedesk-ai-result-v1"

SCORING_VERSION = "evidence-score-2026-09-01-v2"
PROMPT_VERSION = "resume-screening-prompt-2026-09-04-v6"

OPERATIONS_DEVOPS_JD_VERSION = "operations-devops-engineer-2026-09-17-draft-v1"
OPERATIONS_DEVOPS_RUBRIC_VERSION = "operations-devops-rubric-2026-09-17-v1"
OPERATIONS_DEVOPS_SCORING_VERSION = "operations-devops-score-2026-09-17-v1"
OPERATIONS_DEVOPS_PROMPT_VERSION = "operations-devops-screening-prompt-2026-09-17-v1"

ROLE_VERSIONS: dict[str, tuple[str, str]] = {
    "ai-product-manager": ("ai-pm-2026-08-v2", "ai-pm-rubric-2026-08-18-v3"),
    "senior-fullstack-engineer": (
        "senior-fullstack-2026-08-14-v1",
        "senior-fullstack-2026-09-14-v14",
    ),
    "fullstack-development-intern": (
        "fullstack-intern-2026-08-14-v1",
        "fullstack-intern-2026-08-24-v4",
    ),
    "operations-devops-engineer": (
        OPERATIONS_DEVOPS_JD_VERSION,
        OPERATIONS_DEVOPS_RUBRIC_VERSION,
    ),
}

# A tuple is (jd_version, rubric_version, parser_version, scoring_version,
# prompt_version).  It is intentionally JSON-friendly because the health
# command and the worker lease persist this exact matrix for inspection.
ACTIVE_CONTRACTS: dict[str, tuple[str, str, str, str, str]] = {
    role: (
        jd,
        rubric,
        PARSER_VERSION,
        OPERATIONS_DEVOPS_SCORING_VERSION
        if role == "operations-devops-engineer"
        else SCORING_VERSION,
        OPERATIONS_DEVOPS_PROMPT_VERSION
        if role == "operations-devops-engineer"
        else PROMPT_VERSION,
    )
    for role, (jd, rubric) in ROLE_VERSIONS.items()
}


def contract_for_role(role: str) -> tuple[str, str, str, str, str]:
    """Return the complete active contract for one supported role."""

    try:
        return ACTIVE_CONTRACTS[role]
    except KeyError as exc:
        raise ValueError(f"unsupported role: {role!r}") from exc


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
