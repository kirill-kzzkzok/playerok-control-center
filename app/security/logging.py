import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo


class SecretFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def filter(self, record):
        for name in ("order_id", "operation_id", "gift_position"):
            if getattr(record, name, None) is not None:
                value = str(getattr(record, name))
                for secret in self.secrets:
                    value = value.replace(secret, "[REDACTED]")
                value = re.sub(r"@[A-Za-z0-9_]{3,32}", "[RECIPIENT]", value)
                setattr(record, name, re.sub(r"[^A-Za-z0-9_\-\[\]]", "?", value)[:100])
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
        message = re.sub(
            r"""(?i)(token|__ddg\w*|authorization|cookie)['"]?\s*[=:]\s*(?:'[^']*'|"[^"]*"|[^\s;,}]+)""",
            r"\1=[REDACTED]",
            message,
        )
        message = re.sub(
            r"""(?i)(api_hash|access_hash|session_string|secret|stock)['"]?\s*[=:]\s*(?:'[^']*'|"[^"]*"|[^\s;,}]+)""",
            r"\1=[REDACTED]",
            message,
        )
        message = re.sub(r"(?i)(?:[A-Z]:)?[^\s]*\.session(?:-\w+)?", "[SESSION]", message)
        message = re.sub(r"@[A-Za-z0-9_]{3,32}", "@[скрыто]", message)
        message = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT]", message)
        message = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x1f\x7f]", " ", message)
        record.msg, record.args = message, ()
        # Library tracebacks may contain request URLs with credentials.
        record.exc_info = None
        record.exc_text = None
        return True


class ConsoleFormatter(logging.Formatter):
    COLORS: ClassVar = {
        logging.DEBUG: "90",
        logging.INFO: "36",
        logging.WARNING: "33",
        logging.ERROR: "31",
        logging.CRITICAL: "1;31",
    }

    def __init__(self, color=False):
        super().__init__(datefmt="%H:%M:%S")
        self.color = color

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, ZoneInfo("Europe/Moscow")).strftime(
            datefmt or "%H:%M:%S"
        )

    def format(self, record):
        category = getattr(record, "event_type", record.levelname)
        context = " ".join(
            f"{key}={getattr(record, key)}"
            for key in ("order_id", "operation_id", "gift_position")
            if getattr(record, key, None) is not None
        )
        text = f"[{self.formatTime(record, self.datefmt)}] [{category:<8}] {record.getMessage()}"
        if context:
            text += " · " + context
        color = {"SUCCESS": "32", "GIFT": "35", "CHECK": "34", "ORDER": "36"}.get(
            category, self.COLORS.get(record.levelno, "0")
        )
        return f"\033[{color}m{text}\033[0m" if self.color else text


def ansi_supported(stream):
    if os.environ.get("NO_COLOR") or not stream.isatty():
        return False
    if os.name != "nt":
        return os.environ.get("TERM") != "dumb"
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        return bool(
            ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            and ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4)
        )
    except (OSError, AttributeError):
        return False


def event(category, message, *, order=None, operation=None, position=None, level=logging.INFO):
    logging.getLogger("pcc.shop").log(
        level,
        message,
        extra={
            "event_type": category,
            "order_id": order,
            "operation_id": operation,
            "gift_position": position,
        },
    )


def configure_logging(secrets=()):
    Path("logs").mkdir(exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("logs/control_center.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
    ]
    for index, handler in enumerate(handlers):
        handler.addFilter(SecretFilter(secrets))
        handler.setFormatter(ConsoleFormatter(color=index == 0 and ansi_supported(sys.stdout)))
    level = os.environ.get("PCC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
