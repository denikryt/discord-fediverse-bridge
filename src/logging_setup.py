import logging
from typing import Optional, Iterable


def configure_logging(level: str, noisy_loggers: Optional[Iterable[str]] = None) -> None:
    """Configure root logging and silence noisy third-party loggers.

    :param level: logging level name for the root logger (e.g. 'INFO', 'DEBUG')
    :param noisy_loggers: optional iterable of logger names to silence (set to WARNING)
    """
    root_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Common noisy libraries during startup — raise them to WARNING by default
    default_noisy = [
        "httpcore",
        "httpx",
        "discord",
        "discord.gateway",
    ]

    for name in (noisy_loggers or default_noisy):
        logging.getLogger(name).setLevel(logging.WARNING)
