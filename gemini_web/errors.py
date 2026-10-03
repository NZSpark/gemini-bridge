"""异常与常量：会话 / 上游交互过程中可区分的失败类型。

集中放在这里，便于 ``driver`` 与各 mixin 模块共同引用，
也便于上层（``server`` / ``streaming``）只依赖异常类型而不依赖 Driver。
"""


class GeminiTimeoutError(RuntimeError):
    """等待网页版回复超时。区别于普通运行时错误，可触发会话恢复。"""


class GeminiContextLimitError(RuntimeError):
    """网页会话已达上下文长度上限（网页版会停止响应，必须换新会话）。"""


class GeminiBusyError(RuntimeError):
    """某个会话桶正忙（同一会话已有请求在跑且等待超时）。

    与「上游出错」区分开：这是本地的排队保护，客户端稍后重试即可，
    因此会被映射成 HTTP 503 / SSE ``upstream_busy``，而**不会**触发重试阶梯。
    """


# 未指定任务标识时使用的会话桶（保持与历史行为一致：全局共用一条会话）
DEFAULT_SESSION_KEY = "default"
# Gemini 网页版入口（每桶新开对话的落点）。
HOME_URL = "https://gemini.google.com/app"
