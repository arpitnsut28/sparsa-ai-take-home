"""URL validation and the SSRF guard."""
import pytest

import config
from services.urls import UrlRejected, canonicalise, normalise_many


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://example.com", "https://example.com/"),
        ("HTTPS://Example.COM/A?b=1", "https://example.com/A?b=1"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:80/x", "http://example.com/x"),
        ("https://example.com/x#section", "https://example.com/x"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("  https://example.com/x  ", "https://example.com/x"),
    ],
)
def test_canonicalises(raw, expected):
    assert canonicalise(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/x",
        "gopher://example.com",
        "example.com",                          # no scheme
        "",
        "   ",
        "https://",                             # no host
        "http://user:pass@example.com/",        # embedded credentials
        "http://example.com/\r\nHost: evil",    # control characters
    ],
)
def test_rejects_malformed(raw):
    with pytest.raises(UrlRejected):
        canonicalise(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost/x",
        "http://LOCALHOST:8000/x",
        "http://127.0.0.1/x",
        "http://127.1/x",              # inet_aton shorthand
        "http://0177.0.0.1/",          # octal
        "http://0x7f000001/",          # hex
        "http://2130706433/",          # 32-bit decimal
        "http://0.0.0.0/",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://[::1]/",
        "http://[::ffff:10.0.0.1]/",                  # v4-mapped v6 smuggling
        "http://db.internal/",
        "http://printer.local/",
    ],
)
def test_blocks_private_targets(raw):
    with pytest.raises(UrlRejected):
        canonicalise(raw)


def test_private_targets_allowed_when_operator_opts_in():
    config.settings.allow_private_network_urls = True
    assert canonicalise("http://localhost:9000/x") == "http://localhost:9000/x"


def test_url_length_cap():
    config.settings.max_url_chars = 60
    with pytest.raises(UrlRejected, match="exceeds"):
        canonicalise("https://example.com/" + "a" * 100)


def test_deduplicates_preserving_order():
    urls = [
        "https://example.com/a",
        "https://EXAMPLE.com/a",      # same after canonicalisation
        "https://example.com/a#frag",  # same after canonicalisation
        "https://example.org/b",
    ]
    assert normalise_many(urls) == ["https://example.com/a", "https://example.org/b"]


def test_batch_cap():
    config.settings.max_urls_per_run = 3
    with pytest.raises(UrlRejected, match="at most 3"):
        normalise_many([f"https://example.com/{i}" for i in range(4)])


def test_batch_error_names_the_offender():
    with pytest.raises(UrlRejected, match=r"urls\[1\]"):
        normalise_many(["https://example.com/a", "http://127.0.0.1/"])


@pytest.mark.parametrize("raw", ["http://1and1.com/x", "http://3com.com/", "http://123-reg.co.uk/"])
def test_digit_leading_domains_are_not_mistaken_for_ip_literals(raw):
    assert canonicalise(raw).startswith("http://")
