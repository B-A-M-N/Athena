"""Compatibility imports for the interface-neutral native bridge."""

from athena.presentation.native_bridge import (
    native_projection_frame,
    write_native_projection,
)
from athena.presentation.schema import NATIVE_BRIDGE_SCHEMA_VERSION

__all__ = [
    "NATIVE_BRIDGE_SCHEMA_VERSION",
    "native_projection_frame",
    "write_native_projection",
]
