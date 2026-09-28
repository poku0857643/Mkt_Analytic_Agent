"""Public plans and pricing page, and the catalog it is rendered from."""
import re

import pytest


def catalog(client):
    response = client.get("/plans/catalog")
    assert response.status_code == 200
    return response.json()


def test_page_is_public_and_locked_down(client):
    response = client.get("/plans")
    assert response.status_code == 200
    assert "<title>Plans and pricing" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert not re.search(r"<script(?![^>]*\bsrc=)", response.text)
    assert 'href="/plans"' in client.get("/").text  # linked from sign-in


def test_catalog_needs_no_sign_in_and_matches_settings(client, settings):
    body = catalog(client)
    subscription, payg = body["plans"]
    assert subscription["price"] == "$20.00"
    assert "$25.00 of usage every month" in subscription["includes"][0]
    assert payg["price"] == "Cost + 20%"
    assert "Spending cap of $100.00 a month" in payg["limits"][0]
    assert body["billing_enabled"] is False
    assert body["prices"]["output_per_mtok"] == settings.price_output_per_mtok


def test_example_costs_come_from_current_prices(client, settings):
    lookup, report = catalog(client)["examples"]
    assert lookup["cost_usd"] == pytest.approx(0.06, abs=0.005)
    assert report["cost_usd"] == pytest.approx(0.32, abs=0.01)
    assert report["payg_usd"] == pytest.approx(report["cost_usd"] * 1.2, abs=0.0001)

    settings.price_output_per_mtok = 50.0  # prices change -> examples follow
    assert catalog(client)["examples"][1]["cost_usd"] > report["cost_usd"]


def test_settings_changes_show_up(client, settings):
    settings.subscription_fee_usd = 35
    settings.payg_markup = 1.0
    settings.payg_monthly_limit_usd = None
    subscription, payg = catalog(client)["plans"]
    assert subscription["price"] == "$35.00"
    assert payg["price"] == "At cost"
    assert payg["limits"] == ["No monthly spending cap"]


def test_free_trial_is_listed_only_while_open(client, settings):
    assert [p["id"] for p in catalog(client)["plans"]] == ["subscription", "payg"]
    settings.freemium_enabled = True
    trial = catalog(client)["plans"][-1]
    assert trial["id"] == "freemium"
    assert trial["price"] == "Free"
    assert "never company data" in trial["includes"][1]


def test_fair_use_states_an_honest_worst_case(client, settings):
    fair = catalog(client)["fair_use"]
    assert "200 questions per person per day" in fair
    # Worst case prices every token at the output rate: 150,000 x $25 / 1M.
    assert fair[-1].endswith("never cost more than $3.75")
