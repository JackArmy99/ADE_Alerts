"""Daily rotating logs in data/logs, with secrets redacted."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from .config import Settings


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
        return True


def setup(s: Settings, verbose: bool = False) -> None:
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactFilter(s.secrets())
    fh = TimedRotatingFileHandler(s.logs_dir / "radar.log", when="midnight", backupCount=30, encoding="utf-8")
    sh = logging.StreamHandler(sys.stderr)
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(redact)
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
