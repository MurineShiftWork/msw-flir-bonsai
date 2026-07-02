"""Test that the package version is discoverable and non-empty."""

import importlib.metadata


def test_version_is_non_empty_string():
    version = importlib.metadata.version("msw-flir-bonsai")
    assert isinstance(version, str)
    assert version
