import textwrap
from pathlib import Path

import pytest

from outreach.campaign import Campaign, list_campaigns, load_campaign


@pytest.fixture
def campaign_dir(tmp_path: Path) -> Path:
    (tmp_path / "internship.yaml").write_text(
        textwrap.dedent(
            """
            name: internship
            goal: Find an AI internship
            audience: AI startup CTOs in Delhi
            sender_bio: Samyak, Thapar student
            ask: 15-min chat?
            tone: Casual, 3-4 sentences
            attach_resume: true
            """
        ).strip(),
        encoding="utf-8",
    )
    return tmp_path


def test_load_campaign(campaign_dir: Path) -> None:
    c = load_campaign("internship", directory=campaign_dir)
    assert isinstance(c, Campaign)
    assert c.name == "internship"
    assert c.goal == "Find an AI internship"
    assert c.attach_resume is True


def test_list_campaigns(campaign_dir: Path) -> None:
    (campaign_dir / "vc-funding.yaml").write_text(
        "name: vc-funding\ngoal: x\naudience: x\nsender_bio: x\nask: x\ntone: x\n",
        encoding="utf-8",
    )
    assert list_campaigns(campaign_dir) == ["internship", "vc-funding"]


def test_missing_required_field(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("name: broken\ngoal: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required fields"):
        load_campaign("broken", directory=tmp_path)


def test_name_must_match_filename(tmp_path: Path) -> None:
    (tmp_path / "myfile.yaml").write_text(
        "name: othername\ngoal: x\naudience: x\nsender_bio: x\nask: x\ntone: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must match filename"):
        load_campaign("myfile", directory=tmp_path)


def test_campaign_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_campaign("nope", directory=tmp_path)


def test_real_internship_campaign_loads() -> None:
    """The starter campaign on disk should parse — catches accidental YAML breaks."""
    c = load_campaign("internship")
    assert c.name == "internship"
    assert "Samyak" in c.sender_bio
    assert c.attach_resume is True
