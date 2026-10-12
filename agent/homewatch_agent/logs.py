"""Readable key=value logs (they show up in the app's Log tab in Home Assistant).

Logs carry counts and connection events only — never sensor values, entity names,
or keys.
"""

import logging
import sys
import time

_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


class KeyValueFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ")
        extras = " ".join(
            f"{key}={value}"
            for key, value in vars(record).items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        )
        line = f"{stamp} {record.levelname:<7} {record.getMessage()}"
        if extras:
            line = f"{line}  {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
