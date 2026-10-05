"""统一日志配置（T8.1）。

设计：

* 各模块一律 ``logger = logging.getLogger(__name__)`` 记录事件，不再直接 ``print``；
* 本模块把 ``gemini_web`` 包日志接到一个 stdout handler，格式带时间 / 级别 / 模块名，
  这样「谁在什么时候做了什么」不再需要靠 print 里的方括号前缀猜；
* ``GEMINI_DEBUG=1`` → ``DEBUG`` 级别（含逐轮轮询状态、keep-alive、会话键），
  否则 ``INFO``。级别只由这一个开关控制，避免出现“打了日志但看不到”。
* 在 pytest 下**不自动接管**（保持测试输出干净），需要时显式
  ``setup_logging(force=True)``。

``propagate=False``：日志已被本 handler 输出，不再向上冒泡，避免与 uvicorn /
``logging.basicConfig`` 配置的 root handler 重复打印两份。
"""

import logging
import sys
from typing import Optional

PACKAGE_LOGGER_NAME = "gemini_web"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# 标记本模块安装的 handler，重复调用时可幂等替换
_HANDLER_FLAG = "_gemini_bridge_handler"


def _running_under_pytest() -> bool:
    """是否由 pytest 收集执行（import 期即可判断，故看 ``sys.modules``）。"""
    return "pytest" in sys.modules


def resolve_level(debug: Optional[bool] = None) -> int:
    """``GEMINI_DEBUG`` → 日志级别（唯一事实来源：``config.DEBUG``）。"""
    if debug is None:
        from . import config

        debug = bool(config.DEBUG)
    return logging.DEBUG if debug else logging.INFO


def setup_logging(force: bool = False) -> logging.Logger:
    """安装包级 handler（幂等）。返回 ``gemini_web`` logger。"""
    package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    if not force and _running_under_pytest():
        return package_logger
    level = resolve_level()
    for existing in list(package_logger.handlers):
        if getattr(existing, _HANDLER_FLAG, False):
            package_logger.removeHandler(existing)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    handler.setLevel(level)
    setattr(handler, _HANDLER_FLAG, True)
    package_logger.addHandler(handler)
    package_logger.setLevel(level)
    package_logger.propagate = False
    return package_logger
