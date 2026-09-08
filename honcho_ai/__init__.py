"""Compatibility wrapper for the PyPI package name `honcho-ai`.

The published package installs the `honcho` module, so code written against the
package name may import `honcho_ai` and expect the same API surface.
"""

from honcho import *  # noqa: F401,F403
from honcho import __all__ as _honcho_all

__all__ = list(_honcho_all) if _honcho_all else []


def __getattr__(name):
    import honcho as _honcho

    if hasattr(_honcho, name):
        value = getattr(_honcho, name)
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
