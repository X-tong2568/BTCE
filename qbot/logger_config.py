# logger_config.py
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

from config import LOG_BACKUP_COUNT, LOG_DIR, LOG_LEVEL, LOG_MAX_BYTES


def setup_logging(name: str = "qbot", level: Optional[str] = None) -> logging.Logger:
    """
    作用：配置并返回项目日志器，日志同时写入 logs/<name>.log 和控制台。
    入参：name 日志器名称；level 可选，覆盖 config.LOG_LEVEL。
    出参：已配置好的 logging.Logger。
    """
    log_level = getattr(logging, (level or LOG_LEVEL).upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    logger.propagate = False

    # 重复调用时直接返回，避免同一条日志被多个 handler 重复输出。
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_DIR / (name + ".log"),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
