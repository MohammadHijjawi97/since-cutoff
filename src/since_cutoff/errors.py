"""Exception types. Every error a user can hit carries a message that says what to do next."""

from __future__ import annotations


class SinceCutoffError(Exception):
    """Base class for errors that are reported to the user without a traceback."""


class ProjectError(SinceCutoffError):
    """The project directory could not be understood (no lockfile, no dependencies, ...)."""


class PackageIndexError(SinceCutoffError):
    """PyPI could not be reached or a distribution could not be downloaded."""


class NoCodeError(PackageIndexError):
    """A release has no importable modules and requires no package that has them: a
    placeholder that reserves the name (a metapackage raises PackageIndexError)."""


# The words of a metapackage's PackageIndexError (pypi._metapackage), by which the report
# shows that skip as information, not among the dependencies that could not be checked.
METAPACKAGE = "is a metapackage with no API of its own"


class ModelLookupError(SinceCutoffError):
    """The model name could not be resolved to a knowledge cutoff."""


class ProviderError(SinceCutoffError):
    """A model provider call failed."""


class CheckerError(SinceCutoffError):
    """The type checker could not be run."""
