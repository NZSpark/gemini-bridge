"""发送 / 轮询 / 提取（``ChatIOMixin``）。

负责：把一条 prompt 送进网页输入框、轮询等待回复完成、提取代码块，
并在结束（或超时）后更新会话状态。重试阶梯与播种逻辑都在这里。
"""

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

from . import config
from .errors import (
    DEFAULT_SESSION_KEY,
    GeminiContextLimitError,
    GeminiTimeoutError,
)
from .prompting import _delta_piece, estimate_tokens


logger = logging.getLogger(__name__)


def prune_output_dir(output_dir: str) -> int:
    """按 config 的保留策略清理落盘目录（0 = 不限）。返回删除的文件数。

    调用点有两处（T7.2）：`save_extracted_files` 落盘前，以及服务启动 / 后台周期任务。
    此前只在落盘时调用，纯读取的长跑进程永远不会回收旧文件。
    失败不再静默：目录不可读 / 删不掉时至少留一条 warning（否则磁盘默默长满）。
    """
    max_files = config.OUTPUT_MAX_FILES
    max_age_days = config.OUTPUT_MAX_AGE_DAYS
    if not max_files and not max_age_days:
        return 0
    try:
        entries = [p for p in Path(output_dir).iterdir() if p.is_file()]
    except OSError as exc:
        logger.debug("清理落盘目录时无法读取 %s：%s", output_dir, exc)
        return 0
    removed = 0
    now = time.time()
    for path in entries:
        try:
            if max_age_days and now - path.stat().st_mtime > max_age_days * 86400:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.debug("删除过期文件 %s 失败：%s", path, exc)
            continue
    if max_files:
        try:
            remaining = sorted(
                (p for p in Path(output_dir).iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError as exc:
            logger.debug("统计落盘目录 %s 失败：%s", output_dir, exc)
            return removed
        for path in remaining[: max(0, len(remaining) - max_files)]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.debug("按数量上限删除 %s 失败：%s", path, exc)
                continue
    return removed


# 向后兼容：旧名字（既有调用 / 测试 / 文档里的复现命令都可能还在用）
_prune_output_dir = prune_output_dir


class ChatIOMixin:
    async def send_chat(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
    ) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        :param prompt: 增量 prompt（网页会话已有上下文时使用）
        :param seeded_prompt: 带完整历史的“播种”prompt（需要新开会话时使用，
                              未提供则退回 ``prompt``）
        :param key: 会话桶标识（按任务隔离会话）。不同 key 各自持有一条独立
                    的网页会话与页面，互不污染上下文；None 表示默认桶。

        **重试阶梯**（本项目不做 URL 恢复，重试即重开对话 + 播种）：

        1. 首级：直接用现有会话（若已达体积预算或上次到顶，先轮转到新会话）；
        2. 中间级：退避后重试同一个页面；
        3. 最高一级：**重开新对话 + 用播种 prompt 重放历史**。

        页面完全没有新回复（超时）最常见的原因是会话已到顶 / 已失效；
        由于我们无法可靠回填会话 URL，与其重开同一个会话，不如直接新开 + 播种。
        """
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        last_error: Optional[RuntimeError] = None

        # 额外的会话桶需要自己的页面（默认桶就是 self.page，不涉及创建）
        await self._ensure_page(bucket)

        for attempt in range(1, max_attempts + 1):
            state = self._state(bucket)
            if state.pending_rotation:
                # 体积超预算或上次检测到“到顶”：先轮转，再播种
                await self._start_new_session(bucket)
            elif attempt == 1:
                pass
            elif attempt < max_attempts:
                # 不做 URL 恢复：退避后在同一页面重试一次（页面可能只是慢）
                logger.info("[恢复] 第 %s/%s 次重试：等待后重试……", attempt, max_attempts)
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
            else:
                logger.warning("[恢复] 重试无效，改为开启新对话并重放历史……")
                await self._start_new_session(bucket)

            # 会话是新开的（或被轮转过）-> 必须播种，否则模型收不到任何上下文
            active_prompt = seeded if not self._state(bucket).has_history else prompt
            # 记录真正要发出的那份 prompt，供上层估算 usage（按桶隔离，避免并发串台）
            self._last_prompts[bucket] = active_prompt

            await self._remember_session(bucket)
            try:
                return await self._send_chat_locked(active_prompt, on_delta, key=bucket)
            except GeminiContextLimitError as exc:
                # 到顶了：下次不要再恢复同一个会话，直接轮转
                last_error = exc
                self._state(bucket).pending_rotation = True
                logger.warning(
                    "[恢复] 第 %s/%s 次失败：会话已达上下文上限。", attempt, max_attempts
                )
            except GeminiTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                self._state(bucket).last_error = str(exc)
                logger.warning(
                    "[恢复] 第 %s/%s 次失败：等待回复超时。", attempt, max_attempts
                )

        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

    @staticmethod
    def _strip_code_noise(code_content: str, lang: str) -> str:
        """剥离 Gemini 代码块界面噪声，但保留首尾空白。

        旧实现直接 .strip()，会无条件抹掉开头/结尾的空白与空行。
        对 README.md 这类要按原文匹配再改的文件是致命的：
        首行空行、末尾换行被删后，edit 工具逐字节匹配就会失败。

        这里只去掉头部的语言标签行与 Copy/Download 行，
        正文的空白、空行、首尾换行全部原样保留。
        """
        text = code_content.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        lang_alt = re.escape(lang) if lang else r"[A-Za-z0-9_+#.-]*"
        lang_line_re = re.compile(
            r"^\s*(?:" + lang_alt + r"|bash|shell|sh|python|py|json|html|javascript|js|"
            r"typescript|ts|css|sql|go|rust|java|cpp|c|markdown|md|txt)\s*$",
            re.IGNORECASE,
        )
        copy_line_re = re.compile(
            r"^\s*(?:" + lang_alt + r"|bash|shell|sh|python|py|json|html|javascript|js)?"
            r"\s*(?:Copy|Download)\s*$",
            re.IGNORECASE,
        )
        # 头部：允许先跳过空行，再剥语言标签 / Copy 行；
        # 一旦遇到第一行正文就停，避免误删正文里同名的行。
        start = 0
        while start < len(lines):
            line = lines[start]
            if not line.strip():
                start += 1
                continue
            if lang_line_re.match(line) or copy_line_re.match(line):
                start += 1
                continue
            break
        end = len(lines)
        while end > start:
            last = lines[end - 1]
            if last.strip() and copy_line_re.match(last):
                end -= 1
                continue
            break
        return "\n".join(lines[start:end])

    async def _extract_code_blocks(self, element) -> List[dict]:
        """从某条回复的 DOM 节点中提取代码块（语言 + 纯代码文本）。"""
        extracted: List[dict] = []
        if element is None:
            return extracted
        code_elements = await element.query_selector_all(config.CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(config.CODE_TAG_SELECTOR)
            lang = "txt"
            if code_tag:
                class_attr = await code_tag.get_attribute('class') or ""
                lang_match = re.search(r'language-(\w+)', class_attr)
                if lang_match:
                    lang = lang_match.group(1)

            code_content = await self._complete_text(code_tag or code_el)
            clean_code = self._strip_code_noise(code_content, lang)

            extracted.append({"lang": lang, "code": clean_code})
        return extracted

    # 读取回复节点完整文本的 JS：Gemini 把流式回复按 token 渲染成一串
    # <span class="animating">，带逐字显现动画。Playwright 的 inner_text() 遵循
    # **渲染后**可见性，动画未走完的 token 取不到——表现为文本在引号/冒号处被截断
    # （TOOL_CALL 的 JSON 参数被切掉半截）。这里在克隆节点上移除动画类与 animation
    # 样式，挂到屏幕外再读 innerText，既拿到完整文本，又保留块级换行。
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
      holder.style.top = '0';
      holder.appendChild(clone);
      document.body.appendChild(holder);
      const text = clone.innerText || clone.textContent || '';
      holder.remove();
      return text;
    }
    """

    async def _complete_text(self, node) -> str:
        """读取回复节点的完整文本（绕过 Gemini 逐 token 显现动画导致的截断）。

        失败时退回 text_content（无块级换行但一定完整），再退回 inner_text。
        """
        if node is None:
            return ""
        try:
            text = await node.evaluate(self._COMPLETE_TEXT_JS)
            if text and text.strip():
                return text
        except Exception:
            pass
        try:
            text = await node.text_content()
            if text and text.strip():
                return text
        except Exception:
            pass
        try:
            return await node.inner_text()
        except Exception:
            return ""

    async def _has_pending_tokens(self, node) -> bool:
        """回复节点里是否还有尚未显现的 token（span.pending 等）。

        Gemini 流式渲染时，未显现 token 带 .pending / .animating 类，
        虽已进入 DOM 但 inner_text 取不到。停止按钮消失不代表这些 token
        已经显现完毕——若此时收尾，会拿到被截断的半截 JSON。
        """
        if node is None:
            return False
        try:
            return bool(await node.evaluate(
                "(n) => !!n.querySelector('.pending, .animating')"
            ))
        except Exception:
            return False

    _ENTER_JS = """
    (el) => {
        const opts = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                      bubbles: true, cancelable: true};
        el.dispatchEvent(new KeyboardEvent('keydown', opts));
        el.dispatchEvent(new KeyboardEvent('keypress', opts));
        el.dispatchEvent(new KeyboardEvent('keyup', opts));
        return true;
    }
    """

    async def _dispatch_enter(self, chat_input) -> bool:
        """在页面内对输入框派发 Enter 键事件（纯 DOM，不碰 OS 焦点）。

        不用 page.keyboard.press：那是全局键盘通道，窗口不在前台时按键会
        落到别的应用，逼得代码去抢前台（bring_to_front），干扰用户其它窗口。
        """
        if chat_input is None:
            return False
        try:
            return bool(await chat_input.evaluate(self._ENTER_JS))
        except Exception:
            return False

    async def _send_button(self, page):
        """定位发送按钮（多个候选选择器依次尝试），找不到返回 None。"""
        if page is None:
            return None
        for selector in config.SEND_BUTTON_SELECTORS:
            try:
                button = await page.query_selector(selector)
                if button:
                    return button
            except Exception:
                continue
        return None

    async def _click_send_button(self, page) -> bool:
        """点发送按钮：先 Playwright 原生 `click()`（真实鼠标事件，React 才认），
        失败再退化为 DOM `dispatch_event("click")`（同样不碰 OS 焦点）。

        真实故障（用户侧观察）：输入框里已有文字，但发送按钮没被点，网页不再产生回复。
        原因就是旧实现里 `_dispatch_enter` 无论网页有没有接受按键都返回 true，
        函数直接 return，**从不**走到这里。

        ``click()`` 需要按钮处于 enabled 状态；按钮被禁用时它会超时——那正是
        「编辑器还在处理长文本」的信号，在这里会被记成失败并进入下一次尝试。
        """
        button = await self._send_button(page)
        if button is None:
            return False
        timeout = config.FILL_TIMEOUT_MS or 5000
        try:
            await button.click(timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001  含禁用/不可见导致的超时
            logger.debug("发送按钮原生 click 失败（%s），改用 DOM 事件", exc)
        try:
            await button.dispatch_event("click")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送按钮 DOM click 也失败：%s", exc)
            return False

    async def _send_button_state(self, page) -> str:
        """发送按钮的可用性（best-effort 诊断）。"""
        button = await self._send_button(page)
        if button is None:
            return "未找到发送按钮"
        try:
            return str(await button.evaluate(self._BUTTON_STATE_JS))
        except Exception as exc:  # noqa: BLE001
            return f"（按钮状态不可读：{exc}）"

    async def _composer_text(self, chat_input) -> Optional[str]:
        """读取输入框当前文本（best-effort；读不到返回 None，表示“无法判断”）。"""
        if chat_input is None:
            return None
        try:
            text = await chat_input.evaluate(self._COMPOSER_TEXT_JS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取输入框文本失败：%s", exc)
            return None
        return text if isinstance(text, str) else None

    async def _prompt_submitted(self, page, chat_input, bucket: Optional[str] = None) -> Optional[bool]:
        """刚才那次提交是否真的生效？None = 无法判断（不据此报错）。

        判据（满足其一即为已提交）：

        * **输入框已清空**——网页接受了这条消息，最直接的证据；
        * 页面进入「生成中」（停止按钮出现）。

        读不到输入框内容（页面差异 / 元素不可读）时返回 None，调用方按“无法验证”
        处理，不把页面差异当成提交失败。
        """
        text = await self._composer_text(chat_input)
        if text is None:
            return None
        if not text.strip():
            return True
        if await self._page_is_generating(bucket or DEFAULT_SESSION_KEY):
            return True
        return False

    async def _wait_submitted(self, page, chat_input, bucket: Optional[str] = None) -> Optional[bool]:
        """等待「已提交」的迹象，最长 ``SUBMIT_VERIFY_MS``。

        为什么不能只读一次：Enter 派发后，网页要先更新内部状态、清空输入框、
        再开始生成，都有延迟；立即读会看到「输入框里还有字」而误判成没提交。
        若**根本读不到**输入框内容（None），立即返回 None：无法验证就不该空等。
        """
        deadline = asyncio.get_event_loop().time() + max(0.0, config.SUBMIT_VERIFY_MS / 1000.0)
        while True:
            submitted = await self._prompt_submitted(page, chat_input, bucket)
            if submitted is None:
                return None
            if submitted or asyncio.get_event_loop().time() >= deadline:
                return submitted
            await asyncio.sleep(0.1)

    # 读取输入框当前文本：textarea 用 value，contenteditable 用 textContent
    # （不用 innerText：它遵循“渲染后可见性”，窗口不可见时会读到空串，
    #  会让「输入框已清空」的判定变成假阳性）。
    _COMPOSER_TEXT_JS = """
    (el) => {
      if (typeof el.value === 'string') return el.value;
      return el.textContent || el.innerText || '';
    }
    """

    # 发送按钮的可用性（诊断用）
    _BUTTON_STATE_JS = """
    (el) => JSON.stringify({
      aria_label: el.getAttribute('aria-label'),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      connected: el.isConnected,
      size: (() => { const r = el.getBoundingClientRect();
                     return Math.round(r.width) + 'x' + Math.round(r.height); })(),
    })
    """

    # 输入框写入失败时的诊断脚本：把“为什么不可编辑”留下来，而不是只丢一个
    # Playwright 超时。字段都是 DOM 事实，不依赖具体选择器。
    _COMPOSER_DIAG_JS = """
    (el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const active = document.activeElement;
      return JSON.stringify({
        tag: el.tagName,
        ce: el.getAttribute('contenteditable'),
        aria_disabled: el.getAttribute('aria-disabled'),
        disabled: el.hasAttribute('disabled'),
        connected: el.isConnected,
        display: style.display,
        visibility: style.visibility,
        size: Math.round(rect.width) + 'x' + Math.round(rect.height),
        active: active ? active.tagName + (active === el ? '(self)' : '') : null,
      });
    }
    """

    async def _locate_input(self, page):
        """定位对话输入框：最多 3 轮 × 逐个候选选择器。

        只做 DOM 查询与重试，绝不 bring_to_front / focus：那会抢 OS 前台、
        干扰用户正在使用的其它窗口；而提交走页面内事件派发（见 _submit_prompt），
        本就不依赖窗口是否在前台。
        """
        for _round in range(3):
            for selector in config.INPUT_SELECTORS:
                try:
                    node = await page.wait_for_selector(selector, timeout=2000)
                    if node:
                        return node
                except Exception:
                    continue
        return None

    async def _composer_diag(self, handle) -> str:
        """收集输入框状态（best-effort：诊断本身绝不抛错）。"""
        try:
            return str(await handle.evaluate(self._COMPOSER_DIAG_JS))
        except Exception as exc:  # noqa: BLE001  拿到不到就退化成一句话
            return f"（诊断不可用：{exc}）"

    async def _fill_prompt(self, page, prompt: str):
        """把 prompt 写进输入框，返回可提交的句柄；每次尝试都**重新定位**。

        为什么不是“定位一次 + 一次 fill”（参照姊妹项目 ChatGPTBridge 的
        FILL_TIMEOUT_MS / FILL_RETRIES）：

        * ``fill`` 默认超时 30s。网页版重挂载 composer 后，我们手里的旧句柄会
          一直卡在“等它变得可见/可编辑”上直到超时——整轮请求直接失败，而客户端
          重试又拿到同样的失效句柄，把窗口期全部耗光（真实故障已复现）；
        * 所以每次尝试都重新定位（拿到重挂载后的新节点），单次超时收紧到
          ``FILL_TIMEOUT_MS``，失败后按 ``RETRY_BACKOFF_S`` 退避再试，
          最多 ``FILL_RETRIES`` 次；仍不行才报错，并附上输入框诊断。
        """
        attempts = max(1, config.FILL_RETRIES)
        timeout = config.FILL_TIMEOUT_MS or None
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            chat_input = await self._locate_input(page)
            if chat_input is None:
                last_error = RuntimeError("选择器均未命中输入框")
                logger.warning("写入输入框失败（第 %s/%s 次）：找不到输入框", attempt, attempts)
            else:
                try:
                    await chat_input.fill(prompt, timeout=timeout)
                    return chat_input
                except Exception as exc:  # noqa: BLE001  Playwright TimeoutError 等
                    last_error = exc
                    logger.warning(
                        "写入输入框失败（第 %s/%s 次，prompt %s 字符）：%s；输入框状态=%s",
                        attempt, attempts, len(prompt), exc,
                        await self._composer_diag(chat_input),
                    )
            if attempt < attempts:
                await asyncio.sleep(max(0.0, config.RETRY_BACKOFF_S))
        raise RuntimeError(
            f"写入输入框连续失败 {attempts} 次（单次超时 {config.FILL_TIMEOUT_MS}ms）："
            "命中的元素始终不处于「可见 / 可编辑」状态。常见原因：登录态失效或被风控"
            "拦住、页面停在非对话视图、有头模式下窗口失焦后输入框被懒卸载"
            "（可试 HEADLESS=1），或单个 prompt 超出输入框字符上限"
            "（可调小 PROMPT_MAX_CHARS / TOOL_RESULT_MAX_CHARS）。"
        ) from last_error

    async def _submit_prompt(self, page, chat_input, bucket: Optional[str] = None) -> None:
        """提交 prompt，并**确认它真的发出去了**（全程无窗口焦点依赖）。

        为什么必须确认（用户报告的真实故障）：`_dispatch_enter` 内嵌的 JS 只要把事件
        派发出去就 `return true`，而旧实现据此直接 `return`——**永远走不到点击发送按钮的
        兑底路径**。当网页没接住这个合成按键时（长文本刚 `fill` 进去、编辑器还没接管，
        或发送按钮处于 disabled），输入框里就是“文字在、消息没发”，网页不产生任何回复，
        客户端只能干等到超时（用户侧看到的就是“prompt 在输入框里但没发送”）。

        阶梯（每次尝试后都用 `_wait_submitted` 验证）：
          1. 派发 Enter；
          2. 点真正的发送按钮（先原生 click，再 DOM click）；
          3. 再派发一次 Enter（给编辑器消化大文本留出时间）。
        三次都验证不到提交 -> 抛可行动错误（附按钮状态），不再静默等待。
        """
        plan = ("Enter", "发送按钮", "Enter")
        last_detail = "（未知）"
        for index, action in enumerate(plan, start=1):
            if action == "Enter":
                await self._dispatch_enter(chat_input)
            else:
                await self._click_send_button(page)
            submitted = await self._wait_submitted(page, chat_input, bucket)
            if submitted is not False:
                if index > 1:
                    logger.info("[提交] 第 %s 次尝试（%s）成功。", index, action)
                return
            remaining = await self._composer_text(chat_input)
            last_detail = (
                f"输入框仍有 {len(remaining)} 字符" if remaining is not None else "无法读取输入框"
            )
            logger.warning(
                "[提交] 第 %s/%s 次尝试（%s）无效：%s；发送按钮=%s",
                index, len(plan), action, last_detail, await self._send_button_state(page),
            )
        raise RuntimeError(
            f"prompt 已写入输入框但未能提交（尝试 {len(plan)} 次：Enter → 发送按钮 → Enter）；"
            f"{last_detail}。常见原因：发送按钮处于 disabled（编辑器还在消化长文本/粘贴）、"
            "页面停在非对话视图，或按键被网页忽略。可调大 SUBMIT_VERIFY_MS；"
            "若与长 prompt 相关，可调小 PROMPT_MAX_CHARS / TOOL_RESULT_MAX_CHARS。"
        )

    @staticmethod
    def _clamp_prompt(prompt: str) -> str:
        """发送侧最后一道护栏：把整段 prompt 压到输入框能承受的字符上限内。

        诚实标注：这里的阈值**是实测过的**了（T11.3 真机探测，见
        ``tests/e2e/probe_prompt_limit.py`` 与 doc/update.md §16）：composer 能吃下
        20K / 60K / 100K 字符的单条 prompt（含 5×20KB 工具结果的 100KB prompt），
        因此 ``PROMPT_MAX_CHARS`` 不是「输入框物理上限」，而是**我们主动设的预算**；
        真正让它生效的地方在 ``prompting.build_prompt``（拼装即按预算告警），
        这里只是最后兜底。

        触发时**必定留一条 warning**（此前静默）：它意味着中间有一段内容真的被切掉了，
        工具结果可能被拦腰截断，模型可能因此答非所问——不看日志就无从察觉。
        """
        limit = config.PROMPT_MAX_CHARS
        if not limit or len(prompt) <= limit:
            return prompt
        head = limit // 2
        tail = limit - head
        dropped = len(prompt) - limit
        logger.warning(
            "[截断] prompt %s 字符超出 PROMPT_MAX_CHARS=%s：已省略中间 %s 字符"
            "（保留头部 %s + 尾部 %s）。工具结果可能被拦腰截断，模型可能答非所问。",
            len(prompt), limit, dropped, head, tail,
        )
        return (
            prompt[:head]
            + f"\n\n…（prompt 过长，已省略中间 {dropped} 字符）\n\n"
            + prompt[-tail:]
        )

    async def _send_chat_locked(self, prompt: str, on_delta=None,
                                key: Optional[str] = None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        :param key: 会话桶标识（决定使用哪一条页面）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        page = self._page_for(bucket)
        state = self._state(bucket)
        # 默认所有桶共用 self.lock（串行）；只有 PARALLEL_BUCKETS=true 才按桶各持一把锁
        async with self._session_lock(bucket):
            if page is None:
                raise RuntimeError("浏览器尚未初始化：找不到可用于发送的会话页面。")
            self._touch_page(bucket)  # 正在用的页面不会被空闲回收 / LRU 淘汰

            # 1. 先确认输入框存在（快速失败，给出明确的“没登录/没打开”提示）；
            #    真正写入时还会在 _fill_prompt 里**每次尝试重新定位一次**。
            if not await self._locate_input(page):
                raise RuntimeError("无法找到对话输入框，请检查 Gemini 网页是否打开或处于登录状态。")

            # 记录发送前最后一条回复的文本，用来判断“新回复是否已经出现”。
            # 注意：绝不能用“回复节点数量变多”来判断。
            # Gemini 的消息列表会回收/替换节点，长会话下节点数可能恒为 2，
            # 新回复只会把旧节点内容改掉而不会让数量增长，
            # 那样会导致永远读不到本轮回复直接等到超时。
            before_text = ""
            before_count = 0
            try:
                before_nodes = await page.query_selector_all(config.RESPONSE_SELECTORS)
                before_count = len(before_nodes)
                if before_nodes:
                    before_text = (await self._complete_text(before_nodes[-1])).strip()
            except Exception:
                before_text = ""

            prompt = self._clamp_prompt(prompt)
            # 每次发送都留一行长度：这是回答「长 prompt 是否导致上游不响应」的现场证据
            # （此前只有失败时才打印长度，成功发出的那条到底多长无从得知）。
            logger.info("[发送] bucket=%s prompt=%s 字符", bucket, len(prompt))
            chat_input = await self._fill_prompt(page, prompt)
            await self._submit_prompt(page, chat_input, bucket)

            # 2. 轮询等待回复完成
            await asyncio.sleep(config.POLL_INTERVAL_S)
            last_text = ""
            last_normalized = ""
            last_len = -1
            streamed = ""            # 已经通过 on_delta 发给客户端的内容
            stable_count = 0
            saw_generating = False      # 本轮是否观测到过页面「生成中」状态
            latest_node = None          # 本轮最新的回复节点
            poll = 0
            deadline = asyncio.get_event_loop().time() + config.RESPONSE_TIMEOUT_S

            cap_check_every = max(1, config.CAP_CHECK_EVERY)

            # 页面「完全卡住」的判定：连续这么多次轮询既无正文、也无生成中信号，
            # 就不必干等到总超时——直接按超时处理（给出可排查的错误）。
            stall_limit = max(1, int(config.STALL_POLLS))
            stalled = 0

            while True:
                poll += 1
                responses = await page.query_selector_all(config.RESPONSE_SELECTORS)
                current_text = ""
                generating = None
                # 取「最后一个有正文的回复节点」而不是裸的 responses[-1]：
                # RESPONSE_SELECTORS 里 div[class*="response"] 之类会命中大量只有
                # 布局、没有文本的容器节点，排在真正的回复节点之后，inner_text()
                # 恒为空——若直接取 [-1] 会永远读到空串，导致轮询空转到超时。
                for node in reversed(responses):
                    try:
                        node_text = await self._complete_text(node)
                    except Exception:
                        continue
                    if node_text and node_text.strip():
                        latest_node = node
                        current_text = node_text
                        break
                normalized = current_text.strip()

                # 1. 本轮回复是否已经出现。判据（满足其一即可）：
                #    a) 末节点文本 != 发送前文本；
                #    b) 节点数变多（短会话常见）；
                #    c) 已经观测到过「生成中」——这说明本轮确已开始，
                #       此时即使文本暂时等于 before_text（首帧还没渲染完）也算已出现。
                #    注意：不能只看节点数——长会话下新回复会原地替换旧节点，数量不增长。
                #    也不能要求文本非空——选择器可能命中一批尚未渲染出文本的节点
                #    （表现为 nodes 很多但 len=0），那样会永远判不到「已出现」、空转到超时。
                reply_seen = (
                    (bool(normalized) and normalized != before_text)
                    or (len(responses) > before_count)
                    or saw_generating
                )

                # 1.1 还没有新回复时，周期性检查是否“会话到顶”，
                #     并主动探测「生成中」：这是唯一能证明本轮已开始、
                #     但首帧文本尚未渲染出来的信号（否则只能干等到超时）。
                #     到顶与“真的卡住”在外表上完全一样（页面不再产生新回复），
                #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                if not reply_seen:
                    if poll % cap_check_every == 0:
                        if await self._page_shows_context_limit(bucket):
                            self._mark_context_limit(bucket)
                            raise self._context_limit_error()
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True

                if reply_seen:
                    # 2.1 主判定：页面「生成中」状态。一旦观测到过「停止生成」
                    #     控件、又发现它消失，就说明生成真正结束，可立即收尾
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True
                    elif generating is False and saw_generating and normalized:
                        # 停止按钮消失也要确认没有尚未显现的 token，
                        # 否则会读到被截断的半截回复（如 TOOL_CALL 的 JSON 参数）。
                        if await self._has_pending_tokens(latest_node):
                            if config.DEBUG:
                                logger.debug(
                                    "poll=%s 停止按钮已消失，但仍有 pending token，继续等待", poll
                                )
                            # 落到下面的稳定判定 / 下一轮轮询
                        else:
                            last_text = current_text
                            if config.DEBUG:
                                logger.debug("poll=%s 停止按钮已消失且无 pending，判定结束", poll)
                            break

                    # 2.2 兜底判定：文本一模一样算一轮不变；
                    #     仅长度不再增长也算，但要更保守（多等几轮），
                    #     以免尾部重排 / 工具栏插入导致永远等不到逐字相等
                    # 空文本（首帧未渲染）不算「稳定」，否则会把空串当结果收尾
                    same_text = bool(normalized) and normalized == last_normalized
                    same_len = bool(normalized) and len(normalized) == last_len
                    if same_text or same_len:
                        stable_count += 1
                        threshold = config.STABLE_POLLS if same_text else config.LEN_STABLE_POLLS
                        if stable_count >= threshold:
                            last_text = current_text
                            if config.DEBUG:
                                logger.debug(
                                    "poll=%s 内容稳定 %s 次（same_text=%s），判定结束",
                                    poll, stable_count, same_text,
                                )
                            break
                    else:
                        stable_count = 0

                    # 2.3 生成过程中吐出增量，供 SSE 使用。
                    #     用「已发送内容」的公共前缀做 diff，即使节点中途重排也不会漏字
                    if on_delta is not None:
                        piece, streamed = _delta_piece(streamed, current_text)
                        if piece:
                            await on_delta(piece)

                    last_text = current_text
                    last_normalized = normalized
                    last_len = len(normalized)

                # 卡死检测：既没有正文，也没有任何「生成中」信号 -> 累计；
                # 一旦达到阈值即可快速失败，而不是把 180s 全部耗在空转上。
                if normalized or saw_generating or generating:
                    stalled = 0
                else:
                    stalled += 1
                if stalled >= stall_limit:
                    await self._remember_session(bucket)
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise GeminiTimeoutError(
                        f"页面连续 {stalled} 次未产生任何回复内容（疑似未登录或会话失效），"
                        f"已提前中止。本轮 prompt {len(prompt)} 字符。"
                        "请检查 Gemini 登录状态或 RESPONSE_SELECTORS 配置。"
                    )

                if config.DEBUG:
                    logger.debug(
                        "poll=%s nodes=%s len=%s stable=%s generating=%s saw=%s stalled=%s before_len=%s",
                        poll, len(responses), len(normalized), stable_count,
                        generating, saw_generating, stalled, len(before_text),
                    )

                # 总超时判定：若这期间其实已经读到实质回复，就直接返回已产生的内容，
                # 绝不再把同一句 prompt 重发一遍（避免网页多出一轮、与客户端状态错位）
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session(bucket)
                    if last_text:
                        logger.warning("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    # 超时前最后确认一次是否“到顶”，否则错误信息会误导排查方向
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise GeminiTimeoutError(
                        f"等待 Gemini 响应超时（{int(config.RESPONSE_TIMEOUT_S)}s）。"
                        f"本轮 prompt {len(prompt)} 字符。"
                    )

                await asyncio.sleep(config.POLL_INTERVAL_S)

            # 3. 从最新回复节点中提取代码块
            extracted_blocks = await self._extract_code_blocks(latest_node)

            # 4. 更新会话状态：已建立历史，并累计体积；超预算则下一轮轮转
            state.has_history = True
            state.turns += 1
            state.est_tokens += estimate_tokens(prompt) + estimate_tokens(last_text)
            state.last_error = None
            if self._session_over_budget(bucket):
                state.pending_rotation = True
                logger.info(
                    "[轮转] 会话已达预算（轮数=%s，估算 token=%s），下一轮将开启新会话并播种上下文。",
                    state.turns, state.est_tokens,
                )

            # 成功产生回复后：刷新会话状态（可能刚创建了新会话）并续期页面使用时间
            self._touch_page(bucket)
            await self._remember_session(bucket)
            return last_text, extracted_blocks

    @staticmethod
    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        prune_output_dir(output_dir)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        # 同一秒内的多个请求会拿到同样的 timestamp，必须再加一段随机后缀，
        # 否则 code_<ts>_1.py / response_<ts>.md 会互相覆盖（多任务并行后很常见）
        unique = uuid.uuid4().hex[:6]

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}_{unique}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                logger.info("[已保存文件] %s", filepath)
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved
