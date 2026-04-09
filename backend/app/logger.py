import sys
import json
import logging
from datetime import datetime


class StructuredLogger(logging.Logger):
    """Logger that emits structured JSON lines to stdout."""

    def _log_json(self, level: str, msg: str, **extra):
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level":     level,
            "message":   msg,
            **extra,
        }
        print(json.dumps(record, default=str), file=sys.stdout, flush=True)

    def info(self, msg, *args, **kwargs):       # type: ignore[override]
        extra = kwargs.pop("extra", {})
        super().info(msg, *args, **kwargs)
        self._log_json("INFO", str(msg), **extra)

    def warning(self, msg, *args, **kwargs):    # type: ignore[override]
        extra = kwargs.pop("extra", {})
        super().warning(msg, *args, **kwargs)
        self._log_json("WARNING", str(msg), **extra)

    def error(self, msg, *args, **kwargs):      # type: ignore[override]
        extra = kwargs.pop("extra", {})
        super().error(msg, *args, **kwargs)
        self._log_json("ERROR", str(msg), **extra)


logging.setLoggerClass(StructuredLogger)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

logger: StructuredLogger = logging.getLogger("slackbot")  # type: ignore[assignment]
