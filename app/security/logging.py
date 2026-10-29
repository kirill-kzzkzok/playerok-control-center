import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SecretFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
        message = re.sub(
            r"(?i)(token|__ddg\w*|authorization|cookie)\s*[=:]\s*[^\s;,]+", r"\1=[REDACTED]", message
        )
        record.msg, record.args = message, ()
        # Library tracebacks may contain request URLs with credentials.
        record.exc_info = None
        record.exc_text = None
        return True


def configure_logging(secrets=()):
    Path("logs").mkdir(exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler("logs/control_center.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
    ]
    for handler in handlers:
        handler.addFilter(SecretFilter(secrets))
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
