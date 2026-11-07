"""db-podcast package."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("db-podcast")
except PackageNotFoundError:
    __version__ = "unknown"
