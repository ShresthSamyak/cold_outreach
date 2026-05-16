"""Verify the normalizer handles the field-name shapes seen across popular
LinkedIn profile actors. If we add support for a new actor, add a fixture
here first, then make it pass.
"""

from outreach.scraper import normalize


def test_apimaestro_style() -> None:
    item = {
        "fullName": "Alice Founder",
        "headline": "Building AI at Acme",
        "jobTitle": "CTO",
        "companyName": "Acme AI",
        "location": "New Delhi, India",
        "about": "Building agents.",
        "url": "https://www.linkedin.com/in/alice-founder/",
        "activities": [{"text": "We're hiring interns!"}],
    }
    p = normalize(item)
    assert p.name == "Alice Founder"
    assert p.role == "CTO"
    assert p.company == "Acme AI"
    assert p.location == "New Delhi, India"
    assert p.about == "Building agents."
    assert p.url == "https://www.linkedin.com/in/alice-founder/"
    assert p.recent_activity == ["We're hiring interns!"]


def test_dev_fusion_style_with_experience_array() -> None:
    item = {
        "firstName": "Bob",
        "lastName": "Builder",
        "headline": "Engineer",
        "experience": [
            {"title": "Founding Engineer", "companyName": "Beta Labs"},
            {"title": "SWE", "companyName": "OldCo"},
        ],
        "summary": "Shipping things.",
        "linkedinUrl": "https://linkedin.com/in/bob",
        "geoLocation": "Bengaluru",
    }
    p = normalize(item)
    assert p.name == "Bob Builder"
    assert p.role == "Founding Engineer"
    assert p.company == "Beta Labs"
    assert p.about == "Shipping things."
    assert p.location == "Bengaluru"
    assert p.url == "https://linkedin.com/in/bob"


def test_curious_coder_style_positions_with_organization() -> None:
    item = {
        "name": "Carol Chen",
        "positions": [{"position": "Co-founder", "organization": "Gamma"}],
        "profileUrl": "https://linkedin.com/in/carol",
        "posts": ["Looking for ML interns this summer."],
    }
    p = normalize(item)
    assert p.name == "Carol Chen"
    assert p.role == "Co-founder"
    assert p.company == "Gamma"
    assert p.recent_activity == ["Looking for ML interns this summer."]
    assert p.url == "https://linkedin.com/in/carol"


def test_missing_fields_dont_crash() -> None:
    p = normalize({"url": "https://linkedin.com/in/empty"})
    assert p.url == "https://linkedin.com/in/empty"
    assert p.name == ""
    assert p.role == ""
    assert p.recent_activity == []


def test_to_dict_drops_raw() -> None:
    p = normalize({"fullName": "X", "url": "u", "extra": "keep-in-raw"})
    d = p.to_dict()
    assert "raw" not in d
    assert d["name"] == "X"
