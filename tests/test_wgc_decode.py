"""WGC 帧缓冲的**解码字节序**（不需要 GPU / 真实窗口 / WinRT 绑定）。

为什么单独钉：帧池是按 ``B8_G8_R8_A8_UINT_NORMALIZED`` 建的，缓冲里第一个字节是**蓝**。
v0.2.35 前这里按 ``Image.frombytes("RGBA", …)`` 读，于是**红蓝整体对调**——纯蓝 (0,0,255)
读成纯红 (255,0,0)、黄 (204,204,0) 读成青 (0,204,204)，而绿色不受影响，肉眼像"只是偏色"。
在真机上用四色块窗口对照 PrintWindow 才被测出来。

所以这里用**人造缓冲**把字节序钉死：这类错误一旦复发，单测必须能抓住，而不是又靠真机偶遇。
"""

import unittest

from cvision.capture.wgc import _is_bgra, decode_frame

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (204, 204, 0)


def bgra(pixels):
    """把一组 RGB 像素编成 WGC 那种 BGRA 缓冲（每像素 b,g,r,a）。"""
    out = bytearray()
    for r, g, b in pixels:
        out += bytes((b, g, r, 255))
    return bytes(out)


def row(img):
    """按行读出像素（不用 ``getdata``：Pillow 14 起它被标了弃用）。"""
    return [img.getpixel((x, y)) for y in range(img.height) for x in range(img.width)]


class TestDecodeFrame(unittest.TestCase):
    def test_bgra_buffer_decodes_to_true_colors(self):
        """回归点：BGRA 缓冲必须解出**原色**，尤其蓝色不能被换成红色。"""
        img = decode_frame(bgra([RED, GREEN, BLUE, YELLOW]), 4, 1)
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(row(img), [RED, GREEN, BLUE, YELLOW])

    def test_pure_blue_is_not_read_as_red(self):
        """最容易被一句话说清的症状：蓝 → 红，绿 → 绿。"""
        img = decode_frame(bgra([BLUE, GREEN]), 2, 1)
        self.assertEqual(img.getpixel((0, 0)), BLUE)
        self.assertEqual(img.getpixel((1, 0)), GREEN)
        self.assertNotEqual(img.getpixel((0, 0)), RED)

    def test_default_format_is_bgra(self):
        """拿不到格式信息时按 BGRA 算——这正是帧池请求的格式。"""
        self.assertTrue(_is_bgra(None))
        self.assertEqual(decode_frame(bgra([BLUE]), 1, 1).getpixel((0, 0)), BLUE)

    def test_binding_name_variants_are_recognised(self):
        """两套绑定的枚举字符串写法不同，都要认出来是 BGRA。"""
        for name in (
            "B8_G8_R8_A8_UINT_NORMALIZED",
            "DirectXPixelFormat.B8G8R8A8UIntNormalized",
            "B8G8R8A8_UNORM",
            87,  # 枚举整数值也当 BGRA（认不出名字时不冒险调序）
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_bgra(name))

    def test_explicit_rgba_format_is_not_swapped(self):
        """真的拿到 RGBA 缓冲时就**不能**再按 BGRA 拆，否则等于制造同一种错。"""
        self.assertFalse(_is_bgra("R8G8B8A8UIntNormalized"))
        self.assertFalse(_is_bgra("DirectXPixelFormat.R8_G8_B8_A8_UINT_NORMALIZED"))
        raw = b"".join(bytes((r, g, b, 255)) for r, g, b in (RED, GREEN, BLUE))
        img = decode_frame(raw, 3, 1, "R8G8B8A8UIntNormalized")
        self.assertEqual(row(img), [RED, GREEN, BLUE])

    def test_geometry_follows_width_and_height(self):
        """行宽按像素宽算：多行缓冲不能被读成一行（否则图像会被斜切/错行）。"""
        img = decode_frame(bgra([RED, GREEN, BLUE, YELLOW, RED, GREEN]), 3, 2)
        self.assertEqual(img.size, (3, 2))
        self.assertEqual(img.getpixel((0, 1)), YELLOW)
        self.assertEqual(img.getpixel((2, 1)), GREEN)


if __name__ == "__main__":
    unittest.main()
