"""直连 Gemini 网页版的独立客户端（E2E 对照基线）。

设计原则（见 doc/e2e_test_design.md §1.3）：

* 只与 bridge 共享 **配置**（.env 选择器、入口 URL），不复用其任何发送/轮询逻辑；
* 结束判定采用独立算法：「文本连续 N 轮不变 且 节点内无未显现 token」，
  不使用 bridge 的停止按钮 / 节点数 / 双阈值判据，避免用被测代码验证被测代码；
* 同步 Playwright API，单页面串行提问（风控）。
"""

import time

from playwright.sync_api import sync_playwright

from gemini_web import config
from gemini_web.errors import HOME_URL

# 入口 URL 前缀：导航后必须真的落在 Gemini 上。空白页 / 被重定向到登录、同意页
# 都要在测试代码里立刻报错，而不是留一个空白窗口让调用方去猜。
ENTRY_URL_PREFIX = "https://gemini.google.com"

POLL_S = max(0.5, config.POLL_INTERVAL_S)
# 独立稳定阈值：文本连续这么多轮逐字不变即认为生成结束
STABLE_REQUIRED = 8
# 硬上限：若 .pending/.animating 类迟迟不消失（误判风险），稳定这么多轮也强制收尾
STABLE_HARD_CAP = 20
# 回复节点内未显现 token 的标志（与 bridge 的判定条件同源，属 DOM 事实而非逻辑）
PENDING_SELECTOR = ".pending, .animating, .revealing"

# 读取回复完整文本：去掉动画类后读 innerText（与 bridge 同技巧、独立实现）。
# 动画未走完时 Playwright inner_text() 会丢 token，故本方法仅在 settle 后调用。
_COMPLETE_TEXT_JS = """
(node) => {
  const clone = node.cloneNode(true);
  clone.querySelectorAll('.animating, .pending, .revealing, .fade-in')
    .forEach(e => e.classList.remove('animating', 'pending', 'revealing', 'fade-in'));
  clone.querySelectorAll('[style]').forEach(e => {
    e.style.animation = 'none';
    e.style.opacity = '1';
    e.style.visibility = 'visible';
    e.style.filter = 'none';
    e.style.transform = 'none';
  });
  const holder = document.createElement('div');
  holder.style.position = 'absolute';
  holder.style.left = '-99999px';
  holder.appendChild(clone);
  document.body.appendChild(holder);
  const text = clone.innerText || clone.textContent || '';
  holder.remove();
  return text;
}
"""


class DirectGeminiClient:
    """单页面直连客户端：新开对话 -> 填入输入框 -> 独立轮询到稳定。"""

    def __init__(self, user_data_dir: str, headless: bool = True):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self._pw = None
        self.context = None
        self.page = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动浏览器并**显式导航到 Gemini 入口**。

        两个必须遵守的约束：

        * **一定要 goto(HOME_URL)**：持久化上下文的首个页面初值就是 ``about:blank``
          （profile 还可能恢复上次的标签页），不导航就永远停在空白页。
        * **失败也要关掉浏览器**：此前 start() 中途抛错时，半启动的 Chromium 无人
          回收，桌面上会留下一个停在 about:blank 的空白窗口（headed 模式尤其明显）。
        """
        self._pw = sync_playwright().start()
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)

            current = (self.page.url or "").strip()
            if not current.startswith(ENTRY_URL_PREFIX):
                title = ""
                try:
                    title = self.page.title() or ""
                except Exception:  # noqa: BLE001  页面可能已不可用，标题仅作诊断
                    pass
                raise RuntimeError(
                    f"直连页未落在 Gemini 入口：url={current!r} title={title!r}"
                    f"（期望 {HOME_URL}；空白页/登录页请检查 profile 副本的登录态）"
                )
            self.page.wait_for_selector(
                config.READY_SELECTOR, timeout=config.READY_TIMEOUT_MS, state="visible"
            )
        except Exception as exc:  # noqa: BLE001
            # 关键：先关掉浏览器再抛错，避免泄漏空白窗口
            self.close()
            raise RuntimeError(f"直连浏览器启动/导航失败（浏览器已关闭）：{exc}") from exc

    def close(self) -> None:
        for closer in (
            lambda: self.context.close() if self.context else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001  关闭失败不影响测试结论
                pass
        self.context = None
        self._pw = None

    # ---------- 页面操作 ----------

    def new_chat(self) -> None:
        """点击「新建对话」（回退链），点不到则沿用当前页。"""
        for selector in config.NEW_CHAT_SELECTOR.split("||"):
            selector = selector.strip()
            if not selector:
                continue
            try:
                button = self.page.wait_for_selector(selector, timeout=3000)
                if button:
                    button.click()
                    time.sleep(0.5)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _find_input(self):
        for selector in config.INPUT_SELECTORS:
            try:
                el = self.page.wait_for_selector(selector, timeout=3000)
                if el:
                    return el
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError("直连侧找不到输入框（检查登录态 / INPUT_SELECTORS）")

    def _last_reply_node(self):
        """最后一个**有文本**的回复节点（与 bridge 相同的 DOM 事实）。"""
        try:
            nodes = self.page.query_selector_all(config.RESPONSE_SELECTORS)
        except Exception:  # noqa: BLE001
            return None
        for node in reversed(nodes):
            try:
                text = (node.text_content() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return node
        return None

    @staticmethod
    def _read_final(node) -> str:
        """settle 后读取完整文本（去动画 -> innerText，退回 textContent）。"""
        try:
            text = node.evaluate(_COMPLETE_TEXT_JS)
            if text and text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            return (node.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            try:
                return (node.text_content() or "").strip()
            except Exception:  # noqa: BLE001
                return ""

    # ---------- 核心：发一轮并独立判定结束 ----------

    def ask(self, prompt: str, timeout_s: float = None, new_chat: bool = True) -> str:
        """发送 prompt 并返回本轮回复全文；超时且一无所获时抛错。"""
        if new_chat:
            self.new_chat()
        chat_input = self._find_input()

        before = ""
        node = self._last_reply_node()
        if node is not None:
            before = (node.text_content() or "").strip()

        chat_input.fill(prompt)
        self.page.keyboard.press("Enter")

        deadline = time.monotonic() + (timeout_s or config.RESPONSE_TIMEOUT_S)
        last_cmp = None
        stable = 0
        best = ""
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            node = self._last_reply_node()
            if node is None:
                continue
            try:
                current = (node.text_content() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not current or current == before:
                continue  # 回复尚未出现（文本仍是旧内容）
            best = current
            if current == last_cmp:
                stable += 1
            else:
                stable = 0
                last_cmp = current
            if stable < STABLE_REQUIRED:
                continue
            pending = node.query_selector(PENDING_SELECTOR) is not None
            if not pending or stable >= STABLE_HARD_CAP:
                return self._read_final(node)

        if best:
            # 与 bridge 一致的语义：超时但已有实质内容 -> 返回已产生的内容
            return best
        raise TimeoutError(f"直连侧 {int(timeout_s or config.RESPONSE_TIMEOUT_S)}s 内未收到任何回复")
