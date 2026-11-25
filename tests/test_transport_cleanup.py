"""Supported transport selection never depends on an external proxy executable."""

import pytest

from app.config import Config
from app.gifts.settings import GiftSettings


def settings_file(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123:" + "x" * 35 + "\nPLAYEROK_HEADER_STRING=token=fake\n", encoding="utf-8"
    )
    return path


def test_both_clients_default_to_system_transport(tmp_path):
    path = settings_file(tmp_path)
    assert Config.load(str(path)).telegram_transport == "direct"
    assert GiftSettings.load(str(path)).telegram_transport == "direct"


@pytest.mark.parametrize("mode", ["unsupported", "local" + "vpn"])
def test_unsupported_explicit_transport_never_falls_back(tmp_path, mode):
    path = settings_file(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("TELEGRAM_TRANSPORT=" + mode + "\n")
    with pytest.raises(ValueError):
        Config.load(str(path))
    with pytest.raises(ValueError):
        GiftSettings.load(str(path))
