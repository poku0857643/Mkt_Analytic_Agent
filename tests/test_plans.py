"""Choosing, switching, cancelling and resuming plans."""
from datetime import datetime, timedelta, timezone

import pytest

from app.usage import effective_plan, next_month_start
from tests.conftest import ANALYST_KEY
from tests.test_agent import GOOD_SQL, final, query
from tests.test_ask import Harness, h  # noqa: F401  (fixture)
from tests.test_usage import as_trial

KEY = {"X-API-Key": ANALYST_KEY}


def post(h, path, json=None):
    return h.client.post(path, json=json, headers=KEY)


def whoami_plan(h):
    return h.client.get("/whoami", headers=KEY).json()["plan"]


def ask_ok(h):
    h.script(query(GOOD_SQL, "t1"), final())
    return h.ask()


def test_switch_between_subscription_and_pay_as_you_go(h, usage_store):
    assert whoami_plan(h) == "subscription"
    response = post(h, "/plan", {"plan": "payg"})
    assert response.status_code == 200
    assert response.json()["state"] == "active"
    assert whoami_plan(h) == "payg"

    ask_ok(h)
    [row] = usage_store.recent("ana")
    assert row["charged_usd"] > 0  # charged as pay as you go from the switch on

    post(h, "/plan", {"plan": "subscription"})
    assert whoami_plan(h) == "subscription"
    changes = [r.error for r in h.audit.records if r.event == "plan_change"]
    assert changes == ["subscription -> payg", "payg -> subscription"]


def test_only_self_serve_plans_can_be_chosen(h):
    assert post(h, "/plan", {"plan": "freemium"}).status_code == 422
    assert post(h, "/plan", {"plan": "enterprise"}).status_code == 422


def test_cancelled_subscription_runs_to_the_end_of_the_month(h, usage_store):
    response = post(h, "/plan/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ending"
    assert body["ends_on"] == next_month_start(datetime.now(timezone.utc)).date().isoformat()

    # Still usable until then.
    assert whoami_plan(h) == "subscription"
    assert ask_ok(h).status_code == 200

    report = h.client.get("/usage", headers=KEY).json()
    assert report["plan"]["state"] == "ending"


def test_after_a_subscription_ends_questions_stop(h, settings, usage_store):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    usage_store.set_plan_choice("ana", "subscription", past.isoformat(), past)
    assert whoami_plan(h) == "none"
    h.script()
    response = h.ask()
    assert response.status_code == 402
    assert "don't have an active plan" in response.json()["detail"]
    assert h.claude.requests == []


def test_resume_undoes_a_cancellation(h):
    post(h, "/plan/cancel")
    response = post(h, "/plan/resume")
    assert response.status_code == 200
    assert response.json()["state"] == "active"
    assert post(h, "/plan/resume").status_code == 409  # nothing left to undo


def test_cancelling_twice_is_refused(h):
    post(h, "/plan/cancel")
    assert post(h, "/plan/cancel").status_code == 409


def test_stopping_pay_as_you_go_is_immediate_and_reversible(h):
    post(h, "/plan", {"plan": "payg"})
    response = post(h, "/plan/cancel")
    assert response.json() == {"plan": "none", "state": "none", "ends_on": None}
    assert whoami_plan(h) == "none"
    h.script()
    assert h.ask().status_code == 402

    post(h, "/plan", {"plan": "payg"})
    assert ask_ok(h).status_code == 200


def test_no_plan_has_nothing_to_cancel(h):
    post(h, "/plan", {"plan": "payg"})
    post(h, "/plan/cancel")
    assert post(h, "/plan/cancel").status_code == 409


def test_usage_page_offers_plan_changes(h):
    report = h.client.get("/usage", headers=KEY).json()
    assert report["can_change_plan"] is True
    assert [p["id"] for p in report["plan_options"]] == ["subscription", "payg"]
    assert report["plan"]["state"] == "active"


def test_trial_accounts_cannot_choose_a_plan(h, settings):
    as_trial(h, settings)
    response = h.client.post("/plan", json={"plan": "payg"}, headers={"X-Requested-With": "fetch"})
    assert response.status_code == 403
    assert h.client.get("/usage").json()["can_change_plan"] is False


def test_plan_changes_from_the_browser_need_the_csrf_header(h, settings):
    as_trial(h, settings)
    assert h.client.post("/plan/cancel").status_code == 403


@pytest.mark.parametrize("enabled, expected", [(False, "subscription"), (True, "freemium")])
def test_free_plan_only_applies_while_the_trial_service_is_on(settings, usage_store, enabled, expected):
    settings.freemium_enabled = enabled
    settings.user_plans = {"ana": "freemium"}
    assert effective_plan("ana", settings, usage_store) == expected
