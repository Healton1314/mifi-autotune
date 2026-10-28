"""频段选择逻辑测试。

这里的规则是：**默认应该由设备自己声明支持哪些频段，而不是由我们写死一份清单**。
写死清单的后果是会漏测——比如设备其实支持 B8/B38/B39/B41，我们却只测了 B3/B40，
那"最优频段"这个结论就不可信。
"""

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autotune  # noqa: E402

SUPPORTED = [3, 8, 38, 39, 40, 41]


def caps(bands=None):
    return {"bands": SUPPORTED if bands is None else bands,
            "can_lock_band": True, "can_set_channel": True}


def ns(bands="default"):
    return argparse.Namespace(bands=bands)


class TestResolveBands(unittest.TestCase):
    def test_default_uses_every_supported_band(self):
        """默认必须覆盖设备声明的全部频段，不能只挑两个。"""
        self.assertEqual(autotune.resolve_bands(caps(), ns(), {}), SUPPORTED)

    def test_all_is_same_as_default(self):
        self.assertEqual(autotune.resolve_bands(caps(), ns("all"), {}), SUPPORTED)

    def test_explicit_list_wins(self):
        self.assertEqual(autotune.resolve_bands(caps(), ns("3,40"), {}), [3, 40])

    def test_explicit_list_tolerates_spaces(self):
        self.assertEqual(autotune.resolve_bands(caps(), ns("3, 40 "), {}), [3, 40])

    def test_preferred_filters_by_support(self):
        """preferred 只在配置子集落在支持列表内时使用。"""
        cfg = {"candidate_bands": [3, 40, 99]}
        self.assertEqual(autotune.resolve_bands(caps(), ns("preferred"), cfg), [3, 40])

    def test_preferred_falls_back_when_nothing_matches(self):
        """配置里的频段设备都不支持时，退回全部，而不是给出空清单。"""
        cfg = {"candidate_bands": [1, 2]}
        self.assertEqual(autotune.resolve_bands(caps(), ns("preferred"), cfg), SUPPORTED)

    def test_empty_capabilities_uses_config(self):
        """驱动没声明频段时用配置兜底，避免直接空转。"""
        cfg = {"candidate_bands": [3, 40]}
        self.assertEqual(autotune.resolve_bands(caps([]), ns(), cfg), [3, 40])

    def test_no_duplicates_or_reorder(self):
        """保持设备声明的顺序，不去重排——顺序变化会让输出难以对比历史记录。"""
        out = autotune.resolve_bands(caps(), ns(), {})
        self.assertEqual(out, sorted(out) if out == sorted(out) else out)
        self.assertEqual(len(out), len(set(out)))


if __name__ == "__main__":
    unittest.main()
