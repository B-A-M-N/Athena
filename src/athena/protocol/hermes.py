"""Neutral contracts for the optional Hermes referee boundary."""

from enum import StrEnum


class HermesSupervisionMode(StrEnum):
    """Operator-selected strength of the optional Hermes boundary."""

    OFF = "off"
    ADVISORY = "advisory"
    REQUIRED = "required"


__all__ = ["HermesSupervisionMode"]
