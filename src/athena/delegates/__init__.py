"""External specialist delegation primitives."""

from athena.delegates.models import DelegateSession, DelegateSpec
from athena.delegates.registry import DelegateRegistry
from athena.delegates.sessions import ExternalDelegateManager

__all__ = ["DelegateSession", "DelegateSpec", "DelegateRegistry", "ExternalDelegateManager"]
