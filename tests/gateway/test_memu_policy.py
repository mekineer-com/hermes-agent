import json
from pathlib import Path

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Ensure gateway.memu_policy reads the per-test home, not the real ~/.hermes.
    from gateway import memu_policy as _module
    # Bust any module-level cache by re-importing lazily inside the function below.
    yield tmp_path


def _write_memu_json(hermes_home: Path, payload: dict) -> None:
    (hermes_home / "memu.json").write_text(json.dumps(payload), encoding="utf-8")


def test_policy_defaults_to_full_when_file_missing(hermes_home):
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("12345@s.whatsapp.net") == "full"


def test_policy_defaults_to_full_when_file_malformed(hermes_home):
    (hermes_home / "memu.json").write_text("not json", encoding="utf-8")
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("12345@s.whatsapp.net") == "full"


def test_policy_defaults_to_full_when_chat_not_listed(hermes_home):
    _write_memu_json(hermes_home, {"whatsapp": {"channels": {"99999@lid": {"policy": "excluded"}}}})
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("12345@s.whatsapp.net") == "full"


def test_policy_returns_excluded_for_exact_match(hermes_home):
    _write_memu_json(
        hermes_home,
        {"whatsapp": {"channels": {"12345@s.whatsapp.net": {"policy": "excluded"}}}},
    )
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("12345@s.whatsapp.net") == "excluded"


def test_policy_returns_listen_only_for_exact_match(hermes_home):
    _write_memu_json(
        hermes_home,
        {"whatsapp": {"channels": {"67890@g.us": {"policy": "listen_only"}}}},
    )
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("67890@g.us") == "listen_only"


def test_policy_matches_via_normalized_key_when_raw_differs(hermes_home):
    # Policy stored under the bare numeric form. Lookup with @s.whatsapp.net suffix
    # should still match because lookup tries normalized variant.
    _write_memu_json(
        hermes_home,
        {"whatsapp": {"channels": {"15133278228": {"policy": "listen_only"}}}},
    )
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("15133278228@s.whatsapp.net") == "listen_only"


def test_policy_ignores_unknown_policy_value(hermes_home):
    _write_memu_json(
        hermes_home,
        {"whatsapp": {"channels": {"12345@s.whatsapp.net": {"policy": "garbage"}}}},
    )
    from gateway.memu_policy import whatsapp_channel_policy
    assert whatsapp_channel_policy("12345@s.whatsapp.net") == "full"


def test_memorize_defaults_true_for_full_when_not_listed(hermes_home):
    from gateway.memu_policy import whatsapp_channel_memorize
    assert whatsapp_channel_memorize("12345@s.whatsapp.net") is True


def test_memorize_defaults_false_for_excluded_when_flag_missing(hermes_home):
    _write_memu_json(
        hermes_home,
        {"whatsapp": {"channels": {"12345@s.whatsapp.net": {"policy": "excluded"}}}},
    )
    from gateway.memu_policy import whatsapp_channel_memorize
    assert whatsapp_channel_memorize("12345@s.whatsapp.net") is False


def test_memorize_respects_explicit_false(hermes_home):
    _write_memu_json(
        hermes_home,
        {"whatsapp": {"channels": {"12345@s.whatsapp.net": {"policy": "listen_only", "memorize": False}}}},
    )
    from gateway.memu_policy import whatsapp_channel_memorize
    assert whatsapp_channel_memorize("12345@s.whatsapp.net") is False
