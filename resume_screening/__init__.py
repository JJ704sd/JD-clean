"""Reliable, auditable resume-screening pipeline."""

from .scoring import ScoreResult, score_record

__all__ = ["ScoreResult", "score_record"]
