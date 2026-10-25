"""步骤日志。后台进程和前台调用写到同一个文件。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_CONFIGURED = False


def log_path() -> Path:
    override = os.environ.get("SELLER_AUTO_LOG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "logs" / "seller-auto.log"


def configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("seller_auto")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if os.environ.get("SELLER_AUTO_DAEMON") != "1":
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    _CONFIGURED = True


def step(name: str, message: str = "") -> None:
    configure()
    text = f"[{name}] {message}".rstrip()
    logging.getLogger("seller_auto").info(text)
