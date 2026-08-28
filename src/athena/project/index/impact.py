"""Impact analysis façade over an immutable project-index revision."""

from __future__ import annotations

from athena.project.index.models import ProjectIndex


class ImpactAnalyzer:
    def analyze(self, index: ProjectIndex, changed_paths: list[str]) -> dict:
        return index.impact(changed_paths)


__all__ = ["ImpactAnalyzer"]
