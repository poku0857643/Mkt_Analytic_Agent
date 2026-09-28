"""Public intro page."""
import re


def test_intro_page_is_public_and_locked_down(client):
    response = client.get("/welcome")
    assert response.status_code == 200
    assert "Answers from your marketing data, in plain English." in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    # No scripts at all, and no inline styles (the CSP would block them).
    assert "<script" not in response.text
    assert not re.search(r'\sstyle="', response.text)


def test_intro_links_to_sign_in_pricing_and_terms(client):
    html = client.get("/welcome").text
    assert 'href="/?signin=1"' in html
    assert 'href="/plans"' in html
    assert 'href="/static/terms.html"' in html
    assert 'href="/welcome"' in client.get("/").text  # and sign-in links back


def test_intro_example_uses_real_sample_figures(client):
    html = client.get("/welcome").text
    # Figures verified against ga4 (see tests/test_integration_ga4.py for the data).
    for figure in ("$181,603", "$339,457", "109,005", "143,185", "208,942"):
        assert figure in html
    assert "Google Merchandise Store sample data" in html
