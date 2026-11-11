"""Error taxonomy for tiny_translator.

`TranslationError` subclasses `RuntimeError`, so existing `except RuntimeError`
handlers keep working; the subclasses let a host decide *why* a translation
failed — missing key vs exhausted quota vs rate limit vs network — and switch
provider accordingly.
"""

from __future__ import annotations

from pathlib import Path


class TranslationError(RuntimeError):
    """Base class for every translation failure."""


class AuthMissingError(TranslationError):
    """No usable credentials for the requested provider."""

    def __init__(self, engine_label: str, auth_file: Path | None = None) -> None:
        self.engine_label = engine_label
        self.auth_file = auth_file
        super().__init__(f"未检测到 {engine_label} 的有效凭据")


class MissingDependencyError(TranslationError):
    """An optional dependency is required for the chosen credential type."""

    def __init__(self, package: str, message: str) -> None:
        self.package = package
        super().__init__(message)


class ProviderError(TranslationError):
    """Provider rejected the request (bad params, unknown error code, ...)."""


class QuotaExceededError(ProviderError):
    """Balance or quota exhausted — retrying this provider is pointless."""


class RateLimitError(ProviderError):
    """Too many requests — retry later or switch provider."""


class NetworkError(TranslationError):
    """Transport-level failure after all retries were spent."""
