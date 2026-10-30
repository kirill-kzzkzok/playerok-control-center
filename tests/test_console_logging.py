import io
import logging
import os
import subprocess
import sys
from pathlib import Path

from app.security.logging import ConsoleFormatter, SecretFilter, ansi_supported


def test_live_console_file_rotation_no_duplicate_or_private_data(tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(Path.cwd()), "PCC_LOG_LEVEL": "DEBUG", "NO_COLOR": "1"}
    script = """
import logging
from app.security.logging import configure_logging,event
configure_logging(['dummy-token-secret','dummy-api-hash'])
configure_logging(['dummy-token-secret','dummy-api-hash'])
event('ORDER','Пакет 🧸 ×2, 🌹 ×1',order='sale-1',operation='operation-1',position=1)
logging.getLogger('pcc.shop').debug('username @Realbuyer api_hash=dummy-api-hash token=dummy-token-secret access_hash=987654321 session_string=private-string C:/private/account.session')
event('SUCCESS','Доставка подтверждена',order='sale-1',position=1)
logging.getLogger('aiogram.event').info('polling-noise')
for h in logging.getLogger().handlers:
    if hasattr(h,'maxBytes'):
        h.maxBytes=100
        h.backupCount=2
for i in range(12):
    logging.warning('Safe rotation event number %d',i)
logging.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert output.count("Пакет 🧸 ×2, 🌹 ×1") == 1
    assert "[ORDER" in output and "[SUCCESS" in output and "[DEBUG" in output
    assert "order_id=sale-1" in output and "gift_position=1" in output
    assert "polling-noise" not in output and "\x1b" not in output
    logs = list((tmp_path / "logs").glob("control_center.log*"))
    assert len(logs) == 3
    combined = output + "\n" + "".join(p.read_text(encoding="utf-8") for p in logs)
    for value in [
        "Realbuyer",
        "dummy-token-secret",
        "dummy-api-hash",
        "987654321",
        "private-string",
        "account.session",
    ]:
        assert value not in combined


def test_console_color_fallback_moscow_clock_and_control_character_sanitizing():
    stream = io.StringIO()
    assert ansi_supported(stream) is False
    record = logging.LogRecord("shop", logging.ERROR, "", 1, "@Realbuyer\nsecret=private\x1b[31m", (), None)
    record.created = 0
    record.order_id = "@Realbuyer\ninvalid"
    record.operation_id = "sensitive-token"
    SecretFilter(["sensitive-token"]).filter(record)
    rendered = ConsoleFormatter(color=False).format(record)
    assert rendered.startswith("[03:00:00]") and "\n" not in rendered and "\x1b" not in rendered
    assert "private" not in rendered and "sensitive-token" not in rendered
    colored = ConsoleFormatter(color=True).format(record)
    assert colored.startswith("\x1b[31m") and colored.endswith("\x1b[0m")


def test_debug_dict_private_fields_and_jwt_are_also_redacted():
    record = logging.LogRecord(
        "shop",
        logging.DEBUG,
        "",
        1,
        {
            "access_hash": 987654321,
            "session_string": "private session value",
            "api_hash": "unlisted-secret-hash",
            "cookie": "private cookie string",
            "token": "unlisted-token-secret",
        },
        (),
        None,
    )
    SecretFilter().filter(record)
    rendered = ConsoleFormatter().format(record)
    for value in [
        "987654321",
        "private session value",
        "unlisted-secret-hash",
        "private cookie string",
        "unlisted-token-secret",
    ]:
        assert value not in rendered


def test_windows_launcher_runs_foreground_once_and_keeps_error_visible():
    source = Path("start.bat").read_text(encoding="utf-8").lower()
    assert source.count(" -m app") == 1 and "python.exe -u -m app" in source
    assert "pythonunbuffered=1" in source and "chcp 65001" in source
    assert "pause" in source and "if errorlevel 1" in source
    assert "start " not in source and "pythonw" not in source
