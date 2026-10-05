"""落盘目录保留策略与启动/后台清理（tasks.md T7.2 / T9.6）。

背景：`_prune_output_dir` 此前只在 `save_extracted_files` 里被调用 —— 也就是
“只有又发生一次落盘”才会顺带清理一次，纯读取的长跑进程永远不会回收旧文件，
而 v4 任务卡却把「启动 + 后台周期清理」标成了已完成。
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config, server  # noqa: E402
from gemini_web.chat_io import prune_output_dir  # noqa: E402


def _make(dirpath: Path, name: str, age_days: float = 0.0) -> Path:
    path = dirpath / name
    path.write_text("x", encoding="utf-8")
    if age_days:
        ts = time.time() - age_days * 86400
        os.utime(path, (ts, ts))
    return path


class PruneOutputDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_unlimited_keeps_everything(self):
        for i in range(3):
            _make(self.tmp, f"a{i}.txt")
        with mock.patch.object(config, "OUTPUT_MAX_FILES", 0), \
                mock.patch.object(config, "OUTPUT_MAX_AGE_DAYS", 0):
            self.assertEqual(prune_output_dir(str(self.tmp)), 0)
        self.assertEqual(len(list(self.tmp.iterdir())), 3)

    def test_max_files_keeps_newest(self):
        names = []
        for i in range(5):
            path = _make(self.tmp, f"f{i}.txt")
            ts = time.time() - (5 - i) * 10  # f4 最新
            os.utime(path, (ts, ts))
            names.append(path.name)
        with mock.patch.object(config, "OUTPUT_MAX_FILES", 2), \
                mock.patch.object(config, "OUTPUT_MAX_AGE_DAYS", 0):
            removed = prune_output_dir(str(self.tmp))
        self.assertEqual(removed, 3)
        self.assertEqual(
            sorted(p.name for p in self.tmp.iterdir()), sorted(names[-2:])
        )

    def test_max_age_removes_only_old_files(self):
        _make(self.tmp, "old.txt", age_days=10)
        _make(self.tmp, "new.txt")
        with mock.patch.object(config, "OUTPUT_MAX_FILES", 0), \
                mock.patch.object(config, "OUTPUT_MAX_AGE_DAYS", 3):
            removed = prune_output_dir(str(self.tmp))
        self.assertEqual(removed, 1)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["new.txt"])

    def test_missing_directory_is_not_fatal(self):
        with mock.patch.object(config, "OUTPUT_MAX_FILES", 2):
            self.assertEqual(prune_output_dir(str(self.tmp / "does-not-exist")), 0)

    def test_legacy_private_alias_still_works(self):
        from gemini_web.chat_io import _prune_output_dir

        self.assertIs(_prune_output_dir, prune_output_dir)


class StartupPruneTests(unittest.TestCase):
    """lifespan 必须在启动时清理一次（而不是只在落盘时）。"""

    def test_lifespan_prunes_output_on_startup(self):
        from fastapi.testclient import TestClient

        tmp = Path(tempfile.mkdtemp())
        _make(tmp, "stale.txt", age_days=30)
        with mock.patch.object(config, "OUTPUT_DIR", str(tmp)), \
                mock.patch.object(config, "OUTPUT_MAX_AGE_DAYS", 1), \
                mock.patch.object(config, "OUTPUT_MAX_FILES", 0), \
                mock.patch.object(config, "OUTPUT_PRUNE_INTERVAL_S", 0), \
                mock.patch.object(server.driver, "init", new=mock.AsyncMock()), \
                mock.patch.object(server.driver, "close", new=mock.AsyncMock()):
            with TestClient(server.app):
                pass  # 进入 lifespan 即完成启动清理
        self.assertFalse(
            (tmp / "stale.txt").exists(), "启动时应当清理过期落盘文件"
        )

    def test_zero_interval_makes_background_loop_a_noop(self):
        with mock.patch.object(config, "OUTPUT_PRUNE_INTERVAL_S", 0):
            # 应立即返回；若挂住说明 0 没有关闭周期任务
            asyncio.run(asyncio.wait_for(server._output_prune_loop(), timeout=2))


if __name__ == "__main__":
    unittest.main()
