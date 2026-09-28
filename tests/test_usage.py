"""Plans, metering and limits: subscription, pay as you go, and the free trial."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr

from app.auth import SESSION_COOKIE, SESSION_PURPOSE
from app.sessions import sign
from app.usage import UsageRow, charge, client_ip, enforce, plan_for, price, trial_refusal
from tests.conftest import ADMIN_KEY, ANALYST_KEY
from tests.test_agent import GOOD_SQL, final, query
from tests.test_ask import Harness, h  # noqa: F401  (fixture)

SECRET = "usage-test-session-secret-0123456789"
# Two turns of the fake Claude: 200 input + 40 output tokens at Opus 5 list prices.
COST = 200 * 5.00 / 1e6 + 40 * 25.00 / 1e6 + 12_345 / 2**40 * 6.25


def ask(h, n=1, key=ANALYST_KEY):
    responses = []
    for _ in range(n):
        h.script(query(GOOD_SQL, "t1"), final())
        responses.append(h.ask(key=key))
    return responses


def as_trial(h, settings, email="newcomer@gmail.com"):
    """Sign `email` in with Google (not enrolled), so they fall into the free trial."""
    settings.session_secret = SecretStr(SECRET)
    settings.freemium_enabled = True
    settings.role_datasets["trial-only"] = ["ga4"]
    h.client.cookies.set(SESSION_COOKIE, sign({"email": email, "name": email}, SESSION_PURPOSE, SECRET, 3600))


def trial_ask(h):
    h.script(query("SELECT channel FROM ga4.sessions", "t1"), final())
    return h.client.post(
        "/ask", json={"question": "Which channel converts best?"}, headers={"X-Requested-With": "fetch"}
    )


# ---------- pricing and plans ----------


def test_price_and_charge(settings):
    cost = price(settings, input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000,
                 cache_write_tokens=1_000_000, bytes_processed=2**40)
    assert cost.llm_usd == pytest.approx(5 + 25 + 0.5 + 6.25)
    assert cost.bigquery_usd == pytest.approx(6.25)
    assert charge("payg", 1.0, settings) == pytest.approx(1.2)
    assert charge("subscription", 1.0, settings) == 0
    assert charge("freemium", 1.0, settings) == 0


def test_plan_for(settings):
    settings.user_plans = {"ana@company.com": "payg", "@company.com": "freemium", "etl-bot": "payg"}
    assert plan_for("Ana@Company.com", settings) == "payg"
    assert plan_for("bob@company.com", settings) == "freemium"
    assert plan_for("etl-bot", settings) == "payg"
    assert plan_for("someone@else.com", settings) == "subscription"


def test_client_ip_only_trusts_configured_proxies(settings):
    class Req:
        def __init__(self, xff):
            self.headers = {"x-forwarded-for": xff}
            self.client = type("C", (), {"host": "10.0.0.9"})()

    spoofed = Req("1.2.3.4, 203.0.113.7")
    assert client_ip(spoofed, settings) == "10.0.0.9"  # header ignored by default
    settings.trusted_proxy_hops = 1
    assert client_ip(spoofed, settings) == "203.0.113.7"  # the address our proxy saw


# ---------- metering ----------


def test_every_question_is_metered(h, usage_store):
    [response] = ask(h)
    assert response.status_code == 200
    [row] = usage_store.recent("ana")
    assert row["question"] == "Which channel spent the most?"
    assert row["cost_usd"] == pytest.approx(COST)
    assert row["charged_usd"] == 0  # subscription
    assert usage_store.month_totals("ana", datetime.now(timezone.utc).strftime("%Y-%m"))["questions"] == 1


def test_pay_as_you_go_is_charged_with_markup(h, settings, usage_store):
    settings.user_plans = {"ana": "payg"}
    ask(h)
    [row] = usage_store.recent("ana")
    assert row["charged_usd"] == pytest.approx(COST * 1.2)


def test_subscription_stops_when_included_usage_is_used(h, settings):
    settings.subscription_monthly_allowance_usd = COST  # exactly one question
    first, second = ask(h, 2)
    assert first.status_code == 200
    assert second.status_code == 402
    assert "included usage" in second.json()["detail"]
    assert h.audit.records[-1].outcome == "plan_limit"


def test_pay_as_you_go_monthly_cap(h, settings):
    settings.user_plans = {"ana": "payg"}
    settings.payg_monthly_limit_usd = COST * 1.2
    first, second = ask(h, 2)
    assert first.status_code == 200
    assert second.status_code == 402
    assert "spending limit" in second.json()["detail"]


def test_refused_question_does_not_reach_claude(h, settings):
    settings.subscription_monthly_allowance_usd = 0
    h.script()  # any Claude call would fail: no scripted responses
    assert h.ask().status_code == 402
    assert h.claude.requests == []


# ---------- free trial ----------


def test_unenrolled_person_gets_a_trial_on_trial_datasets(h, settings):
    as_trial(h, settings)
    me = h.client.get("/whoami").json()
    assert me == {"user": "newcomer@gmail.com", "role": "trial", "allowed_datasets": ["ga4"], "plan": "freemium"}


def test_trial_is_off_by_default(h, settings):
    as_trial(h, settings)
    settings.freemium_enabled = False
    response = h.client.get("/whoami")
    assert response.status_code == 403
    assert "does not have access yet" in response.json()["detail"]


def test_trial_daily_question_cap(h, settings):
    as_trial(h, settings)
    settings.freemium_daily_questions = 2
    assert trial_ask(h).status_code == 200
    assert trial_ask(h).status_code == 200
    third = trial_ask(h)
    assert third.status_code == 402
    assert "2 free questions" in third.json()["detail"]


def test_trial_daily_cost_cap(h, settings):
    as_trial(h, settings)
    settings.freemium_daily_cost_usd = COST / 2
    assert trial_ask(h).status_code == 200
    assert "free allowance" in trial_ask(h).json()["detail"]


def test_trial_cap_is_shared_across_accounts_on_one_network(h, settings):
    as_trial(h, settings, "first@gmail.com")
    settings.freemium_ip_daily_questions = 1
    assert trial_ask(h).status_code == 200
    as_trial(h, settings, "second@gmail.com")  # same machine, new Google account
    response = trial_ask(h)
    assert response.status_code == 402
    assert "your network" in response.json()["detail"]


def test_enrolled_people_are_not_limited_by_trial_network_caps(h, settings):
    settings.freemium_ip_daily_questions = 0
    assert ask(h)[0].status_code == 200


def test_trial_ends_after_its_length(settings, usage_store):
    settings.freemium_enabled = True
    long_ago = datetime.now(timezone.utc) - timedelta(days=15)
    usage_store.record(UsageRow("r1", "old@gmail.com", "freemium", "1.2.3.4", "q", "answered"), now=long_ago)
    with pytest.raises(Exception) as caught:
        enforce("old@gmail.com", "freemium", "1.2.3.4", usage_store, settings)
    assert caught.value.status_code == 402
    assert "trial ended" in caught.value.detail


@pytest.mark.parametrize(
    "rule, value, email, ip, allowed",
    [
        ("freemium_blocked_accounts", ["spam@gmail.com"], "spam@gmail.com", "1.2.3.4", False),
        ("freemium_blocked_accounts", ["@tempmail.dev"], "x@tempmail.dev", "1.2.3.4", False),
        ("freemium_allowed_accounts", ["@partner.com"], "x@gmail.com", "1.2.3.4", False),
        ("freemium_allowed_accounts", ["@partner.com"], "x@partner.com", "1.2.3.4", True),
        ("freemium_blocked_ips", ["203.0.113.0/24"], "x@gmail.com", "203.0.113.50", False),
        ("freemium_allowed_ips", ["198.51.100.0/24"], "x@gmail.com", "203.0.113.50", False),
        ("freemium_allowed_ips", ["198.51.100.0/24"], "x@gmail.com", "198.51.100.7", True),
    ],
)
def test_trial_account_and_network_rules(settings, rule, value, email, ip, allowed):
    settings.freemium_enabled = True
    setattr(settings, rule, value)
    assert (trial_refusal(email, ip, settings) is None) == allowed


def test_blocked_network_sees_why(h, settings):
    as_trial(h, settings)
    settings.freemium_blocked_ips = ["0.0.0.0/0", "::/0"]
    settings.trusted_proxy_hops = 1
    response = h.client.get("/whoami", headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 403
    assert "your network" in response.json()["detail"]


# ---------- usage page ----------


def test_usage_report(h, settings):
    ask(h, 2)
    report = h.client.get("/usage", headers={"X-API-Key": ANALYST_KEY}).json()
    today = datetime.now(timezone.utc)
    assert report["plan"]["id"] == "subscription"
    assert "$20.00 a month" in report["plan"]["summary"]
    assert report["month"]["questions"] == 2
    assert report["month"]["cost_usd"] == pytest.approx(round(2 * COST, 4))
    assert len(report["daily"]) == today.day
    assert report["daily"][-1]["questions"] == 2
    assert [item["id"] for item in report["limits"]] == ["included_usage"]
    assert len(report["recent"]) == 2
    assert report["is_usage_admin"] is False


def test_trial_usage_report_hides_network_counts(h, settings):
    as_trial(h, settings)
    trial_ask(h)
    report = h.client.get("/usage").json()
    assert report["plan"]["name"] == "Free trial"
    assert [item["id"] for item in report["limits"]] == ["trial_questions", "trial_cost"]
    assert report["limits"][0]["used"] == 1
    assert report["trial_ends_on"] is not None


def test_everyones_usage_is_admin_only(h):
    ask(h)
    assert h.client.get("/usage/all", headers={"X-API-Key": ANALYST_KEY}).status_code == 403
    everyone = h.client.get("/usage/all", headers={"X-API-Key": ADMIN_KEY}).json()
    assert [u["user"] for u in everyone["users"]] == ["ana"]


def test_each_plan_counts_only_its_own_usage(h, settings, usage_store):
    # A trial question earlier doesn't eat into a later subscription allowance.
    usage_store.record(UsageRow("r0", "ana", "freemium", "1.2.3.4", "q", "answered", cost_usd=10.0))
    settings.subscription_monthly_allowance_usd = 5.0
    assert ask(h)[0].status_code == 200
