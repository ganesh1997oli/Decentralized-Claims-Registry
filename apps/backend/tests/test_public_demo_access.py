import pytest

from apps.backend.app.public_demo_access import (
    PublicDemoAccess,
    PublicDemoConfigurationError,
)


def test_public_demo_access_is_secure_by_default():
    access = PublicDemoAccess.from_settings({})

    assert access.public_read_only is False
    assert access.public_prototype_assessor is False
    assert access.allows_anonymous_read(None) is False
    assert access.anonymous_assessor_reference(None) is None
    assert access.allows_anonymous_assessor_write(None) is False


def test_public_demo_access_allows_only_missing_credentials():
    access = PublicDemoAccess.from_settings({"PUBLIC_DEMO_READ_ONLY": "true"})

    assert access.public_read_only is True
    assert access.allows_anonymous_read(None) is True
    assert access.allows_anonymous_read("  ") is True
    assert access.allows_anonymous_read("invalid-supplied-key") is False


def test_public_demo_access_rejects_ambiguous_configuration():
    with pytest.raises(PublicDemoConfigurationError, match="true or false"):
        PublicDemoAccess.from_settings({"PUBLIC_DEMO_READ_ONLY": "yes"})


def test_public_prototype_allows_missing_assessor_key_only():
    access = PublicDemoAccess.from_settings(
        {
            "PUBLIC_DEMO_READ_ONLY": "true",
            "PUBLIC_PROTOTYPE_ASSESSOR": "true",
        }
    )

    assert access.public_read_only is True
    assert access.public_prototype_assessor is True
    assert access.anonymous_assessor_reference(None) == "public-prototype-assessor"
    assert access.allows_anonymous_assessor_write("  ") is True

    # Supplying a value opts back into normal authentication. Prototype mode
    # must never turn a wrong key into an accepted anonymous request.
    assert access.anonymous_assessor_reference("wrong-key") is None
    assert access.allows_anonymous_assessor_write("wrong-key") is False


def test_public_prototype_rejects_ambiguous_configuration():
    with pytest.raises(PublicDemoConfigurationError, match="true or false"):
        PublicDemoAccess.from_settings({"PUBLIC_PROTOTYPE_ASSESSOR": "yes"})
