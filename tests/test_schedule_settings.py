import pytest
from web import credentials, db

_KEYS = {s["key"] for s in credentials.SETTINGS_REGISTRY}
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        "SCHEDULE_NIGHTLY_SCAN_TIME" not in _KEYS,
        reason="setting lives in a TIER:2 block — stripped below tier 2",
    ),
]


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    monkeypatch.delenv("SCHEDULE_NIGHTLY_SCAN_TIME", raising=False)
    db.init_db()


def test_registry_entry_properties():
    matches = [s for s in credentials.SETTINGS_REGISTRY if s["key"] == "SCHEDULE_NIGHTLY_SCAN_TIME"]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["secret"] is False
    assert entry["group"] == "Automation Schedule"


def test_mask_setting_non_secret_is_verbatim():
    assert credentials.mask_setting("SCHEDULE_NIGHTLY_SCAN_TIME", "21:15") == "21:15"


def test_list_settings_meta_shows_db_value_verbatim():
    db.set_app_setting("SCHEDULE_NIGHTLY_SCAN_TIME", "21:15")
    entry = next(
        s for s in credentials.list_settings_meta()["registry"] if s["key"] == "SCHEDULE_NIGHTLY_SCAN_TIME"
    )
    assert entry["has_value"] is True
    assert entry["masked"] == "21:15"


def test_entry_sits_inside_tier2_block():
    src = (credentials.__file__).replace(".pyc", ".py")
    text = open(src).read()
    begin = text.index("# TIER:2 BEGIN")
    key_pos = text.index("SCHEDULE_NIGHTLY_SCAN_TIME")
    end = text.index("# TIER:2 END")
    assert begin < key_pos < end
