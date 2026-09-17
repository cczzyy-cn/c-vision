"""窗口跟踪诊断的纯逻辑测试（平台无关：不碰真窗口、不依赖 pywin32）。

诊断工具本身必须可信——它是我用来判定「谁动了窗口」的**测量仪器**。这轮会话里我已经
三次因为「测量工具本身没验证」得出过错误结论，所以这里把它的判定逻辑钉住。

覆盖：改动字段的判定、开关行为（默认关、设 1 才开）、关闭时不写文件、写入失败不影响调用方。
"""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from cvision import diagnose


def snap(rect=(0, 0, 100, 100), showCmd=1, zoomed=False, iconic=False, foreground=1):
    return {
        "rect": list(rect),
        "showCmd": showCmd,
        "zoomed": zoomed,
        "iconic": iconic,
        "foreground": foreground,
    }


def read_lines(path):
    """读一个文件的非空行（用 with 关好句柄，避免 ResourceWarning）。"""
    with io.open(path, encoding="utf-8") as handle:
        return [line for line in handle.read().strip().splitlines() if line]


class TestChangedFields(unittest.TestCase):
    def test_identical_snapshots_report_nothing(self):
        self.assertEqual(diagnose.changed_fields(snap(), snap()), [])

    def test_detects_rect_change(self):
        self.assertEqual(diagnose.changed_fields(snap(), snap(rect=(5, 5, 105, 105))), ["rect"])

    def test_detects_multiple_fields_in_stable_order(self):
        after = snap(rect=(1, 1, 2, 2), showCmd=3, zoomed=True, foreground=9)
        self.assertEqual(diagnose.changed_fields(snap(), after), ["rect", "showCmd", "zoomed", "foreground"])

    def test_foreground_only_change_is_reported(self):
        """只换前台也算「窗口被动了」——这正是要区分的情形之一。"""
        self.assertEqual(diagnose.changed_fields(snap(foreground=1), snap(foreground=2)), ["foreground"])

    def test_none_snapshots_are_safe(self):
        self.assertEqual(diagnose.changed_fields(None, snap()), [])
        self.assertEqual(diagnose.changed_fields(snap(), None), [])
        self.assertEqual(diagnose.changed_fields(None, None), [])


class TestEnabledSwitch(unittest.TestCase):
    def _with_env(self, value):
        patched = {} if value is None else {"CVISION_TRACE_WINDOWS": value}
        return mock.patch.dict(os.environ, patched, clear=False)

    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(diagnose.enabled())

    def test_explicit_values(self):
        for value, expected in [("1", True), ("true", True), ("0", False), ("false", False), ("", False)]:
            with mock.patch.dict(os.environ, {"CVISION_TRACE_WINDOWS": value}, clear=True):
                self.assertEqual(diagnose.enabled(), expected, f"value={value!r}")

    def test_trace_path_honours_override(self):
        with mock.patch.dict(os.environ, {"CVISION_TRACE_FILE": r"C:\x\y.jsonl"}, clear=True):
            self.assertEqual(diagnose.trace_path(), r"C:\x\y.jsonl")

    def test_trace_path_defaults_to_tempdir(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                os.path.dirname(diagnose.trace_path()),
                tempfile.gettempdir(),
            )


class TestWriteBehaviour(unittest.TestCase):
    def test_disabled_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            with mock.patch.dict(os.environ, {"CVISION_TRACE_FILE": path}, clear=True):
                diagnose.write_event("capture", 1, moved=False)
            self.assertFalse(os.path.exists(path), "未开启跟踪时不该创建任何文件")

    def test_enabled_appends_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            env = {"CVISION_TRACE_WINDOWS": "1", "CVISION_TRACE_FILE": path}
            with mock.patch.dict(os.environ, env, clear=True):
                diagnose.write_event("capture", 42, moved=True, changed=["rect"])
                diagnose.write_event("capture", 43, moved=False)
            lines = read_lines(path)
            self.assertEqual(len(lines), 2, "应当逐行追加")
            first = json.loads(lines[0])
            self.assertEqual(first["event"], "capture")
            self.assertEqual(first["hwnd"], 42)
            self.assertTrue(first["moved"])
            self.assertEqual(first["changed"], ["rect"])
            self.assertIn("pid", first)
            self.assertIn("t", first)

    def test_write_failure_never_raises(self):
        """诊断写不进去（路径非法）也绝不能影响抓图调用方。"""
        env = {"CVISION_TRACE_WINDOWS": "1", "CVISION_TRACE_FILE": r"Z:\nonexistent\dir\trace.jsonl"}
        with mock.patch.dict(os.environ, env, clear=True):
            diagnose.write_event("capture", 1)  # 不抛即通过

    def test_trace_step_disabled_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(diagnose.trace_step(1234, "wgc", snap()))

    def test_trace_step_records_stage_and_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trace.jsonl")
            env = {"CVISION_TRACE_WINDOWS": "1", "CVISION_TRACE_FILE": path}
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(diagnose, "snapshot", return_value=snap(rect=(9, 9, 10, 10))):
                    after = diagnose.trace_step(7, "prepare", snap())
            self.assertEqual(after["rect"], [9, 9, 10, 10], "应返回新快照供链式传递")
            record = json.loads(read_lines(path)[0])
            self.assertEqual(record["event"], "step")
            self.assertEqual(record["stage"], "prepare")
            self.assertEqual(record["changed"], ["rect"])
            self.assertEqual(record["note"], "几何被改动")


if __name__ == "__main__":
    unittest.main()
