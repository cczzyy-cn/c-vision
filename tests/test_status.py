"""status() 健康探针结构测试（平台无关：仅依赖 Pillow）。"""

import unittest

from cvision import status


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
            "ocr_engine",
            "input_capabilities",
            "deps",
            "ok",
        ]:
            self.assertIn(key, s, f"missing {key}")
        self.assertIsInstance(s["deps"], dict)
        self.assertIsInstance(s["input_capabilities"], list)
        self.assertIsInstance(s["ok"], bool)

    def test_backend_known(self):
        self.assertTrue(status.status()["backend_known"])

    def test_cvison_dir_points_at_package_root(self):
        """该字段是「包根目录」而非 cwd：插件把子进程 cwd 改成临时目录后，cwd 不再代表 cvision 的位置。"""
        import os

        root = status.status()["cvison_dir"]
        self.assertTrue(os.path.isdir(root), root)
        self.assertTrue(os.path.isdir(os.path.join(root, "cvision")), root)


if __name__ == "__main__":
    unittest.main()
