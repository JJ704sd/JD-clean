"""Stable, role-aware scanning for a downloaded resume directory."""

from __future__ import annotations

import hashlib
import ctypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cleaning import SUPPORTED_SUFFIXES
from .metadata import infer_candidate_name, infer_role

TEMPORARY_SUFFIXES = {".crdownload", ".part", ".tmp", ".temp", ".swp"}


def is_ignored_watch_file(path: str | Path) -> bool:
    """Return whether a directory entry is hidden or a known temporary file."""

    name = Path(path).name
    lowered = name.casefold()
    if name.startswith(".") or name.startswith("~$"):
        return True
    if os.name == "nt":
        try:
            attributes = ctypes.windll.kernel32.GetFileAttributesW(str(Path(path)))
        except (AttributeError, OSError):
            attributes = -1
        if attributes != -1 and attributes & 0x2:
            return True
    return Path(name).suffix.casefold() in TEMPORARY_SUFFIXES or lowered.endswith(
        ".crdownload"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class WatchCandidate:
    """A file that is stable and ready to be inserted into the task queue."""

    source_path: Path
    source_sha256: str
    candidate_id: str
    candidate_name: str | None
    role: str


@dataclass
class _FileState:
    signature: tuple[int, int]
    stable_cycles: int = 1
    source_sha256: str | None = None
    emitted_key: tuple[tuple[int, int], str, str] | None = None
    event_keys: set[tuple[tuple[int, int], str]] = field(default_factory=set)


class WatchScanner:
    """Scan one directory without rereading stable files on every poll.

    The scanner owns only in-memory state.  Queue idempotency remains the
    durable guard across process restarts and contract versions.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        role: str | None = None,
        auto_route: bool = False,
        accept_unlabeled: bool = False,
        on_event: Callable[[str], None] | None = None,
    ):
        if bool(role) == auto_route:
            raise ValueError("watch scanner requires exactly one of role or auto_route")
        if accept_unlabeled and auto_route:
            raise ValueError("--accept-unlabeled only applies to a fixed role")
        self.directory = Path(directory).resolve()
        self.role = role
        self.auto_route = auto_route
        self.accept_unlabeled = accept_unlabeled
        self.on_event = on_event
        self._states: dict[str, _FileState] = {}
        self.event_counts: dict[str, int] = {}

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path.resolve()).casefold()

    def _event(self, code: str, path: Path, signature: tuple[int, int]) -> None:
        state = self._states.get(self._path_key(path))
        event_key = (signature, code)
        if state is not None and event_key in state.event_keys:
            return
        if state is not None:
            state.event_keys.add(event_key)
        self.event_counts[code] = self.event_counts.get(code, 0) + 1
        if self.on_event is not None:
            self.on_event(code)

    def _state_for(self, path: Path, signature: tuple[int, int]) -> _FileState:
        key = self._path_key(path)
        state = self._states.get(key)
        if state is None or state.signature != signature:
            state = _FileState(signature=signature)
            self._states[key] = state
        else:
            state.stable_cycles += 1
        return state

    def _route(self, path: Path) -> str | None:
        detected = infer_role(path)
        if self.auto_route:
            if detected is None:
                return None
            return detected
        assert self.role is not None
        if detected is not None and detected != self.role:
            return None
        if detected is None and not self.accept_unlabeled:
            return None
        return self.role

    def scan(self) -> list[WatchCandidate]:
        """Return newly stable, explicitly routable files from this poll."""

        if not self.directory.is_dir():
            self._event("WATCH_INPUT_UNAVAILABLE", self.directory, (0, 0))
            return []
        try:
            entries = sorted(self.directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            self._event("WATCH_SCAN_ERROR", self.directory, (0, 0))
            return []

        candidates: list[WatchCandidate] = []
        for path in entries:
            if is_ignored_watch_file(path) or not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix not in SUPPORTED_SUFFIXES:
                try:
                    stat = path.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    self._event("WATCH_SCAN_ERROR", path, (0, 0))
                    continue
                state = self._state_for(path, signature)
                if state.stable_cycles >= 2:
                    self._event("UNSUPPORTED_FILE_SKIPPED", path, signature)
                continue
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                self._event("WATCH_SCAN_ERROR", path, (0, 0))
                continue

            state = self._state_for(path, signature)
            if state.stable_cycles < 2:
                continue

            detected_role = infer_role(path)
            route = self._route(path)
            if route is None:
                if self.auto_route:
                    code = "UNKNOWN_FILE_SKIPPED"
                elif detected_role is not None:
                    code = "ROLE_MISMATCH_SKIPPED"
                else:
                    code = "UNLABELED_FILE_SKIPPED"
                self._event(code, path, signature)
                continue

            if state.source_sha256 is None:
                try:
                    source_sha256 = _sha256(path)
                    after = path.stat()
                except OSError:
                    self._event("WATCH_HASH_ERROR", path, signature)
                    continue
                after_signature = (after.st_size, after.st_mtime_ns)
                if after_signature != signature:
                    # The file changed while it was being hashed.  Require two
                    # fresh stable observations before trying again.
                    self._states[self._path_key(path)] = _FileState(
                        signature=after_signature
                    )
                    continue
                state.source_sha256 = source_sha256

            assert state.source_sha256 is not None
            emitted_key = (signature, state.source_sha256, route)
            if state.emitted_key == emitted_key:
                continue
            state.emitted_key = emitted_key
            candidates.append(
                WatchCandidate(
                    source_path=path.resolve(),
                    source_sha256=state.source_sha256,
                    candidate_id=f"candidate-{state.source_sha256[:12]}",
                    candidate_name=infer_candidate_name(path),
                    role=route,
                )
            )
        return candidates

    scan_once = scan
