import logging
import logging.config


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "format": (
                '{"time": "%(asctime)s", "level": "%(levelname)s", '
                '"name": "%(name)s", "msg": "%(message)s"}'
            ),
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}


def configure_logging() -> None:
    logging.config.dictConfig(LOGGING_CONFIG)
