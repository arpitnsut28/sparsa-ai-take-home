"""Token bucket mechanics."""
import time

import config
from services.ratelimit import RateLimiter


def test_burst_then_throttle():
    config.settings.rate_limit_enabled = True
    config.settings.rate_limit_burst = 3
    config.settings.rate_limit_per_minute = 60      # one token per second
    limiter = RateLimiter()

    assert [limiter.check("ip-a")[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.check("ip-a")
    assert allowed is False
    assert retry_after > 0


def test_buckets_are_per_client():
    config.settings.rate_limit_enabled = True
    config.settings.rate_limit_burst = 1
    config.settings.rate_limit_per_minute = 1
    limiter = RateLimiter()

    assert limiter.check("ip-a")[0] is True
    assert limiter.check("ip-a")[0] is False
    assert limiter.check("ip-b")[0] is True, "one noisy client must not throttle everyone"


def test_tokens_refill_over_time():
    config.settings.rate_limit_enabled = True
    config.settings.rate_limit_burst = 1
    config.settings.rate_limit_per_minute = 6000    # 100/s
    limiter = RateLimiter()

    assert limiter.check("ip")[0] is True
    assert limiter.check("ip")[0] is False
    time.sleep(0.05)
    assert limiter.check("ip")[0] is True


def test_disabled_limiter_always_allows():
    config.settings.rate_limit_enabled = False
    limiter = RateLimiter()
    assert all(limiter.check("ip")[0] for _ in range(100))


def test_idle_buckets_are_swept():
    """The bucket map is keyed by client IP — without a sweep it is a slow leak."""
    config.settings.rate_limit_enabled = True
    limiter = RateLimiter()
    for i in range(50):
        limiter.check(f"ip-{i}")
    assert limiter.sweep(max_idle_s=0) == 50
