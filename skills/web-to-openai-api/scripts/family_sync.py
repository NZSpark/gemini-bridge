#!/usr/bin/env python3
"""同族 skill 的**单一共享源**：漂移检查（只读）与受控同步（默认 dry-run）。

背景：同一套「Web 页面 → OpenAI API」方法论被多个桥项目各复制了一份 skill，很快就会各说各话
（同一件事 A 项目写「用秒」，B 项目写「用轮询次数」）。本工具把它变成一条可执行的约定：

- **唯一共享源**：`family.json` 所在目录（canonical，默认就是本 skill）。共享文件（`SKILL.md`、
  `references/*.md`、`scripts/*.py`、`tests/*.py`…）只在这里维护；
- **站点专属**：`references/case-study-*.md`、`assets/config.template.env`、`family.json` 等按
  `site_specific` 通配排除，**永不被 sync 触碰**（站点差异本来就该留在那里）；
- **共享正文、站点专属 frontmatter**：`SKILL.md` 只比对 **frontmatter 之后**的内容，
  各站点可以自留 `name` / `description` / 触发词；
- **版本钉**：每个成员的 `SKILL.md` frontmatter 写 `family-source: <family>@<version>#<hash8>`；
  `hash8` 覆盖全部共享内容（SKILL.md 只算正文），所以**改了任何共享文件都必须更新钉**，
  否则 `check` 与门禁都会报 STALE-PIN。

用法::

    python3 scripts/family_sync.py check                     # 只读：本机所有成员的漂移情况
    python3 scripts/family_sync.py check --self-only          # 只校验 canonical 自己的钉是否新鲜
    python3 scripts/family_sync.py check --json family-drift-report.json
    python3 scripts/family_sync.py diff DeepseekBridge        # 某个成员的逐文件差异
    python3 scripts/family_sync.py sync DeepseekBridge        # 计划（dry-run，不写任何文件）
    python3 scripts/family_sync.py sync DeepseekBridge --apply # 真正写入（自动先备份）

退出码：`0` = 全部成员与共享源一致（离线成员按 N/A 跳过）；`1` = 有 DRIFT / MISSING / UNPINNED /
STALE-PIN；`2` = 用法或环境错误。

安全约定：`sync` 只写 `shared` 里的文件，写前把原文件备份到
`<成员 skill>/.skill-family-backup/<时间戳>/`，**从不删除**成员自己的文件。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
DEFAULT_FAMILY = HERE.parent / "family.json"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def body_of(text: str) -> str:
    """去掉 YAML frontmatter 之后的正文，并**归一化到恰好一个结尾换行**。

    归一化必须是幂等的：sync 会把 body_of(canonical) 写进成员文件，下次 check 再对那个文件
    算一次 body_of —— 两次结果必须相同，否则「同步完立刻变漂移」。
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.lstrip("\n").rstrip("\n") + "\n"


def signature(path: Path, rel: str, family: dict) -> str:
    """单个共享文件的内容指纹；`frontmatter_stripped` 里的文件只算正文。"""
    text = read_text(path)
    if rel in family.get("frontmatter_stripped", []):
        return sha256(body_of(text).encode("utf-8"))
    return sha256(text.encode("utf-8"))


def load_family(path: Path) -> dict:
    family = json.loads(read_text(path))
    local = path.parent / "family.local.json"
    if local.exists():
        override = json.loads(read_text(local))
        for key in ("shared", "site_specific", "notes"):
            if key in override:
                family[key] = override[key]
        if "members" in override:  # 按名字合并，允许只覆盖路径
            by_name = {m["name"]: m for m in override["members"]}
            family["members"] = [by_name.get(m["name"], m) for m in family["members"]]
            family["members"] += [m for n, m in by_name.items()
                                  if n not in {x["name"] for x in family["members"]}]
        family["_local"] = str(local)
    return family


def is_shared(rel: str, family: dict) -> bool:
    if any(fnmatch.fnmatchcase(rel, pat) for pat in family.get("site_specific", [])):
        return False
    return any(fnmatch.fnmatchcase(rel, pat) for pat in family.get("shared", []))


def list_shared(skill_dir: Path, family: dict) -> dict[str, Path]:
    """共享文件清单：相对路径（POSIX 风格）→ 绝对路径。"""
    out: dict[str, Path] = {}
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(skill_dir).parts)
        if {".git", "__pycache__", ".skill-family-backup"} & parts:
            continue
        rel = path.relative_to(skill_dir).as_posix()
        if is_shared(rel, family):
            out[rel] = path
    return out


def source_hash(skill_dir: Path, family: dict) -> tuple[str, int]:
    """共享内容的整体指纹 + 参与计算的文件数（SKILL.md 只算正文，避免自我引用）。"""
    entries = []
    for rel, path in list_shared(skill_dir, family).items():
        entries.append(f"{rel}:{signature(path, rel, family)}")
    digest = sha256("\n".join(sorted(entries)).encode("utf-8"))[:8]
    return digest, len(entries)


def read_pin(skill_dir: Path, family: dict) -> str | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    field = re.escape(family.get("pin_field", "family-source"))
    match = re.search(rf"^{field}:\s*(\S+)\s*$", read_text(skill_md), re.MULTILINE)
    return match.group(1) if match else None


def expected_pin(family: dict, canonical_dir: Path) -> str:
    digest, _ = source_hash(canonical_dir, family)
    return f"{family['family']}@{family['version']}#{digest}"


def require_canonical(canonical_dir: Path, family_path: Path, family: dict) -> bool:
    """共享源目录必须真实存在，否则任何「一致」结论都是假的。

    拷贝到别处（没有同族仓库）时最容易踩：`canonical_skill` 解析成一个不存在的路径，
    共享文件数为 0，指纹退化成「空串的哈希」，而工具仍打印「共享源 = …」并返回 0 ——
    调用方会把这种**没有检查过任何东西**的结果当成 PASS。所以这里直接判环境错误。
    """
    if canonical_dir.is_dir():
        return True
    print(f"找不到共享源（canonical）：{canonical_dir}", file=sys.stderr)
    print(f"  来自 {family_path} 的 canonical_skill = "
          f"{family.get('canonical_skill', '.')!r}", file=sys.stderr)
    print("这通常说明当前是一份**独立拷贝**（同族仓库不在这个相对位置上）。两种正确做法：",
          file=sys.stderr)
    print('  ① 指向真正的共享源：把 canonical_skill 改成共享源 skill 目录的路径；',
          file=sys.stderr)
    print('  ② 让本副本自成共享源：把 canonical_skill 改成 "."，members 只保留自己'
          '（然后 check 会拿本副本的共享文件算指纹）。', file=sys.stderr)
    print("绝不允许在找不到共享源时输出「一致」——那等于用空指纹假装检查过。", file=sys.stderr)
    return False


# ---------------------------------------------------------------- check


def _all_files(skill_dir: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(skill_dir).parts)
        if {".git", "__pycache__", ".skill-family-backup"} & parts:
            continue
        out.append(path.relative_to(skill_dir).as_posix())
    return out


def check_member(member: dict, family: dict, canonical_dir: Path) -> dict:
    skill_dir = (canonical_dir if member.get("role") == "canonical"
                 else (canonical_dir / member["skill"]).resolve())
    result: dict = {"name": member["name"], "role": member.get("role", "member"),
                    "skill": str(skill_dir), "drift": [], "missing": [], "extra": []}
    if not skill_dir.exists():
        result["status"] = "N/A"
        result["detail"] = "本机上找不到该成员目录（未参与判定）"
        return result

    canonical_files = list_shared(canonical_dir, family)
    member_files = list_shared(skill_dir, family)
    for rel, path in canonical_files.items():
        target = member_files.get(rel)
        if target is None:
            result["missing"].append(rel)
            continue
        if signature(path, rel, family) != signature(target, rel, family):
            result["drift"].append(rel)
    result["extra"] = sorted(set(member_files) - set(canonical_files))
    # 成员自己的文件（含站点专属）：只提示，不参与判定，sync 也永不动它们
    canonical_all = set(canonical_files) | {
        rel for rel in _all_files(canonical_dir)
        if any(fnmatch.fnmatchcase(rel, pat) for pat in family.get("site_specific", []))
    }
    result["member_local"] = sorted(set(_all_files(skill_dir)) - canonical_all)

    pin = read_pin(skill_dir, family)
    want = expected_pin(family, canonical_dir)
    result["pin"] = pin
    result["expected_pin"] = want
    if pin is None:
        result["unpinned"] = True
    elif pin != want:
        result["stale_pin"] = True

    if result["missing"] or result["drift"] or result.get("unpinned") or result.get("stale_pin"):
        result["status"] = "DRIFT"
    else:
        result["status"] = "OK"
    return result


def do_check(args) -> int:
    family_path = Path(args.family).resolve()
    if not family_path.exists():
        print(f"找不到 {family_path}", file=sys.stderr)
        return 2
    family = load_family(family_path)
    canonical_dir = (family_path.parent / family.get("canonical_skill", ".")).resolve()
    if not require_canonical(canonical_dir, family_path, family):
        return 2
    members = [m for m in family["members"]
               if not args.self_only or m.get("role") == "canonical"]

    digest, counted = source_hash(canonical_dir, family)
    if counted == 0:
        print(f"共享源里没有任何共享文件（{canonical_dir}）：shared = "
              f"{family.get('shared')!r}，无法据此判定一致性。", file=sys.stderr)
        return 2
    print(f"共享源：{canonical_dir}")
    print(f"共享内容 = {counted} 个文件（SKILL.md 只算正文）；共享指纹 "
          f"{family['family']}@{family['version']}#{digest}")
    print(f"钉（每个成员 SKILL.md 应写）：{expected_pin(family, canonical_dir)}\n")

    results = [check_member(m, family, canonical_dir) for m in members]
    for res in results:
        mark = {"OK": "OK  ", "DRIFT": "DRIFT", "N/A": "N/A "}[res["status"]]
        print(f"[{mark}] {res['name']}  {res['skill']}")
        if res["status"] == "N/A":
            print(f"        {res.get('detail', '')}")
            continue
        if res["missing"]:
            print(f"        缺共享文件 {len(res['missing'])}：{', '.join(res['missing'][:6])}")
        if res["drift"]:
            print(f"        与共享源不一致 {len(res['drift'])}：{', '.join(res['drift'][:6])}")
        if res.get("unpinned"):
            print("        未钉版本：SKILL.md 里没有 family-source 行")
        elif res.get("stale_pin"):
            print(f"        钉过期：写的是 {res['pin']}，应为 {res['expected_pin']}")
        local = list(dict.fromkeys(res["extra"] + res.get("member_local", [])))
        if local:
            names = ", ".join(local[:6]) + (f" 等 {len(local)} 个" if len(local) > 6 else "")
            print(f"        成员独有（site-specific，不会被 sync 触碰）：{names}")
        if res["status"] == "OK":
            print("        与共享源完全一致")

    bad = [r for r in results if r["status"] == "DRIFT"]
    unavailable = [r for r in results if r["status"] == "N/A"]
    print(f"\n=== 结论 ===\n一致 {sum(1 for r in results if r['status'] == 'OK')}"
          f" / 漂移 {len(bad)} / 不在本机 {len(unavailable)}")
    if bad:
        print("漂移：" + "、".join(r["name"] for r in bad))
        print("下一步：`family_sync.py sync <成员>` 看计划；确认后加 `--apply` 写入（会先备份）。")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"family": family["family"], "version": family["version"],
                        "canonical": str(canonical_dir), "source_hash": digest,
                        "expected_pin": expected_pin(family, canonical_dir),
                        "members": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")
    if unavailable and args.require_all:
        print("有成员不在本机，且指定了 --require-all", file=sys.stderr)
        return 1
    return 1 if bad else 0


def do_diff(args) -> int:
    family_path = Path(args.family).resolve()
    family = load_family(family_path)
    canonical_dir = (family_path.parent / family.get("canonical_skill", ".")).resolve()
    if not require_canonical(canonical_dir, family_path, family):
        return 2
    member = next((m for m in family["members"] if m["name"] == args.member), None)
    if member is None:
        print(f"family.json 里没有成员 {args.member}；可用："
              f"{', '.join(m['name'] for m in family['members'])}", file=sys.stderr)
        return 2
    skill_dir = (canonical_dir / member["skill"]).resolve()
    if not skill_dir.exists():
        print(f"{skill_dir} 不存在", file=sys.stderr)
        return 2
    res = check_member(member, family, canonical_dir)
    print(f"== {res['name']} ({skill_dir})")
    for rel in res["missing"]:
        print(f"  缺：{rel}")
    for rel in res["drift"]:
        print(f"  差异：{rel}")
        left = (canonical_dir / rel).read_text(encoding="utf-8").splitlines()
        right = (skill_dir / rel).read_text(encoding="utf-8").splitlines()
        for i, (a, b) in enumerate(zip(left, right), 1):
            if a != b:
                print(f"    共享源:{i}: {a[:100]}")
                print(f"    成员  :{i}: {b[:100]}")
        print(f"    （共享源 {len(left)} 行 / 成员 {len(right)} 行）")
    if res.get("stale_pin") or res.get("unpinned"):
        print(f"  钉：{res['pin']} ≠ 期望 {res['expected_pin']}")
    return 0 if res["status"] in ("OK", "N/A") else 1


def do_sync(args) -> int:
    family_path = Path(args.family).resolve()
    family = load_family(family_path)
    canonical_dir = (family_path.parent / family.get("canonical_skill", ".")).resolve()
    if not require_canonical(canonical_dir, family_path, family):
        return 2
    member = next((m for m in family["members"] if m["name"] == args.member), None)
    if member is None or member.get("role") == "canonical":
        print(f"成员名无效（不能同步 canonical 自己）：{args.member}", file=sys.stderr)
        return 2
    skill_dir = (canonical_dir / member["skill"]).resolve()
    if not skill_dir.exists():
        print(f"{skill_dir} 不存在（成员没 clone 在本机？）", file=sys.stderr)
        return 2

    # 先保证共享源自己是自洽的（钉新鲜），否则会把过期的钉复制出去
    canonical_pin = read_pin(canonical_dir, family)
    want = expected_pin(family, canonical_dir)
    if canonical_pin != want:
        print(f"共享源自己的钉不新鲜：{canonical_pin} ≠ {want}\n"
              "先更新共享源 SKILL.md 的 family-source 行再同步。", file=sys.stderr)
        return 2

    canonical_files = list_shared(canonical_dir, family)
    to_write = [
        rel for rel, path in canonical_files.items()
        if rel != "SKILL.md"  # SKILL.md 只同步正文（见下面的 _write_skill_body）
        and (
            not (skill_dir / rel).exists()
            or signature(path, rel, family) != signature(skill_dir / rel, rel, family)
        )
    ]
    print(f"== 同步 {member['name']} ← 共享源")
    print(f"   目标：{skill_dir}")
    print(f"   共享文件 {len(canonical_files)} 个，需要写入 {len(to_write)} 个"
          f"{'（dry-run，不写任何文件）' if not args.apply else ''}")
    for rel in to_write:
        action = "新建" if not (skill_dir / rel).exists() else "覆盖"
        print(f"   - {action} {rel}")
    member_skill = skill_dir / "SKILL.md"
    body_differs = (
        not member_skill.exists()
        or signature(canonical_dir / "SKILL.md", "SKILL.md", family)
        != signature(member_skill, "SKILL.md", family)
    )
    if body_differs:
        print(f"   - {'新建' if not member_skill.exists() else '覆盖'} SKILL.md 正文（保留本站 frontmatter）")
    also_pin = read_pin(skill_dir, family) != want
    if also_pin:
        print(f"   - 更新钉 {family.get('pin_field', 'family-source')} → {want}")
    if not to_write and not also_pin and not body_differs:
        print("   已一致，无需改动")
        return 0
    if not args.apply:
        print("\n确认无误后加 `--apply` 真正写入（写前会备份到 .skill-family-backup/）。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = skill_dir / ".skill-family-backup" / stamp
    for rel in to_write:
        target = skill_dir / rel
        if target.exists():
            keep = backup / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, keep)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical_files[rel], target)
    if body_differs or also_pin:
        skill_md = member_skill
        if skill_md.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_md, backup / "SKILL.md")
        existing = read_text(skill_md) if skill_md.exists() else "---\n---\n"
        frontmatter, _, _ = _split_frontmatter(existing)
        lines = frontmatter.splitlines() if frontmatter else []
        field = family.get("pin_field", "family-source")
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(f"{field}:"):
                lines[i] = f"{field}: {want}"
                replaced = True
        if not replaced:  # 没有就插在 version 之后（frontmatter 内）
            for i, line in enumerate(lines):
                if line.startswith("version:"):
                    lines.insert(i + 1, f"{field}: {want}")
                    break
            else:
                lines.append(f"{field}: {want}")
        # 必须重新补上两侧的 `---`：丢了开头那行，frontmatter 就会被当正文 → 立刻变漂移
        head = "---\n" + "\n".join(line for line in lines if line.strip()) + "\n---"
        body = body_of(read_text(canonical_dir / "SKILL.md"))
        skill_md.write_text(f"{head}\n{body}", encoding="utf-8")
    written = len(to_write) + (1 if body_differs or also_pin else 0)
    print(f"\n已写入 {written} 个文件；原文件备份在 {backup}")
    print("比对确认：`family_sync.py check`")
    return 0


def _split_frontmatter(text: str) -> tuple[str, str, str]:
    """返回（frontmatter 内容，正文，原样分隔符）；没有 frontmatter 时第一项为空。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end].strip("\n"), text[end + 4 :].lstrip("\n"), "---"
    return "", text, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="同族 skill 的共享源漂移检查与受控同步")
    ap.add_argument("command", choices=["check", "diff", "sync"])
    ap.add_argument("member", nargs="?", default="", help="diff / sync 的成员名")
    ap.add_argument("--family", default=str(DEFAULT_FAMILY))
    ap.add_argument("--json", default="", help="check 的结果写成 JSON")
    ap.add_argument("--self-only", action="store_true", help="check 只看 canonical 自己的钉")
    ap.add_argument("--require-all", action="store_true", help="成员不在本机也算失败")
    ap.add_argument("--apply", action="store_true", help="sync 真正写入（默认 dry-run）")
    args = ap.parse_args()

    if args.command in ("diff", "sync") and not args.member:
        print(f"{args.command} 需要成员名（见 family.json 的 members）", file=sys.stderr)
        return 2
    if args.command == "check":
        return do_check(args)
    if args.command == "diff":
        return do_diff(args)
    return do_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
