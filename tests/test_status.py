"""status() 健康探针结构测试（平台无关：仅依赖 Pillow）。

这个探针的**唯一职责**是「依赖残缺时仍能给出结论」，所以本文件里有两条用子进程做的
端到端用例（导入路径 + 缺 Pillow 故障注入），而不只是结构断言——v0.2.33 修的正是
「探针自己先崩在 import 上」这个真实缺陷。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from cvision import status

#: 仓库根（`cvision/` 的父目录）：子进程要靠它才能 import 到本仓库的包。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_python(args, **kwargs):
    """spawn 一个 Python 子进程并返回 CompletedProcess（UTF-8 解码，超时 60s）。"""
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=REPO_ROOT,
        **kwargs,
    )


class TestStatus(unittest.TestCase):
    def test_shape(self):
        s = status.status()
        for key in [
            "platform",
            "python",
            "cvison_dir",
            "backend",
            "backend_known",
            "backend_implemented",
            "backend_import_error",
            "ocr_engine",
            "input_capabilities",
            "deps",
            "ok",
        ]:
            self.assertIn(key, s, f"missing {key}")
        self.assertIsInstance(s["deps"], dict)
        self.assertIsInstance(s["input_capabilities"], list)
        self.assertIsInstance(s["ok"], bool)
        # 「后端有实现但导不进来」必须能被单独看出来：它与「平台未实现」的处置完全不同。
        self.assertIsInstance(s["backend_import_error"], str, "该字段必须是字符串（空串 = 导入正常）")

    def test_backend_known(self):
        self.assertTrue(status.status()["backend_known"])

    def test_probe_covers_pyautogui_eager_import_deps(self):
        """探针必须覆盖 pyautogui 的**导入期**硬依赖（pyperclip 经 mouseinfo 顶层 import）。

        只探 pyautogui 会误报 ok：pyperclip 缺失时 pyautogui 根本 import 不进来。
        """
        deps = status.status()["deps"]
        self.assertIn("Pillow", deps)
        self.assertIn("pyautogui", deps)
        self.assertIn("pyperclip", deps)

    def test_cvison_dir_points_at_package_root(self):
        """该字段是「包根目录」而非 cwd：插件把子进程 cwd 改成临时目录后，cwd 不再代表 cvision 的位置。"""
        import os

        root = status.status()["cvison_dir"]
        self.assertTrue(os.path.isdir(root), root)
        self.assertTrue(os.path.isdir(os.path.join(root, "cvision")), root)

    def test_correctly_spelled_alias_matches(self):
        """`cvison_dir` 是历史拼写错误（保留兼容）；`cvision_dir` 是正确拼写，两者必须一致。"""
        s = status.status()
        self.assertIn("cvision_dir", s, "应提供拼写正确的别名")
        self.assertEqual(s["cvision_dir"], s["cvison_dir"])

    def test_probe_does_not_import_capture_stack(self):
        """探针不得把 PIL / win32gui / 捕获层拉进来——它必须在「一个依赖都没装」的环境里出结论。

        v0.2.33 前的真实缺陷：`status.py` 顶层 `from cvision import capturer`，链式 import
        `encoding`（`from PIL import Image`）与 `capture.windows`（`import win32gui`）。于是缺依赖时
        `python -m cvision.cli_capture --status` 在 import 期就崩，宿主两条通道全断，用户拿到的
        是一句裸 ImportError，而 README 承诺的是「缺什么 + 怎么装」。

        用**子进程**断言：本进程早已 import 过 status（甚至已用过 PIL），内存里的 sys.modules
        代表不了真实的导入路径。
        """
        code = (
            "import sys, cvision.status; "
            "print(sorted(m for m in ('PIL', 'win32gui', 'pyautogui', 'cvision.capturer') if m in sys.modules))"
        )
        out = _run_python(["-c", code])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]", f"探针拉进了不该有的模块：{out.stdout.strip()}")

    def test_cli_status_survives_missing_pillow(self):
        """缺 Pillow 时 `--status` 仍必须以退出码 0 输出合法 JSON（体检的全部意义所在）。

        做法：临时造一个 `PIL` 包、其 `__init__` 直接抛 ImportError，放到 PYTHONPATH 最前面，
        再真 spawn `python -m cvision.cli_capture --status`。这是端到端的：同时覆盖
        `cli_capture` 的延迟导入与 `status.py` 的「按平台推导后端名」。
        """
        with tempfile.TemporaryDirectory() as tmp:
            pil_dir = os.path.join(tmp, "PIL")
            os.makedirs(pil_dir)
            with open(os.path.join(pil_dir, "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write('raise ImportError("simulated: Pillow is not installed")\n')
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([tmp, REPO_ROOT])
            env["PYTHONUTF8"] = "1"
            out = _run_python(["-m", "cvision.cli_capture", "--status"], env=env)

        self.assertEqual(out.returncode, 0, f"缺 Pillow 时探针不该崩：{out.stderr}")
        info = json.loads(out.stdout)
        self.assertFalse(info["deps"]["Pillow"], "缺 Pillow 必须如实报告")
        # 「后端有实现」与「后端导得进来」是两件事：前者按平台判定，不该被依赖问题带偏。
        expected_implemented = sys.platform.startswith("win") or sys.platform == "darwin"
        self.assertEqual(info["backend_implemented"], expected_implemented)
        self.assertTrue(info["backend_import_error"], "要给出真实的导入失败原因（用户唯一能自救的线索）")
        self.assertFalse(info["ok"], "依赖不齐时 ok 必须是 false，不能谎报")
        self.assertIn("import_error", info["capture_backends"])


if __name__ == "__main__":
    unittest.main()
