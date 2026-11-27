"""Shared exceptions for ctxshare."""


class CtxshareError(Exception):
    """An expected, user-facing error."""


class ConfigError(CtxshareError):
    """Invalid configuration."""


class CapsuleError(CtxshareError):
    """Invalid or unreadable context capsule."""


class ProviderError(CtxshareError):
    """Upstream provider returned an error."""
