#!/usr/bin/env python3
"""目标页面侦察（Recon）——给「把 Web 页面包装成 OpenAI API」找选择器与路线。

用法::

    python3 recon.py --url https://chat.example.com/ --user-data ./user_data \
        --out ./recon-report.md [--wait 8] [--sample-gap 6] \
        [--sample-selector '.markdown'] [--network-log] [--headless]

做了什么：

1. 有头启动 Chromium（持久化 profile，复用登录态）；
2. 打开目标页，给你时间手动登录 / 打开一条已有的对话；
3. 采集候选：输入框、发送按钮、「生成中」控件、回复节点容器、代码块；
4. （给 ``--sample-gap`` 时）隔一段时间再采一次回复节点，判断是
   **原地流式更新**（数量不变、末节点文本变长）还是**追加新节点**；
5. （给 ``--network-log`` 时）记录页面自己发的 XHR/fetch 接口（方法 + 主机 + 路径 + 状态码 +
   content-type + 请求体字符数 + 响应 JSON 的**键名**），并给出「能不能走路线 A（直接复用
   站点自身接口）」的结论；
6. 输出 Markdown 报告与可选 JSON，可直接把选择器粘进 ``.env``。

脚本只读页面 DOM / 网络元信息，不写入、不提交、不回显对话正文（只报长度/结构）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

# 报告里有中文；在 LC_ALL=C 等非 UTF-8 终端下保证 print 不炸、不乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------- 采集 JS

# 只收集「结构信息」：标签、属性、class、可见性、文本长度。绝不带走正文内容。
_COLLECT_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const cls = (el) => {
    const c = el.className;
    if (typeof c === 'string') return c;
    if (c && typeof c.baseVal === 'string') return c.baseVal;
    return '';
  };
  const firstToken = (el) => (cls(el).trim().split(/\s+/)[0] || '');
  const brief = (el) => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || '',
    name: el.getAttribute('name') || '',
    type: el.getAttribute('type') || '',
    role: el.getAttribute('role') || '',
    placeholder: el.getAttribute('placeholder') || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    cls: cls(el).slice(0, 120),
    contenteditable: el.getAttribute('contenteditable') || '',
    visible: visible(el),
  });

  // 1) 候选输入框（页面退化时同页可能有多个编辑器，全部收集）
  const inputSel = 'textarea, input[type="text"], input:not([type]), ' +
                   '[contenteditable="true"], [contenteditable=""]';
  const inputs = [...document.querySelectorAll(inputSel)].map(brief);

  // 2) 候选按钮（含发送 / 停止）
  const buttons = [...document.querySelectorAll('button, [role="button"], [type="submit"]')]
    .map((el) => ({ ...brief(el), text: (el.textContent || '').trim().slice(0, 40) }));

  // 3) 「生成中」控件：aria-label/class 命中，或**叶子节点**自身文本命中
  //    （不要用容器聚合文本，否则一个包着 Send/Stop 的 div 也会被误报）
  const stopWords = ['stop', '停止', '生成中', 'generating', 'loading'];
  const stopCandidates = [];
  for (const el of document.querySelectorAll('button, [role="button"], [aria-label], span, svg, i, div')) {
    const aria = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase();
    const c = cls(el).toLowerCase();
    const ownText = (el.children.length === 0 ? (el.textContent || '') : '').toLowerCase();
    const ariaHit = stopWords.some((w) => aria.includes(w) || c.includes(w));
    const textHit = ownText && stopWords.some((w) => ownText.includes(w));
    if (!ariaHit && !textHit) continue;
    const r = el.getBoundingClientRect();
    stopCandidates.push({
      tag: el.tagName.toLowerCase(),
      cls: cls(el).slice(0, 80),
      ariaLabel: el.getAttribute('aria-label') || '',
      text: (el.textContent || '').trim().slice(0, 30),
      visible: visible(el),
      bottomHalf: r.top > window.innerHeight * 0.5,
    });
    if (stopCandidates.length >= 25) break;
  }

  // 4) 回复节点候选：按「标签.首个class」分组，统计出现次数与平均文本长度
  const groups = {};
  for (const el of document.querySelectorAll('article, [class], [role="listitem"], li')) {
    const text = el.innerText || '';
    if (text.length < 60) continue;
    const kids = el.querySelectorAll('*').length;
    if (kids > 400) continue;                       // 跳过整页级容器
    const key = el.tagName.toLowerCase() + (firstToken(el) ? '.' + firstToken(el) : '');
    const g = groups[key] || (groups[key] = { count: 0, total: 0, sample: [] });
    g.count += 1;
    g.total += text.length;
    if (g.sample.length < 3) g.sample.push(brief(el));
  }
  const responseCandidates = Object.entries(groups)
    .map(([selector, g]) => ({
      selector,
      count: g.count,
      avgTextLen: Math.round(g.total / Math.max(1, g.count)),
      sample: g.sample,
    }))
    .sort((a, b) => (b.count * Math.log(1 + b.avgTextLen)) - (a.count * Math.log(1 + a.avgTextLen)))
    .slice(0, 20);

  // 5) 代码块
  const pre = document.querySelectorAll('pre');
  const code = document.querySelectorAll('pre code, code, [data-language]');
  const langs = [...new Set([...code].map((el) => {
    const m = cls(el).match(/language-([\w+#-]+)/);
    return m ? m[1] : (el.getAttribute('data-language') || '');
  }).filter(Boolean))];

  return {
    meta: {
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      inputs: inputs.length,
      buttons: buttons.length,
      scrollHeight: document.documentElement.scrollHeight,
    },
    inputs,
    buttons,
    stopCandidates,
    responseCandidates,
    codeBlocks: { preCount: pre.length, codeCount: code.length, languages: langs },
  };
}
"""

# 采样回复节点（用于判断流式形态）
_SAMPLE_JS = r"""
(selector) => {
  let nodes = [];
  try { nodes = [...document.querySelectorAll(selector)]; } catch (e) { return null; }
  const last = nodes[nodes.length - 1];
  return {
    count: nodes.length,
    lastTextLen: last ? (last.innerText || '').length : 0,
    lastTailSha: last ? (last.innerText || '').slice(-80) : '',
  };
}
"""


# ------------------------------------------------- 站点自身接口（路线 A 判据）

# 静态资源后缀：这些请求与「复用接口」无关，别灌进报告
_STATIC_EXT = (
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".webm", ".avif",
)
# 记录哪些 JSON 键名（**只记键名，不记值**）——用来判断响应里有没有可提取的正文
_MAX_KEYS = 24


class NetworkLog:
    """记录页面自身发出的 XHR/fetch 接口骨架，判断能否走「直接复用接口」路线。

    只记：方法 / 主机 / 路径（去查询串）/ 状态码 / content-type / 请求体字符数 /
    响应 JSON 的**键名**。**不记任何请求体与响应值**——提示词与回复正文都不落地
    （与报告的脱敏约定一致）。

    注：本实现由同族项目（DeepseekBridge）的 recon 补齐——原先只记 method/host/path 时，
    「路线 A 是否可行」只能靠猜；状态码与响应键名才是它的证据。
    """

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._by_key: dict[tuple[str, str, str], dict] = {}

    def attach(self, page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _entry(self, request) -> dict | None:
        try:
            if request.resource_type not in ("xhr", "fetch", "websocket"):
                return None
            url = urlsplit(request.url)
            path = url.path or "/"
            if path.lower().endswith(_STATIC_EXT):
                return None
            key = (request.method, url.netloc, path)
        except Exception:  # noqa: BLE001
            return None
        entry = self._by_key.get(key)
        if entry is None:
            entry = {
                "method": key[0], "host": key[1], "path": key[2],
                "count": 0, "status": [], "contentType": "", "jsonKeys": [],
                "requestBodyChars": 0,
            }
            self._by_key[key] = entry
            self.entries.append(entry)
        entry["count"] += 1
        return entry

    def _on_request(self, request) -> None:
        entry = self._entry(request)
        if entry is None:
            return
        try:  # 只记长度，不记内容
            post = request.post_data
            if post:
                entry["requestBodyChars"] = max(entry["requestBodyChars"], len(post))
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, response) -> None:
        try:
            request = response.request
        except Exception:  # noqa: BLE001
            return
        entry = self._entry(request)
        if entry is None:
            return
        try:
            if response.status not in entry["status"]:
                entry["status"].append(response.status)
            ctype = (response.headers or {}).get("content-type", "")
            if ctype and not entry["contentType"]:
                entry["contentType"] = ctype.split(";")[0].strip()
        except Exception:  # noqa: BLE001
            return
        if "json" in entry["contentType"]:
            try:  # 异步取键名；拿不到就算了（不重试、不阻塞）
                asyncio.get_running_loop().create_task(self._sniff_keys(response, entry))
            except Exception:  # noqa: BLE001
                pass

    async def _sniff_keys(self, response, entry: dict) -> None:
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 — 非 JSON / body 已释放
            return
        keys: list[str] = []
        if isinstance(body, dict):
            keys = [str(k) for k in list(body.keys())[:_MAX_KEYS]]
        elif isinstance(body, list) and body and isinstance(body[0], dict):
            keys = [str(k) for k in list(body[0].keys())[:_MAX_KEYS]]
        for key in keys:
            if key not in entry["jsonKeys"]:
                entry["jsonKeys"].append(key)

    def summary(self, limit: int = 25) -> list[dict]:
        """按「请求体大小 → 请求次数」排序；生成类接口（有请求体）优先排在前面。"""
        rows = sorted(self.entries, key=lambda e: (-e["requestBodyChars"], -e["count"]))
        return rows[:limit]

    def route_verdict(self) -> tuple[str, list[dict]]:
        """路线 A（复用站点自身接口）是否可行，以及候选接口。"""
        candidates = [
            e for e in self.summary()
            if e["method"] in ("POST", "PUT") and (e["requestBodyChars"] > 0 or e["jsonKeys"])
        ]
        if not candidates:
            return ("未观察到 JSON POST 接口 → 走路线 B（DOM 自动化），无需再折腾接口回放。", [])
        return (
            "观察到 JSON POST 接口 → 可评估路线 A（直接复用接口，最稳）。"
            "先用 DevTools 对照一次请求/响应（本报告不采集正文），确认会话/鉴权字段怎么来，"
            "再决定 A（接口回放）或 C（DOM 登录 + 接口回放）。",
            candidates[:5],
        )


# ---------------------------------------------------------------- 报告生成


def _pick_input_selectors(inputs: list[dict]) -> list[str]:
    """从可见输入框里推导候选选择器（优先具体属性，最后才用裸标签）。"""
    out: list[str] = []
    for el in inputs:
        if not el.get("visible"):
            continue
        if el.get("placeholder"):
            out.append(f'{el["tag"]}[placeholder*="{el["placeholder"][:24]}"]')
        if el.get("id"):
            out.append(f'#{el["id"]}')
        if el.get("ariaLabel"):
            out.append(f'{el["tag"]}[aria-label*="{el["ariaLabel"][:24]}"]')
        if el.get("contenteditable") in ("true", ""):
            out.append('[contenteditable="true"]')
        if el["tag"] == "textarea":
            out.append("textarea")
    return _dedupe(out)


def _pick_send_selectors(buttons: list[dict]) -> list[str]:
    out: list[str] = []
    for el in buttons:
        if not el.get("visible"):
            continue
        label = (el.get("ariaLabel") + " " + el.get("text") + " " + el.get("cls")).lower()
        if el.get("type") == "submit":
            out.append('button[type="submit"]')
        if any(w in label for w in ("send", "发送", "提交", "submit")):
            if el.get("ariaLabel"):
                out.append(f'{el["tag"]}[aria-label*="{el["ariaLabel"][:24]}"]')
            elif el.get("text"):
                out.append(f'{el["tag"]}:has-text("{el["text"][:20]}")')
    return _dedupe(out)


def _pick_response_selectors(candidates: list[dict]) -> list[str]:
    out: list[str] = []
    for c in candidates:
        if c["count"] >= 2 and c["avgTextLen"] >= 60:
            out.append(c["selector"])
    return _dedupe(out)[:6]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _or(selectors: list[str], fallback: str = "") -> str:
    return "||".join(selectors) if selectors else fallback


def _guess_session_regex(url: str) -> str:
    # 把路径里的 uuid / 长十六进制段替换成占位符，供人确认
    m = re.search(r"/([a-z]+/)?([sct])/([0-9a-fA-F-]{8,})", url)
    if m:
        prefix = re.escape(m.group(0).rsplit("/", 1)[0] + "/")
        return f"{prefix}[0-9a-fA-F-]{{8,}}"
    return "<未识别：请手动从地址栏抄下会话 URL 的固定前缀与 ID 形态>"


def _streaming_verdict(first, second) -> str:
    if not first or not second:
        return "未采样（用 --sample-gap 再跑一次）"
    if second["count"] > first["count"]:
        return "**追加新节点**（回复节点数量增长）→ 结束判定用「末节点/节点数变化」"
    if second["count"] == first["count"] and second["lastTextLen"] > first["lastTextLen"]:
        return ("**原地流式更新**（数量不变、末节点文本变长）→ 结束判定**必须用内容变化**，"
                "不能看数量")
    return "两次采样无差异（可能没在生成，或选择器没命中）"


def _render_report(data: dict, sample: dict, network) -> str:
    """network 为 NetworkLog 实例（--network-log）或 None。"""
    meta = data["meta"]
    inputs = [el for el in data["inputs"] if el.get("visible")] or data["inputs"]
    input_sel = _pick_input_selectors(data["inputs"])
    send_sel = _pick_send_selectors(data["buttons"])
    resp_sel = _pick_response_selectors(data["responseCandidates"])
    code = data["codeBlocks"]

    lines: list[str] = []
    lines.append("# Recon 报告\n")
    lines.append(f"- URL: `{meta['url']}`")
    lines.append(f"- 标题: {meta['title']!r}    readyState: {meta['readyState']}")
    lines.append(f"- 可见输入框: {len(inputs)}   按钮: {len(data['buttons'])}   "
                 f"'生成中'候选: {len(data['stopCandidates'])}\n")

    default_input = 'textarea||[contenteditable="true"]'
    default_send = 'button[type="submit"]'
    default_ready = 'textarea, [contenteditable="true"]'
    lines.append("## 建议写入 .env 的选择器\n")
    lines.append("```dotenv")
    lines.append(f"INPUT_SELECTORS={_or(input_sel, default_input)}")
    lines.append(f"SEND_BUTTON_SELECTORS={_or(send_sel, default_send)}")
    lines.append(f"RESPONSE_SELECTORS={_or(resp_sel, '.markdown, article')}")
    lines.append(f"READY_SELECTOR={_or(input_sel[:2], default_ready)}")
    lines.append("CODE_BLOCK_SELECTOR=pre")
    lines.append("```\n")

    lines.append("## 输入框候选（注意：页面退化时可能有多个编辑器）\n")
    lines.append("| tag | id | placeholder | contenteditable | aria-label | 可见 | class |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for el in inputs[:15]:
        lines.append(
            f"| {el['tag']} | {el['id']} | {el['placeholder']} | {el['contenteditable']} "
            f"| {el['ariaLabel']} | {el['visible']} | `{el['cls'][:48]}` |"
        )
    lines.append("")

    lines.append("## 「生成中」候选控件（结束判定的主依据）\n")
    lines.append("| tag | aria-label | 文本 | 可见 | 视口下半部 | class |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for el in data["stopCandidates"][:15]:
        lines.append(
            f"| {el['tag']} | {el['ariaLabel']} | {el['text']} | {el['visible']} "
            f"| {el['bottomHalf']} | `{el['cls'][:48]}` |"
        )
    if not data["stopCandidates"]:
        lines.append("| — | 未发现 | — | — | — | 需要手动找「停止生成」控件 |")
    lines.append("")

    lines.append("## 回复节点候选（按置信度排序）\n")
    lines.append("| 选择器 | 数量 | 平均文本长度 |")
    lines.append("| --- | --- | --- |")
    for c in data["responseCandidates"][:15]:
        lines.append(f"| `{c['selector']}` | {c['count']} | {c['avgTextLen']} |")
    lines.append("")

    lines.append("## 代码块\n")
    lines.append(f"- `<pre>`: {code['preCount']}   `<code>`: {code['codeCount']}   "
                 f"语言标记: {code['languages'] or '无'}\n")

    if network is not None:
        verdict, candidates = network.route_verdict()
        lines.append("## 路线决策：站点自身的 XHR/fetch 接口（--network-log）\n")
        lines.append(f"- 结论：{verdict}")
        lines.append(f"- 采集到 {len(network.entries)} 个非静态请求（去查询串，仅记键名不记正文）\n")
        if candidates:
            lines.append("| 方法 | 主机 | 路径 | 次数 | 状态 | content-type | 请求体字符 | 响应 JSON 键 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for e in candidates:
                lines.append(
                    f"| {e['method']} | {e['host']} | `{e['path']}` | {e['count']} "
                    f"| {','.join(str(s) for s in e['status']) or '—'} | {e['contentType'] or '—'} "
                    f"| {e['requestBodyChars'] or '—'} | {', '.join(e['jsonKeys']) or '—'} |"
                )
            lines.append("")
            lines.append(
                "> 走路线 A/C 时：把「请求体字符」大的接口列为重点，用 DevTools 看**一次**真实"
                "请求/响应结构（脚本故意不采集正文），确认会话标识与流式字段的来源；"
                "再按 references/design.md §3「同族分歧」的取舍结论决定 A / C。"
            )
        else:
            lines.append("- 只观察到静态资源与页面导航 → 默认路线 B（Playwright DOM 自动化）。")
        lines.append("")
    else:
        lines.append("## 网络接口线索（未采集）\n")
        lines.append("- 没开 `--network-log`：无法判断「直接复用站点自身接口」（路线 A）是否可行。"
                     "想省掉 DOM 适配的成本，就带上这个开关重跑一次。")
        lines.append("")

    lines.append("## 流式形态与后续步骤\n")
    lines.append(f"- 采样结论：{_streaming_verdict(sample.get('first'), sample.get('second'))}")
    lines.append(f"- 会话 URL 正则建议：`SESSION_URL_RE={_guess_session_regex(meta['url'])}`")
    lines.append("\n**还需要手动补充（recon 拿不到）：**")
    lines.append("1. 上下文到顶时的提示**原文** → `CAP_NOTICE_PATTERNS`")
    lines.append("2. 是否需要登录 / 未登录时页面长什么样 → 决定 `unavailable` 的判定")
    lines.append("3. 客户端**临时会话 ID** 的形态（如 `local-chatgpt%3A<uuid>`）→ 绑定时拒绝")
    lines.append("4. 无头模式是否可用（挑战页会拦截）→ 不可用则必须走有头 / xvfb-run")
    lines.append("5. 工具调用回落的回复形态（围栏 / 无围栏 / 其它）→ 决定解析分支")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程


async def _run(args) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("未安装 playwright：python3 -m pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    user_data = Path(args.user_data).expanduser().resolve()
    user_data.mkdir(parents=True, exist_ok=True)
    network = NetworkLog() if args.network_log else None

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(user_data),
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        if network:
            network.attach(page)
        await page.goto(args.url, wait_until="domcontentloaded")

        if args.wait > 0:
            print(f"[recon] 等待 {args.wait}s（可手动登录 / 打开对话）...", flush=True)
            await asyncio.sleep(args.wait)
        elif not args.headless:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, input,
                "[recon] 请在浏览器里完成登录 / 打开一条对话，然后回来按 Enter 继续...",
            )

        data = await page.evaluate(_COLLECT_JS)
        sample = {}
        if args.sample_gap > 0 and args.sample_selector:
            first = await page.evaluate(_SAMPLE_JS, args.sample_selector)
            print(f"[recon] 采第一次：{first}；等 {args.sample_gap}s 再采一次"
                  f"（期间请在页面里发一条较长的消息）...", flush=True)
            await asyncio.sleep(args.sample_gap)
            second = await page.evaluate(_SAMPLE_JS, args.sample_selector)
            sample = {"first": first, "second": second}

        net_summary = network.summary() if network else []
        report = _render_report(data, sample, network)
        out = Path(args.out).expanduser() if args.out else None
        if out:
            out.write_text(report, encoding="utf-8")
        if args.json:
            Path(args.json).expanduser().write_text(
                json.dumps({"data": data, "sample": sample, "network": net_summary},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(report)
        if out:
            print(f"\n[recon] 报告已写入 {out}", file=sys.stderr)

        if not args.keep_open:
            await context.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="目标页面侦察：为 Web→OpenAI API 桥接找选择器")
    ap.add_argument("--url", required=True, help="目标页面 URL")
    ap.add_argument("--user-data", default="./user_data", help="Chromium 持久化 profile 目录")
    ap.add_argument("--wait", type=float, default=0,
                    help="打开后等待秒数（0 = 有头模式下等按 Enter）")
    ap.add_argument("--sample-gap", type=float, default=0,
                    help=">0 时隔这么久再采一次，判断流式形态")
    ap.add_argument("--sample-selector", default="",
                    help="用于流式采样的回复节点选择器（先跑一次拿报告里的建议值）")
    ap.add_argument("--network-log", action="store_true",
                    help="记录站点自身 XHR/fetch 接口（仅状态码/键名/长度，不含正文），评估路线 A 是否可行")
    ap.add_argument("--out", default="recon-report.md", help="Markdown 报告输出路径")
    ap.add_argument("--json", default="", help="可选：原始 JSON 输出路径")
    ap.add_argument("--headless", action="store_true",
                    help="无头模式（登录完成后可用；部分站点无头会被挑战页拦截）")
    ap.add_argument("--keep-open", action="store_true", help="结束后不关闭浏览器（便于继续观察）")
    args = ap.parse_args()

    if args.sample_gap > 0 and not args.sample_selector:
        print("[recon] --sample-gap 需要配合 --sample-selector（可先跑一次拿建议值）",
              file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
