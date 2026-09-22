"""Compatibility imports for the interface-neutral presentation reducer.

Shared consumers should import from :mod:`athena.presentation.projection`.
This module remains so hosted CLI integrations do not acquire a second
projection authority during the package extraction.
"""

from athena.presentation.projection import OperationNode, ProjectionState

__all__ = ["OperationNode", "ProjectionState"]
