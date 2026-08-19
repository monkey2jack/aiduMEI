#!/usr/bin/env python3
"""宿主 Hermes 源码解析（`test_hermes_plugin._resolve_host`）的守卫。

★ 为什么单独一个文件，而不是塞进 test_hermes_plugin.py：
  那份文件的前提是「**整份**都随宿主缺席而跳过」——README 里「12 跳过」这个
  数字，正是靠「该文件 `--collect-only` 收到几条」推导出来的（见
  test_v19_4_1_audit_fixes._collected_counts）。往里掺几条**永不跳过**的用例，
  归因立刻失真：收集数变 16，实际仍跳 12，而守卫只会报「README 数字不对」，
  不会告诉你是它自己的前提被掀了。**别把守卫的地基当普通空地用。**

★ 为什么本文件的用例**刻意不带 skipIf**：
  它们守的恰恰是「纯净机 / 装了宿主的机器，行为是否都可控」。若它们也跟着
  宿主缺席而跳过，在纯净开发机上就永远空转 —— **守卫跟着被守对象一起消失**，
  是本轮反复踩到的同一个坑。所以这里全程用临时目录伪造宿主，
  一棵真源码树都不需要。

守的三态（显式永远压过隐式）：
  ① 未设 HERMES_SRC                     → 自动发现
  ② HERMES_SRC=none/no/off/0/false/空   → 强制无宿主，一条回退路径都不试
  ③ HERMES_SRC=<路径>                   → 只认这一条，无效即报错、绝不回落
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent


def _load_plugin_tests():
    """按绝对路径加载同目录的 test_hermes_plugin，不依赖 sys.path 的摆布。"""
    spec = importlib.util.spec_from_file_location(
        "_hermes_plugin_tests_under_guard", _TESTS_DIR / "test_hermes_plugin.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


HP = _load_plugin_tests()


def _make_host(root):
    """在临时目录里造一棵**看起来像宿主**的最小源码树。"""
    (Path(root) / "agent").mkdir(parents=True, exist_ok=True)
    (Path(root) / "agent" / "memory_provider.py").write_text("", encoding="utf-8")
    return str(root)


class TestHostResolution(unittest.TestCase):
    def test_unset_falls_back_to_auto_discovery(self):
        """① 未设变量 → 走自动发现（既有行为不变，生产的 0 skipped 不受影响）。"""
        with tempfile.TemporaryDirectory() as tmp:
            host = _make_host(tmp)
            with mock.patch.object(HP, "_AUTO_CANDIDATES", (host,)):
                self.assertEqual(HP._resolve_host({}), host)

    def test_explicit_disable_beats_auto_discovery(self):
        """② 显式禁用必须压过自动发现 —— 哪怕宿主**确实就在那儿**。

        这是「在装了宿主的机器上把 N 全绿复现成 (N-12)+12 跳过」唯一的抓手。
        这条一旦退化成「宿主在就用宿主」，README 的复现命令又变回半真。
        """
        with tempfile.TemporaryDirectory() as tmp:
            host = _make_host(tmp)
            with mock.patch.object(HP, "_AUTO_CANDIDATES", (host,)):
                # 正面锚点：先确认自动发现**确实找得到** —— 否则下面的
                # 「返回 None」可能只是因为压根没有宿主，断言会空转成永真。
                self.assertEqual(HP._resolve_host({}), host)
                for word in ("none", "NONE", " None ", "no", "off", "0", "false", ""):
                    with self.subTest(word=word):
                        self.assertIsNone(
                            HP._resolve_host({"HERMES_SRC": word}),
                            f"HERMES_SRC={word!r} 未能强制关掉宿主",
                        )

    def test_explicit_path_beats_auto_discovery(self):
        """③ 显式路径优先于自动发现的路径。"""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            want, other = _make_host(a), _make_host(b)
            with mock.patch.object(HP, "_AUTO_CANDIDATES", (other,)):
                self.assertEqual(HP._resolve_host({"HERMES_SRC": want}), want)

    def test_invalid_explicit_path_raises_instead_of_falling_back(self):
        """③ 显式路径无效 → **报错**，绝不静默回落到自动发现的那棵树。

        回落是标准的假绿灯：用户指了 A，实际测的是 B，结果还是绿的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            other = _make_host(tmp)
            with mock.patch.object(HP, "_AUTO_CANDIDATES", (other,)):
                bogus = str(Path(tmp) / "definitely" / "not" / "here")
                with self.assertRaises(RuntimeError) as ctx:
                    HP._resolve_host({"HERMES_SRC": bogus})
                # 报错必须点名那条坏路径，否则排障时等于没说
                self.assertIn(bogus, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
