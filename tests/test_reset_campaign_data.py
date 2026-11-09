from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.db.models.outreach import Campaign
from app.db.session import SessionLocal


def _load_script(monkeypatch):
    path = (
        Path(__file__).resolve().parent.parent / "scripts" / "inactive" / "reset_campaign_data.py"
    )
    spec = importlib.util.spec_from_file_location("inactive_script_reset_campaign_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "configure_logging", lambda level: None)
    return module


def _exists(campaign_id: int) -> bool:
    with SessionLocal() as session:
        return session.get(Campaign, campaign_id) is not None


@pytest.mark.parametrize("argv", [[], ["--yes"]])
def test_refuses_to_run_without_campaign_ids(monkeypatch, argv):
    module = _load_script(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        module.main(argv)

    assert exit_info.value.code == 2


def test_names_every_unknown_campaign_id_and_deletes_nothing(db: None, monkeypatch, capsys):
    module = _load_script(monkeypatch)

    with pytest.raises(SystemExit):
        module.main(["--campaign-id", "999000002", "999000001", "--campaign-id", "999000003"])

    assert "[999000001, 999000002, 999000003]" in capsys.readouterr().err


def test_deletes_only_the_named_campaigns(db: None, monkeypatch):
    module = _load_script(monkeypatch)
    with SessionLocal() as session:
        named, other = Campaign(name="reset named"), Campaign(name="reset other")
        session.add_all([named, other])
        session.commit()
        named_id, other_id = named.campaign_id, other.campaign_id

    try:
        module.main(["--campaign-id", str(named_id)])
        assert _exists(named_id) and _exists(other_id)

        module.main(["--campaign-id", str(named_id), "--yes"])
        assert not _exists(named_id)
        assert _exists(other_id)
    finally:
        with SessionLocal() as session:
            session.query(Campaign).filter(Campaign.campaign_id.in_([named_id, other_id])).delete(
                synchronize_session=False
            )
            session.commit()
