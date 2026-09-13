"""剪贴板竞态回归测试（平台无关：用假 win32clipboard，不碰真剪贴板）。

回归的 bug：``_paste_clipboard`` 为了输入非 ASCII 文本会临时占用剪贴板，输完再把旧内容写回。
原实现**无条件**恢复，于是如果用户在我们占用期间复制了别的东西，那份**用户新内容会被旧备份覆盖**
——这是本插件里唯一会破坏用户数据的路径。

现在恢复前会比对：只有剪贴板仍是「我们写进去的那份」才恢复。这两个用例分别钉死
「用户改动过 → 绝不覆盖」与「用户没动过 → 正常恢复」。
"""

import sys
import types
import unittest

from cvision import input as input_module


class FakeClipboard:
    """假 win32clipboard：用内存里的一个字符串模拟剪贴板文本。"""

    CF_UNICODETEXT = 13

    def __init__(self, initial=None, fail_first_open=False):
        self.text = initial
        self.writes = []
        self.fail_open = False
        #: 只让**第一次** OpenClipboard 失败，模拟「读的时候被占用、随后释放」。
        self.fail_first_open = fail_first_open
        self.open_attempts = 0

    def OpenClipboard(self, *args):
        self.open_attempts += 1
        if self.fail_first_open and self.open_attempts == 1:
            raise OSError("剪贴板被别的进程占用")
        if self.fail_open:
            raise OSError("剪贴板被别的进程占用")

    def CloseClipboard(self):
        pass

    def IsClipboardFormatAvailable(self, fmt):
        return self.text is not None and fmt == self.CF_UNICODETEXT

    def GetClipboardData(self, fmt):
        return self.text

    def EmptyClipboard(self):
        self.writes.append(("empty",))

    def SetClipboardData(self, fmt, value):
        self.text = value
        self.writes.append(("set", value))


class FakeCon:
    CF_UNICODETEXT = 13


class FakePyAutoGUI:
    """假 pyautogui：在「按 Ctrl+V」那一刻模拟用户/系统对剪贴板的改动。"""

    def __init__(self, on_hotkey=None):
        self.on_hotkey = on_hotkey
        self.hotkeys = []

    def hotkey(self, *keys):
        self.hotkeys.append(keys)
        if self.on_hotkey is not None:
            self.on_hotkey()


class PasteTestCase(unittest.TestCase):
    def setUp(self):
        self.original_modules = {
            name: sys.modules.get(name) for name in ("win32clipboard", "win32con")
        }
        self.original_require = input_module._require_pyautogui

    def tearDown(self):
        for name, module in self.original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        input_module._require_pyautogui = self.original_require

    def _install(self, clipboard, pyautogui):
        fake_clip = types.ModuleType("win32clipboard")
        for attr in ("OpenClipboard", "CloseClipboard", "IsClipboardFormatAvailable",
                     "GetClipboardData", "EmptyClipboard", "SetClipboardData"):
            setattr(fake_clip, attr, getattr(clipboard, attr))
        fake_clip.CF_UNICODETEXT = clipboard.CF_UNICODETEXT
        sys.modules["win32clipboard"] = fake_clip

        fake_con = types.ModuleType("win32con")
        fake_con.CF_UNICODETEXT = FakeCon.CF_UNICODETEXT
        sys.modules["win32con"] = fake_con

        input_module._require_pyautogui = lambda: pyautogui


class TestPasteDoesNotClobberUserClipboard(PasteTestCase):
    def test_user_copy_during_paste_is_preserved(self):
        """核心回归：占用期间用户复制了新内容 → 绝不写回旧备份。"""
        clipboard = FakeClipboard(initial="agent 的旧备份")
        # 模拟：Ctrl+V 那一刻（或之后）用户复制了别的东西。
        pyautogui = FakePyAutoGUI(on_hotkey=lambda: setattr(clipboard, "text", "用户刚复制的内容"))
        self._install(clipboard, pyautogui)

        input_module._paste_clipboard("中文输入")

        self.assertEqual(clipboard.text, "用户刚复制的内容", "用户的新内容被旧备份覆盖了")
        self.assertNotIn(("set", "agent 的旧备份"), clipboard.writes)

    def test_restores_when_clipboard_untouched(self):
        """用户没动过 → 正常恢复旧内容（保持原有便利性）。"""
        clipboard = FakeClipboard(initial="agent 的旧备份")
        pyautogui = FakePyAutoGUI()  # 不模拟用户改动
        self._install(clipboard, pyautogui)

        input_module._paste_clipboard("中文输入")

        self.assertEqual(clipboard.text, "agent 的旧备份")
        self.assertEqual(pyautogui.hotkeys, [("ctrl", "v")])

    def test_no_restore_when_there_was_no_old_text(self):
        """原来剪贴板里没有文本 → 不该伪造一个空文本写回去。"""
        clipboard = FakeClipboard(initial=None)
        pyautogui = FakePyAutoGUI()
        self._install(clipboard, pyautogui)

        input_module._paste_clipboard("中文输入")

        self.assertEqual(clipboard.text, "中文输入")
        self.assertEqual([w for w in clipboard.writes if w[0] == "set"], [("set", "中文输入")])

    def test_occupied_clipboard_does_not_crash_the_read(self):
        """读剪贴板时被占用（OpenClipboard 抛错）不该让整次输入失败。"""
        clipboard = FakeClipboard(initial="旧备份", fail_first_open=True)
        pyautogui = FakePyAutoGUI()
        self._install(clipboard, pyautogui)

        input_module._paste_clipboard("中文输入")

        self.assertEqual(pyautogui.hotkeys, [("ctrl", "v")])
        self.assertEqual(clipboard.text, "中文输入", "读不到旧内容时不该有任何恢复动作")


if __name__ == "__main__":
    unittest.main()
