"""Apple Foundation Model adapter over the on-device `fm` CLI."""

from dfm.models.apple.fm import ON_DEVICE_MODEL, AppleFMModel, classify_failure

__all__ = ["ON_DEVICE_MODEL", "AppleFMModel", "classify_failure"]
