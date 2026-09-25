"""Exception types. Every error a user can hit carries a message that says what to do next."""

from __future__ import annotations


class SinceCutoffError(Exception):
    """Base class for errors that are reported to the user without a traceback."""


class ProjectError(SinceCutoffError):
    """The project directory could not be understood (no lockfile, no dependencies, ...)."""


class PackageIndexError(SinceCutoffError):
    """PyPI could not be reached or a distribution could not be downloaded."""


class ModelLookupError(SinceCutoffError):
    """The model name could not be resolved to a knowledge cutoff."""


class ProviderError(SinceCutoffError):
    """A model provider call failed."""


class CheckerError(SinceCutoffError):
    """The type checker could not be run."""
