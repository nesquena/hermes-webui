"""Hermes Web UI -- API modules."""

# >>> hermes-fork: load HermesOS Cloud extension patches (HermesOS Cloud)
# All fork patches that used to live as in-line fork-marker blocks inside
# api/config.py now live in api/_hermes_fork_config.py — see that file's
# docstring for the why. Importing it here is the single touch point so
# any caller doing `from api import config` gets the patched module
# transparently, and `api/config.py` itself stays byte-identical to
# upstream master (no more daily rebase conflicts on that file).
from . import _hermes_fork_config  # noqa: F401
# <<< hermes-fork
