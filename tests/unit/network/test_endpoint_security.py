import pytest

from athena.network.endpoint_security import (
    EndpointSecurityError,
    classify_endpoint,
    validate_endpoint,
)


@pytest.mark.parametrize(
    ("url", "classification"),
    [
        ("http://localhost:8080", "loopback"),
        ("http://127.0.0.1:8080", "loopback"),
        ("http://[::1]:8080", "loopback"),
        ("http://10.0.0.2:8080", "private"),
        ("http://169.254.1.2:8080", "link-local"),
        ("https://api.example.test", "public"),
    ],
)
def test_endpoint_classification(url, classification):
    assert classify_endpoint(url) == classification


def test_loopback_http_is_allowed_but_remote_http_requires_override():
    assert validate_endpoint("http://127.0.0.1:1", credentialed=True).loopback
    with pytest.raises(EndpointSecurityError, match="credentialed"):
        validate_endpoint("http://10.0.0.2:1", credentialed=True)
    with pytest.raises(EndpointSecurityError, match="explicit operator"):
        validate_endpoint("http://10.0.0.2:1", credentialed=False)
    assert (
        validate_endpoint(
            "http://10.0.0.2:1", credentialed=False, allow_insecure_remote=True
        ).classification
        == "private"
    )


@pytest.mark.parametrize(
    "url",
    ["ftp://example.test", "https://user:pass@example.test", "https://example.test:bad"],
)
def test_endpoint_rejects_unsafe_url_forms(url):
    with pytest.raises(EndpointSecurityError):
        validate_endpoint(url, credentialed=False, allow_insecure_remote=True)


def test_proxy_environment_is_disabled_by_default():
    assert validate_endpoint("https://api.example.test", credentialed=True).trust_env is False
    assert (
        validate_endpoint("https://api.example.test", credentialed=True, trust_env=True).trust_env
        is True
    )
