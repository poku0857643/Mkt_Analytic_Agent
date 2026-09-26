from app.limits import DailyScanBudget, RateLimiter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_rate_limiter_allows_up_to_limit_then_blocks():
    clock = Clock()
    limiter = RateLimiter(limit=3, window=60, clock=clock)
    assert [limiter.check("u") for _ in range(3)] == [None, None, None]
    assert limiter.check("u") == 60


def test_rate_limiter_window_rolls():
    clock = Clock()
    limiter = RateLimiter(limit=2, window=60, clock=clock)
    limiter.check("u")
    clock.now = 30
    limiter.check("u")
    clock.now = 45
    assert limiter.check("u") == 15  # oldest hit expires at t=60
    clock.now = 60
    assert limiter.check("u") is None


def test_rejected_requests_do_not_extend_the_block():
    clock = Clock()
    limiter = RateLimiter(limit=1, window=60, clock=clock)
    limiter.check("u")
    for t in (10, 20, 30):
        clock.now = t
        assert limiter.check("u") is not None
    clock.now = 60
    assert limiter.check("u") is None


def test_rate_limiter_is_per_user():
    limiter = RateLimiter(limit=1, clock=Clock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None


def test_daily_budget_tracks_and_resets_by_day():
    day = ["2026-09-26"]
    budget = DailyScanBudget(limit_bytes=1000, today=lambda: day[0])
    budget.add("u", 300)
    budget.add("u", 500)
    assert budget.remaining("u") == 200
    assert budget.remaining("other") == 1000

    budget.add("u", 999)
    assert budget.remaining("u") == 0  # never negative

    day[0] = "2026-09-27"
    assert budget.remaining("u") == 1000
