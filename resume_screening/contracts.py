"""Load the role-owned validators for model records."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROLE_SKILL_DIRS = {
    "ai-product-manager": "screen-ai-product-manager-resumes",
    "senior-fullstack-engineer": "screen-senior-fullstack-resumes",
    "fullstack-development-intern": "screen-fullstack-intern-resumes",
    "operations-devops-engineer": "screen-operations-devops-resumes",
    "business-system-operations-engineer": "screen-business-system-operations-resumes",
}


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    module_directory = str(path.parent)
    inserted = module_directory not in sys.path
    # Validators import their sibling profile validator as the plain module
    # name ``validate_jd_profile``. Isolate that dependency while loading each
    # role so one role's validator cannot remain cached for another role.
    dependency_name = "validate_jd_profile"
    previous_dependency = sys.modules.pop(dependency_name, None)
    if inserted:
        sys.path.insert(0, module_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(dependency_name, None)
        if previous_dependency is not None:
            sys.modules[dependency_name] = previous_dependency
        if inserted:
            sys.path.remove(module_directory)
    return module


def validate_record(
    project_root: str | Path, role: str, record: dict[str, Any]
) -> list[str]:
    skill_dir = ROLE_SKILL_DIRS[role]
    path = (
        Path(project_root)
        / "skills"
        / skill_dir
        / "scripts"
        / "validate_screening_output.py"
    )
    module = _load_module(path, f"screening_validator_{skill_dir.replace('-', '_')}")
    validator = getattr(module, "validate_record", None) or getattr(
        module, "validate", None
    )
    if validator is None:
        raise RuntimeError(f"validator has no public validation function: {path}")
    return validator(record)
