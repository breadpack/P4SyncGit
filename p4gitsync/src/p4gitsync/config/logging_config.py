import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })


def setup_logging(level: str = "INFO", fmt: str = "json", log_file: str = "") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler()
    if fmt == "json":
        stream_handler.setFormatter(JsonFormatter())
    handlers.append(stream_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        if fmt == "json":
            file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)

    logging.basicConfig(level=log_level, handlers=handlers)
