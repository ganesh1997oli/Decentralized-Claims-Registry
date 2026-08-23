import pytest

from apps.backend.app.public_demo_access import (
    PublicDemoAccess,
    PublicDemoConfigurationError,
)


def test_public_demo_access_is_secure_by_default():
    access = PublicDemoAccess.from_settings({})

    assert access.public_read_only is False
    assert access.allows_anonymous_read(None) is False


def test_public_demo_access_allows_only_missing_credentials():
    access = PublicDemoAccess.from_settings({"PUBLIC_DEMO_READ_ONLY": "true"})

    assert access.public_read_only is True
    assert access.allows_anonymous_read(None) is True
    assert access.allows_anonymous_read("  ") is True
    assert access.allows_anonymous_read("invalid-supplied-key") is False


def test_public_demo_access_rejects_ambiguous_configuration():
    with pytest.raises(PublicDemoConfigurationError, match="true or false"):
        PublicDemoAccess.from_settings({"PUBLIC_DEMO_READ_ONLY": "yes"})
