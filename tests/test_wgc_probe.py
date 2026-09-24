"""WGC 可用性探测的缓存语义（Windows 专用，但**不需要真实窗口**）。

为什么要单独钉：探测一次要 ~280ms（初始化 WinRT/DirectX 栈），而抓一张图才 ~170ms，
CLI 每次调用又是新进程——所以「失败的结论要落盘、下次直接跳过 WGC」是 see 路径上的真实提速。
同时它必须**会失效**：装了 PyWinRT、换了绑定包或过了保鲜期之后，WGC 可能从不可用变成可用，
一直吃旧结论会让用户永远用不上它。
"""

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 与 test_capture_foreground.py 同样的约束：`capture/windows.py` 在**模块顶层**就 import
# win32gui/win32con/win32ui，所以这里必须 try/except 兜住导入，否则 macOS/Linux 的整套测试会在
# collection 阶段崩掉。
try:
    from cvision.capture import windows as win_backend

    _IMPORT_ERROR = None
except ImportError as exc:  # 非 Windows（缺 pywin32）
    win_backend = None
    _IMPORT_ERROR = exc


@unittest.skipIf(win_backend is None, f"仅 Windows 可跑：{_IMPORT_ERROR}")
class TestWgcProbeCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_file = win_backend._WGC_PROBE_FILE
        self._orig_memory = win_backend._WGC_PROBE_MEMORY
        win_backend._WGC_PROBE_FILE = Path(self.tmp) / "probe.json"
        win_backend._WGC_PROBE_MEMORY = None

    def tearDown(self):
        win_backend._WGC_PROBE_FILE = self._orig_file
        win_backend._WGC_PROBE_MEMORY = self._orig_memory
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, payload: dict) -> None:
        win_backend._WGC_PROBE_FILE.write_text(json.dumps(payload), encoding="utf-8")

    def test_failed_result_is_cached_and_readable(self):
        """失败结论能被读回（这正是省掉每次 280ms 探测的机制）。"""
        win_backend._write_probe_cache({"available": False, "reason": "boom", "binding": "winrt"})
        cached = win_backend._read_probe_cache()
        self.assertIsNotNone(cached)
        self.assertFalse(cached["available"])
        self.assertEqual(cached["reason"], "boom")
        self.assertTrue(cached["cached"])

    def test_available_result_is_never_cached(self):
        """可用时不写缓存：那时本来就该走 WGC，没什么要跳过的。"""
        win_backend._write_probe_cache({"available": True, "reason": "ok", "binding": "winrt"})
        self.assertIsNone(win_backend._read_probe_cache())

    def test_fingerprint_mismatch_invalidates_cache(self):
        """环境指纹不符（换了绑定包/解释器/系统）时必须失效，否则会一直吃旧结论。"""
        self._seed({"fingerprint": "别的环境", "ts": time.time(), "available": False, "reason": "x"})
        self.assertIsNone(win_backend._read_probe_cache())

    def test_expired_cache_invalidates(self):
        """过了保鲜期要重新探测——用户的驱动/环境可能已经修好了。"""
        fingerprint = win_backend._environment_fingerprint()
        old = time.time() - win_backend._WGC_PROBE_TTL_SECONDS - 60
        self._seed({"fingerprint": fingerprint, "ts": old, "available": False, "reason": "x"})
        self.assertIsNone(win_backend._read_probe_cache())

    def test_future_timestamp_is_rejected(self):
        """时钟回拨/文件被改到未来时也不能当成有效缓存。"""
        fingerprint = win_backend._environment_fingerprint()
        self._seed({"fingerprint": fingerprint, "ts": time.time() + 86400, "available": False})
        self.assertIsNone(win_backend._read_probe_cache())

    def test_corrupt_cache_file_is_ignored(self):
        win_backend._WGC_PROBE_FILE.write_text("{ 不是 json", encoding="utf-8")
        self.assertIsNone(win_backend._read_probe_cache())

    def test_fingerprint_mentions_both_bindings(self):
        """指纹必须同时覆盖两套绑定：装了 PyWinRT 就能让 WGC 从不可用变可用。"""
        fingerprint = win_backend._environment_fingerprint()
        self.assertIn(sys.version.split()[0], fingerprint)
        # 两套绑定各占一段（缺失时写 absent），因此分段数固定为 5
        self.assertEqual(len(fingerprint.split("|")), 5)


if __name__ == "__main__":
    unittest.main()
