"""发送 / 轮询 / 提取（``ChatIOMixin``）。

负责：把一条 prompt 送进网页输入框、轮询等待回复完成、提取代码块，
并在结束（或超时）后更新会话状态。重试阶梯与播种逻辑都在这里。
"""

import asyncio
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
                print(f"[恢复] 第 {attempt}/{max_attempts} 次重试：等待后重试……")
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
            else:
                print("[恢复] 重试无效，改为开启新对话并重放历史……")
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
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：会话已达上下文上限。")
            except GeminiTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                self._state(bucket).last_error = str(exc)
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：等待回复超时。")

        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

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
            clean_code = re.sub(
                r'^(?:' + lang + r'|bash|python|json|html|javascript)?\s*(?:Copy|Download)\s*\n',
                '', code_content, flags=re.IGNORECASE
            ).strip()

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
            # 1. 定位并填入输入框
            chat_input = None
            for selector in config.INPUT_SELECTORS:
                try:
                    chat_input = await page.wait_for_selector(selector, timeout=3000)
                    if chat_input:
                        break
                except Exception:
                    continue

            if not chat_input:
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

            await chat_input.fill(prompt)
            await page.keyboard.press("Enter")

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
                                print(f"[debug] poll={poll} 停止按钮已消失，但仍有 pending token，继续等待")
                            # 落到下面的稳定判定 / 下一轮轮询
                        else:
                            last_text = current_text
                            if config.DEBUG:
                                print(f"[debug] poll={poll} 停止按钮已消失且无 pending，判定结束")
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
                                print(
                                    f"[debug] poll={poll} 内容稳定 {stable_count} 次"
                                    f"（same_text={same_text}），判定结束"
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
                        "已提前中止。请检查 Gemini 登录状态或 RESPONSE_SELECTORS 配置。"
                    )

                if config.DEBUG:
                    print(
                        f"[debug] poll={poll} nodes={len(responses)} len={len(normalized)} "
                        f"stable={stable_count} generating={generating} saw={saw_generating} "
                        f"stalled={stalled} before_len={len(before_text)}"
                    )

                # 总超时判定：若这期间其实已经读到实质回复，就直接返回已产生的内容，
                # 绝不再把同一句 prompt 重发一遍（避免网页多出一轮、与客户端状态错位）
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session(bucket)
                    if last_text:
                        print("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    # 超时前最后确认一次是否“到顶”，否则错误信息会误导排查方向
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise GeminiTimeoutError(
                        f"等待 Gemini 响应超时（{int(config.RESPONSE_TIMEOUT_S)}s）。"
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
                print(
                    f"[轮转] 会话已达预算（轮数={state.turns}，"
                    f"估算 token={state.est_tokens}），"
                    "下一轮将开启新会话并播种上下文。"
                )

            # 成功产生回复后：刷新会话状态（可能刚创建了新会话）并续期页面使用时间
            self._touch_page(bucket)
            await self._remember_session(bucket)
            return last_text, extracted_blocks

    @staticmethod
    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
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
                print(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved
