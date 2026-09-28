"""Public intro page."""
import re


def test_intro_page_is_public_and_locked_down(client):
    response = client.get("/welcome")
    assert response.status_code == 200
    assert 'Answers from your marketing data, in <mark class="hl">plain English</mark>.' in response.text
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


def test_errc_grid_compares_with_the_usual_approach(client):
    html = client.get("/welcome").text
    for quadrant in ("Eliminate", "Reduce", "Raise", "Create"):
        assert f">{quadrant}</h3>" in html
    # Every item pairs the usual approach with ours.
    assert html.count('class="errc-row errc-typical"') == html.count('class="errc-row errc-ours"') == 12
    # Raise and Create carry the highlight colour; Eliminate and Reduce don't.
    assert html.count('class="card errc hot"') == 2


def test_highlight_colour_is_defined_for_both_themes(client):
    css = client.get("/static/styles.css").text
    for token in ("--highlight:", "--highlight-text:", "--highlight-soft:", "--highlight-marker:"):
        assert css.count(token) == 2  # once for light, once for dark
