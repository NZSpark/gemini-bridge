# 事故档案 → 假 page/driver 回归用例模板（25 条）

[../SKILL.md](../SKILL.md) 的「事故档案」列了 25 条真实反模式（`P0-A…P0-I`、`A1…A16`）。
**每条反模式都必须有一条能被机器证明「有区分力」的回归用例**——把修复换回旧代码，用例必须变红。
本文件给出这 25 条用例的可粘贴版本，全部只用假件（**不联网、不启浏览器**），配合
[../scripts/regression_kit.py](../scripts/regression_kit.py) 使用。

为什么值得这样做：这些故障里有相当一部分**不会在单测里报错**（静默空操作、类型问题、
单例串味、阈值单位），只靠 code review 和「感觉没问题」是拦不住的；而一条能变红的用例，
才是「这个坑以后不会再踩」的唯一证据。

---

## 0. 三步用法

```bash
# 1) 把工具箱放到你的测试目录（或调 import 路径）
cp skills/web-to-openai-api/scripts/regression_kit.py tests/

# 2) 粘下面对应的用例块进 tests/test_regressions.py（块内已自带 import）
#    —— 每个块只依赖 regression_kit + 标准库

# 3) 跑两遍：正常必须全绿；开旧行为必须变红（区分力实验）
.venv/bin/python -m pytest tests/test_regressions.py -q
TEMPLATE_VARIANT=p0a .venv/bin/python -m pytest tests/test_regressions.py -q   # 期望：只 p0a 变红
```

`TEMPLATE_VARIANT` 取值：`fixed`（默认，修好之后的行为）/ `<bug_id>`（只把这条的旧行为换回来）/
`all`（全部换回旧行为）。**变异开关本身也是文档**：它精确写出「修复前长什么样」。

---

## 1. 索引：反模式 → 假件 → 断言 → 变异 id

| 反模式 | 假件形态 | 断言（可观察判据） | 变异 id | 用例名 |
| --- | --- | --- | --- | --- |
| P0-A 回显当回复 | 回复节点列表（含回显 / 空节点） | 拿不到新正文时返回 `None`，不返回旧内容 | `p0a` | `test_p0a_echo_is_never_a_reply` |
| P0-B 漏 `self`/`staticmethod` | `ReplyStore` 类 | 实例调用**与**类调用都可用 | `p0b` | `test_p0b_persist_callable_on_instance_and_class` |
| P0-C 建页/拿锁间被回收 | 容量受限页面池 + in-flight 计数 | 有请求在飞的桶不得被回收 | `p0c` | `test_p0c_reap_skips_bucket_with_inflight_request` |
| P0-D 死页面留在池里 | 页面注册表 | 死页面必重建，且回到同一条会话 | `p0d` | `test_p0d_dead_page_is_rebuilt_on_same_session` |
| P0-E 状态 url 当真相 | 会被重定向的页面 | `goto` 后按**真实落点**重算 `has_history` | `p0e` | `test_p0e_landing_url_decides_has_history` |
| P0-F 全页扫到顶提示 | 页面外壳 + 会话正文 | 正文里引用过提示语不误报到顶 | `p0f` | `test_p0f_cap_notice_ignores_quoted_transcript` |
| P0-G 装饰类名 `stopAtEnd-*` | 停止控件命中表 | 只有可见且真的是停止控件才算「生成中」 | `p0g` | `test_p0g_decorative_stop_class_is_not_generating` |
| P0-H 纯文本调用行被渲染改写 | 渲染后的回复文本 | 围栏载体能解析；畸形旧格式不得猜 | `p0h` | `test_p0h_fenced_tool_call_survives_rendering` |
| P0-I 否定式措辞被当能力事实 | 系统提示词文本 | 禁用词表不出现 + 出现链路句式 | `p0i` | `test_p0i_system_prompt_never_negates_capabilities` |
| A1 播种标记只信磁盘 | 状态 + 新旧页面 | 页面对不上就必须重新完整播种 | `a01` | `test_a01_seed_flag_is_bound_to_live_page` |
| A2 提交后无条件重发 | 会话（新建计数） | 无法确认时只读恢复，`created == 0` | `a02` | `test_a02_ambiguous_submit_never_resends` |
| A3 不可恢复错误进重试 | 错误码 | 输入区缺失 / 未登录 / 额度用尽不重试 | `a03` | `test_a03_unrecoverable_error_is_not_retried` |
| A4 挑战页误报 | 正文 + 输入区可用性 | 命中 + 输入区**不可用**才成立 | `a04` | `test_a04_challenge_needs_dead_composer` |
| A5 凭 DOM 命中报成功 | 导航状态码 + DOM 命中数 | 403 的壳里有输入框也不算可用 | `a05` | `test_a05_probe_requires_navigation_ok` |
| A6 额度弹窗被报成解析失败 | 可见文案 | 归因 `usage_limit_reached` | `a06` | `test_a06_usage_limit_popup_is_attributed` |
| A7 死键（写了没人读） | 配置源码 + 读取源码 | 死键必须被列出来 | `a07` | `test_a07_dead_config_keys_are_found` |
| A8 字符串替换只剔一处 | 出现两次的子树文本 | 每一处都被剪掉 | `a08` | `test_a08_subtree_text_removed_everywhere` |
| A9 有损读回否决 `fill` | 读回有损的输入区 | 写入成功不被清空重写 | `a09` | `test_a09_lossy_readback_does_not_veto_fill` |
| A10 照抄不存在的字段 | 只有两个字段的 usage 对象 | 引用不存在的字段必须报错 | `a10` | `test_a10_unknown_usage_field_is_rejected` |
| A11 只看第一个 attached | 僵尸 + 正常 + 隐藏输入区 | 选中真正能写入且可见的那个 | `a11` | `test_a11_zombie_composer_is_skipped` |
| A12 前缀比较会话 URL | 只差末位的两条 URL | 主机 + 路径精确匹配 | `a12` | `test_a12_conversation_url_exact_match` |
| A13 指标进程单例 | 两个注册表实例 | 两实例不共享计数 | `a13` | `test_a13_registry_is_not_a_process_singleton` |
| A14 未验证含糊成通过 | 验收文档文本 | 既标通过又写未验证必须被指出 | `a14` | `test_a14_acceptance_marks_unverified_items` |
| A15 次数阈值 | 秒级静默窗口时钟 | 调快轮询不提前判结束 | `a15` | `test_a15_idle_window_is_seconds_not_ticks` |
| A16 取消时 await watcher | 取消后再 await 会抛错的 watcher | 取消不 await；页面归还、桶锁释放 | `a16` | `test_a16_cancel_does_not_await_watcher` |

---

## 2. 模板（逐条粘贴）

### P0-A 回显当成回复

判据：结束时取「最后一个**有正文**的节点」，且必须既不是本轮 prompt 的回显，也不是上一轮正文；
拿不到新正文就返回 `None`（宁可超时，也不返回旧内容）。

```python
def test_p0a_echo_is_never_a_reply() -> None:
    from regression_kit import pick_new_reply

    previous, echo = "上一轮回复：好的。", "现在几点？"
    # 回显 + 上一轮内容都在页面里 → 必须判定「还没有新正文」
    assert pick_new_reply([previous], [previous, echo], echo) is None
    # 空节点不参与判定（虚拟化列表里常见）
    assert pick_new_reply([previous], ["", "   "], echo) is None
    # 真正的新正文照样要能取到
    assert pick_new_reply([previous], [previous, "现在 15:04。"], echo) == "现在 15:04。"
```

**区分力**：`TEMPLATE_VARIANT=p0a` → 本用例必红（旧行为返回回显）。

### P0-B 落盘方法漏 `self` / `@staticmethod`

判据：实例调用与**类调用**都必须可用（测试全按类上调用会掩盖它）。

```python
def test_p0b_persist_callable_on_instance_and_class() -> None:
    from regression_kit import ReplyStore

    store = ReplyStore(root="output")
    assert store.persist("hello") == "output/reply.md"  # 实例调用
    assert ReplyStore.persist("hello") == "output/reply.md"  # 类/静态引用调用（真实事故）
```

**区分力**：`TEMPLATE_VARIANT=p0b` → 第二行抛 `TypeError`，用例必红。

### P0-C 建页与拿锁之间被回收

判据：回收看**「有请求在飞」的可重入计数**，不是「桶锁在谁手里」。

```python
def test_p0c_reap_skips_bucket_with_inflight_request() -> None:
    from regression_kit import FakePagePool

    pool = FakePagePool(max_pages=1)
    page_b1 = pool.acquire("b1")
    pool.acquire("b2")  # 容量压力 → 触发回收
    assert pool.inflight["b1"] == 1
    assert not page_b1.is_lost(), "有请求在飞的桶被回收 = 随机 502"
    pool.release("b1")
    pool.release("b2")
```

**区分力**：`TEMPLATE_VARIANT=p0c` → 页面被回收，断言变红。

### P0-D 死页面留在池里

判据：记录里有页面也要**判活**；死页面必须重建，且重建后回到同一条会话。

```python
def test_p0d_dead_page_is_rebuilt_on_same_session() -> None:
    from regression_kit import PageRegistry

    registry = PageRegistry()
    registry.session_url_for["b1"] = "https://chat.example.com/c/abc123"
    first, rebuilt_first = registry.get_or_rebuild("b1")
    assert rebuilt_first is True
    first.close()  # 标签被关 / 渲染进程崩溃
    second, rebuilt = registry.get_or_rebuild("b1")
    assert rebuilt is True, "死页面留在池里 = 该桶永久 502"
    assert second is not first and not second.is_lost()
    assert second.url.endswith("/c/abc123"), "重建要回到同一条会话，不能开新对话"
```

**区分力**：`TEMPLATE_VARIANT=p0d` → 返回那张已关闭的页面，断言变红。

### P0-E 用「状态里有 url」当「页面真在会话里」

判据：`goto` 之后反查真实落点，按落点重算 `has_history`。

```python
def test_p0e_landing_url_decides_has_history() -> None:
    from regression_kit import FakePage, SessionState, reconcile_after_goto

    state = SessionState(has_history=True, url="https://chat.example.com/c/abc123")
    page = FakePage()
    page.redirect_to = "https://chat.example.com/"  # 被重定向回首页
    page.goto(state.url)
    result = reconcile_after_goto(state, page)
    assert result["landing"] == "https://chat.example.com/"
    assert result["has_history"] is False, "状态里有 url ≠ 页面真在这条会话里"
```

**区分力**：`TEMPLATE_VARIANT=p0e` → `has_history` 仍为 `True`，断言变红。

### P0-F 全页文本扫「到顶提示」

判据：扫描前**剔除会话正文**；正文里引用过这句话不得让每个请求都误报到顶。

```python
def test_p0f_cap_notice_ignores_quoted_transcript() -> None:
    from regression_kit import cap_notice_hit

    transcript = "用户：请解释这句 —— You've reached the maximum length。"
    page_text = "侧边栏 | 新对话\n" + transcript
    assert cap_notice_hit(page_text, transcript) is False
    assert cap_notice_hit("You've reached the maximum length", "") is True
```

**区分力**：`TEMPLATE_VARIANT=p0f` → 第一行断言变红。

### P0-G 装饰类名让 `generating` 恒为真

判据：停止控件必须**可见**，且是独立 stop 词（`(^|[-_])stop([-_]|$)` 或精确文案）。

```python
def test_p0g_decorative_stop_class_is_not_generating() -> None:
    from regression_kit import is_generating

    decorative = [{"cls": "stopAtEnd-9f2a1c", "visible": True, "text": ""}]
    assert is_generating(decorative) is False, "侧边栏装饰类名不是停止控件"
    assert is_generating([{"cls": "btn", "visible": True, "text": "Stop generating"}]) is True
    assert is_generating([{"cls": "btn-stop-abc", "visible": False, "text": "Stop"}]) is False
```

**区分力**：`TEMPLATE_VARIANT=p0g` → 第一行断言变红。

### P0-H 纯文本调用行被 markdown 渲染改写

判据：围栏载体为主、兼容「标记行 + 围栏」退化形态与旧格式；**解析不出来时返回空**，不猜。

```python
def test_p0h_fenced_tool_call_survives_rendering() -> None:
    from regression_kit import parse_tool_calls

    fence = "`" * 3  # 文档里不出现裸反引号，方便整段复制
    fenced = f'{fence}tool_call\n{{"name": "run", "arguments": {{"cmd": "ls -l"}}}}\n{fence}\n'
    calls = parse_tool_calls(fenced)
    assert len(calls) == 1 and calls[0]["name"] == "run"

    degraded = f'TOOL_CALL\n{fence}\n{{"name": "run"}}\n{fence}\n'  # 围栏进不了 innerText 的站点
    assert [c["name"] for c in parse_tool_calls(degraded)] == ["run"]

    assert parse_tool_calls("TOOL_CALL {name: run") == []  # 畸形格式宁可不解析
```

**区分力**：`TEMPLATE_VARIANT=p0h` → 前两条断言变红（旧行为只认纯文本行）。

### P0-I 提示词否定式措辞被读成能力事实

判据：提示词只描述链路；禁用「你没有 / 已挂载 / you do not have」这类措辞。

```python
def test_p0i_system_prompt_never_negates_capabilities() -> None:
    from regression_kit import system_prompt

    prompt = system_prompt([{"name": "run_shell"}])
    for phrase in ("你没有", "无法访问", "已挂载", "you do not have", "you cannot"):
        assert phrase not in prompt, f"会被当成关于自身能力的事实题而拒答：{phrase}"
    assert "客户端会真实执行" in prompt  # 只描述链路
```

**区分力**：`TEMPLATE_VARIANT=p0i` → 禁用词命中，断言变红。

### A1 播种标记只信磁盘 `has_history`

判据：标记**同时绑内存里的页面对象**；页面对不上就重新完整播种。

```python
def test_a01_seed_flag_is_bound_to_live_page() -> None:
    from regression_kit import FakePage, SessionState, should_seed

    old, new = FakePage(), FakePage()
    state = SessionState(has_history=True, seeded_page=old)
    assert should_seed(state, new) is True, "播种的是另一张页面 → 必须重新完整播种"
    assert should_seed(state, old) is False
    assert should_seed(SessionState(has_history=False), new) is True
```

**区分力**：`TEMPLATE_VARIANT=a01` → 第一行断言变红。

### A2 提交后无条件重发

判据：无法确认提交时只做「读页面现有回复」式恢复；没有安全恢复路径就明确失败。

```python
def test_a02_ambiguous_submit_never_resends() -> None:
    from regression_kit import (
        FakeConversation,
        UnrecoverableSubmission,
        recover_after_ambiguous_submit,
    )

    conversation = FakeConversation()
    action = recover_after_ambiguous_submit(confirmed=False, existing_reply="上一轮的回复")
    if action == "resend":
        conversation.new()  # 真实代码在这里会再提交一次 → 重复对话
    assert action != "resend"
    assert conversation.created == 0
    assert recover_after_ambiguous_submit(confirmed=True, existing_reply=None) == "submitted"
    try:
        recover_after_ambiguous_submit(confirmed=False, existing_reply=None)
    except UnrecoverableSubmission:
        pass
    else:
        raise AssertionError("没有安全恢复路径时必须明确失败，而不是空等或重发")
```

**区分力**：`TEMPLATE_VARIANT=a02` → `action == "resend"` 且 `created == 1`，断言变红。

### A3 不可恢复错误进重试阶梯

判据：命中不可恢复集合即冒泡；只对无副作用的阶段重试。

```python
def test_a03_unrecoverable_error_is_not_retried() -> None:
    from regression_kit import is_retryable

    for code in ("composer_not_found", "unavailable", "usage_limit_reached",
                 "context_length_exceeded", "challenge_required"):
        assert is_retryable(code) is False, f"{code} 命中即冒泡，不进重试阶梯"
    assert is_retryable("upstream_error") is True
    assert is_retryable("timeout") is True
```

**区分力**：`TEMPLATE_VARIANT=a03` → 第一轮循环即断言变红。

### A4 挑战页误报

判据：三条件与门 —— 页面 chrome 命中 + 命中可见 + **输入区不可用**。

```python
def test_a04_challenge_needs_dead_composer() -> None:
    from regression_kit import is_challenged

    transcript = "用户：请解释 security verification 是什么意思"
    quoted_only = "Just a moment... | " + transcript
    assert is_challenged(quoted_only, transcript, composer_ok=True) is False
    chrome_hit = "Just a moment...\nVerifying you are human\n" + transcript
    assert is_challenged(chrome_hit, transcript, composer_ok=True) is False, "输入区还能用 → 不是挑战页"
    assert is_challenged(chrome_hit, transcript, composer_ok=False) is True
```

**区分力**：`TEMPLATE_VARIANT=a04` → 前两条断言变红（旧行为命中就 503）。

### A5 探测凭 DOM 命中报成功

判据：成功判据必须含 `navigation_ok`（`200 ≤ status < 400`）。

```python
def test_a05_probe_requires_navigation_ok() -> None:
    from regression_kit import probe_ok

    assert probe_ok(403, dom_hits=1) is False, "403 的挑战壳里仍可能命中输入框"
    assert probe_ok(200, dom_hits=1) is True
    assert probe_ok(302, dom_hits=2) is True
    assert probe_ok(200, dom_hits=0) is False
```

**区分力**：`TEMPLATE_VARIANT=a05` → 第一行断言变红。

### A6 额度用尽弹窗被报成「解析失败 / 停滞」

判据：归因 `usage_limit_reached`(429)，与「停滞 / 解析失败」可区分。

```python
def test_a06_usage_limit_popup_is_attributed() -> None:
    from regression_kit import classify_failure

    visible = ["You've reached your limit. Upgrade to continue."]
    assert classify_failure(visible, stalled=True) == "usage_limit_reached"
    assert classify_failure([], stalled=True) == "stalled"
    assert classify_failure([], stalled=False, parsed_zero=True) == "parse_error"
```

**区分力**：`TEMPLATE_VARIANT=a06` → 第一行断言变红（旧行为报 `stalled`）。

### A7 配置死键

判据：定义了却没人读的键必须被列出来（要么接线，要么删）。

```python
def test_a07_dead_config_keys_are_found() -> None:
    from regression_kit import find_dead_keys

    config_src = 'TIMEOUT = env_int("TIMEOUT", 240)\nDEAD = env_bool("DEAD_KEY", False)\n'
    read_src = "timeout = config.TIMEOUT\n"  # DEAD_KEY 没有任何读取点
    assert find_dead_keys(config_src, read_src) == ["DEAD_KEY"]
```

接进真实项目时：`config_src` 传配置包源码，`read_src` 传**排除配置包本身**的其余源码拼接。

**区分力**：`TEMPLATE_VARIANT=a07` → 返回空列表，断言变红。

### A8 字符串替换只剔掉一处

判据：在**克隆体上剪掉子树**；等价实现必须删掉**每一处**同名文本。

```python
def test_a08_subtree_text_removed_everywhere() -> None:
    from regression_kit import text_without_subtrees

    transcript = "上一轮回复"
    page_text = f"侧边栏 {transcript} 尾巴 {transcript} 结束"  # 同一段文本出现两次
    cleaned = text_without_subtrees(page_text, [transcript])
    assert transcript not in cleaned, "只剔掉第一处 → 剩下的那处会污染扫描结果"
```

**区分力**：`TEMPLATE_VARIANT=a08` → 仍残留一处，断言变红。

### A9 有损读回否决成功的 `fill`

判据：读回只能**发现失败**，不能**否决成功**；只有 `fill` 真的写不进才清空重写。

```python
def test_a09_lossy_readback_does_not_veto_fill() -> None:
    from regression_kit import FakeComposer, write_prompt

    composer = FakeComposer(lossy=True)  # 读回丢换行 / 收敛空白
    prompt = "第一行\n第二行\n    print(1)\n第四行"  # 多行 + 代码块缩进：真实 prompt 的典型形状
    info = write_prompt(composer, prompt)
    assert info["written"] == len(prompt)
    assert info["readback"] != prompt, "先确认这个测试里读回确实是有损的"
    assert composer.clears == 0, "有损读回不得触发清空重写"
    assert composer.text == prompt
```

**区分力**：`TEMPLATE_VARIANT=a09` → `clears == 1`，断言变红。

### A10 照抄引用了不存在的字段

判据：先核实字段 / API 真实存在；不存在就报错，不用 `0` 顶替。

```python
def test_a10_unknown_usage_field_is_rejected() -> None:
    from regression_kit import UnknownFieldError, describe_usage

    class Usage:
        prompt_tokens = 12
        completion_tokens = 3

    usage = Usage()
    assert describe_usage(usage, ("prompt_tokens", "completion_tokens")) == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
    }
    try:
        describe_usage(usage, ("prompt_tokens", "reasoning_tokens"))
    except UnknownFieldError as exc:
        assert "reasoning_tokens" in str(exc)
    else:
        raise AssertionError("引用了不存在的字段必须报错，而不是用 0 顶替")
```

**区分力**：`TEMPLATE_VARIANT=a10` → 没抛异常，断言变红。

### A11 只按「第一个 attached」取输入区

判据：候选全枚举 + 可见可编辑优先 + 逐个试填校验读回；僵尸节点必须跳过。

```python
def test_a11_zombie_composer_is_skipped() -> None:
    from regression_kit import FakeComposer, pick_composer

    zombie = FakeComposer(attached=True, visible=True, accepts_input=False)  # 点不动
    live = FakeComposer(attached=True, visible=True)
    hidden = FakeComposer(attached=False, visible=False)
    chosen = pick_composer([zombie, live, hidden])
    assert chosen is live
    assert zombie.text == "" and live.text == ""  # 试填后不留残字
```

**区分力**：`TEMPLATE_VARIANT=a11` → 选中 zombie，断言变红。

### A12 用前缀比较判断会话是否同一条

判据：解析 URL 后 **主机 + 路径精确匹配**。

```python
def test_a12_conversation_url_exact_match() -> None:
    from regression_kit import same_conversation

    longer = "https://chat.example.com/c/abc1234"
    shorter = "https://chat.example.com/c/abc123"
    assert same_conversation(longer, shorter) is False, "只差末位不是同一条会话"
    assert same_conversation(shorter, "https://chat.example.com/c/abc123/") is True
    assert same_conversation(shorter, "https://other.example.com/c/abc123") is False
```

**区分力**：`TEMPLATE_VARIANT=a12` → 前缀比较判成同一条，第一行断言变红。

### A13 测试共用指标单例 / 状态注册表

判据：注册表逐测试注入，不用进程单例（否则计数串味）。

```python
def test_a13_registry_is_not_a_process_singleton() -> None:
    from regression_kit import make_registry

    first = make_registry()
    first.incr("requests", 3)
    second = make_registry()
    assert second.get("requests") == 0, "两处实例必须互不影响"
    assert first.get("requests") == 3
```

**区分力**：`TEMPLATE_VARIANT=a13` → 第二个实例读到 `3`，断言变红。

### A14 未验证的项含糊成「应该没问题」

判据：未验证项必须原样留成限制；**同一行不得既标通过又写未验证**。

```python
def test_a14_acceptance_marks_unverified_items() -> None:
    from regression_kit import check_acceptance_markers

    leaky = "| A4 | smoke | ✅ | 未验证 |"  # 既标通过又写未验证
    assert check_acceptance_markers(leaky), "含糊成通过必须被抓出来"
    good = (
        "# 验收记录\n"
        "| A4 | smoke | ✅ | 42 chunks + [DONE] |\n"
        "\n## 未验证\n- 无头模式：缺环境\n"
    )
    assert check_acceptance_markers(good) == []
    assert check_acceptance_markers("- [ ] A10 未验证：缺账号") == []
```

**区分力**：`TEMPLATE_VARIANT=a14` → 永远返回空列表，第一行断言变红。

### A15 用「轮询次数」当结束/停滞阈值

判据：阈值一律用**秒**；调快轮询不得等比削弱停滞保护。

```python
def test_a15_idle_window_is_seconds_not_ticks() -> None:
    from regression_kit import FakeClock, IdleClock

    clock = FakeClock()
    idle = IdleClock(clock, idle_s=3.0)
    for _ in range(15):  # 快速轮询：0.2s 一次，共 3.0s 的墙钟时间里跑了 15 次
        clock.advance(0.2)
        idle.tick("一样的正文")
    assert idle.ticks == 15
    assert idle.stalled() is False, "阈值单位是秒：轮询变快不能提前判结束"
    clock.advance(3.0)
    assert idle.stalled() is True
```

**区分力**：`TEMPLATE_VARIANT=a15` → 第一个 `stalled()` 断言变红。

### A16 取消时 `await` 被取消的 watcher

判据：只 `cancel()`，**不 await**；断开即取消 + 归还页面 + 释放桶锁。

```python
def test_a16_cancel_does_not_await_watcher() -> None:
    import asyncio

    from regression_kit import FakeLock, FakeMetrics, FakePage, FakeWatcher, cancel_and_release

    async def scenario() -> dict:
        watcher, page, lock, metrics = FakeWatcher(), FakePage(), FakeLock(), FakeMetrics()
        lock.acquire()
        result = await cancel_and_release(watcher, page, lock, metrics)
        assert watcher.awaited_after_cancel == 0, "取消后不得再 await 它"
        assert metrics.get("cancelled") == 1
        return result

    assert asyncio.run(scenario()) == {
        "cancelled": True,
        "page_closed": True,
        "lock_released": True,
    }
```

**区分力**：`TEMPLATE_VARIANT=a16` → `CancelledError` 抛出，用例必红（真实事故里表现为路由被拖死 >8 分钟）。

---

## 3. 接入你自己的实现（ADAPTER）

模板里的 `regression_kit.*` 是**参考判据**；真正有价值的是把它换成你项目里的符号。
替换点一共 10 处，其余 15 条模板本身就是完整逻辑（判据在测试里，没有可替换的实现）：

| kit 符号 | 换成你项目里的 | 怎么找 |
| --- | --- | --- |
| `pick_new_reply` | 「新正文」判定 | 结束判定模块里的 `_extract_new_reply` / `is_new_content` |
| `ReplyStore.persist` | 落盘方法 | 回复落盘 / `save_reply` |
| `FakePagePool.reap` | 页面池回收 | `page_pool.py` 的 `reap` / 容量淘汰 |
| `PageRegistry.get_or_rebuild` | 页面重建 | `driver` 里的 `ensure_page` / 重建分支 |
| `reconcile_after_goto` | 落点校验 | `goto` 之后的 `has_history` 重算 |
| `cap_notice_hit` / `is_challenged` | 文案扫描 | `CAP_NOTICE_PATTERNS` / 挑战检测 |
| `is_generating` | 「生成中」信号 | 停止控件选择器与可见性判定 |
| `IdleClock` | 停滞判定 | `end_detection` 里的静默窗口 |
| `is_retryable` | 重试阶梯 | `_UNRECOVERABLE_*` 集合 |
| `cancel_and_release` | 取消路径 | 路由里的 `is_disconnected` 分支 |

替换后**必须重跑区分力实验**：把修复关掉（`git stash` / 改回旧分支），新用例要变红；
变不红说明它只是在复述实现，不是在保护行为。

---

## 4. 机械验证：`check_templates.py`

```bash
python3 skills/web-to-openai-api/scripts/check_templates.py            # 默认 fixed + 全部变异
python3 skills/web-to-openai-api/scripts/check_templates.py --json out.json
```

它会做三件事（退出码：`0` 全过 / `1` 有问题 / `2` 用法或环境错误）：

1. 从本文件抽出所有含 `def test_` 的代码块 + 同目录的 `regression_kit.py`，在临时目录真跑一遍
   （`TEMPLATE_VARIANT=fixed`）——证明模板**能跑、不漏断言**；
2. 逐个设置 `TEMPLATE_VARIANT=<bug_id>` 再跑——**只有**对应用例变红，否则报「没有区分力」或「误伤」；
3. 对照 [../SKILL.md](../SKILL.md) 的「事故档案」表核对**覆盖**：每条反模式都要有模板，
   且 `BUG_TESTS` 里登记的用例名真的存在。

所以这份文件不会悄悄腐烂：门禁（`scripts/gate.py` 的 ③ 阶段）会把它当硬检查跑。

---

## 5. 纪律

- 模板里的断言**只认可观察判据**（返回值 / 计数 / 落地 URL / 抛出的异常），不认实现细节；
- 不许为了「让用例过」而放宽断言或用 skip 掩盖：跑不出来的用例就如实标成未验证；
- 每条真机新故障 → 先在「事故档案」加一行，再在这里加一条**能变红**的模板（顺序不能反）；
- 模板通过 ≠ 真机可用。真机对等（直连基线 vs bridge）与安全验收见
  [acceptance.md](acceptance.md) 的 §5 / §6，那里的结论必须来自真实站点。
