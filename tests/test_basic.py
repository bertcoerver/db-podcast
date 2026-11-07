"""Basic import and version tests."""

import db_podcast


def test_version():
    """Test that version is defined."""
    assert hasattr(db_podcast, "__version__")
    assert isinstance(db_podcast.__version__, str)
