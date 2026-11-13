# -*- coding: utf-8 -*-
"""题面生成自检：对照 `方案/技术规格_v5.md` 附录 A 的题面列逐字比较。

用法：
    python tools/check_statement.py

附录 A 里带题面字符串的用例是 I1–I7 与 D1（共 8 条）。
规格 §2.5 要求至少 4 条断言逐字相等，这里全部断言。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.executor import statement

CASES = [
    ("I1", "poly", {"coeffs": [1, 1], "lower": 0, "upper": 1},
     "计算定积分 ∫_{0}^{1} (x + 1) dx。"),
    ("I2", "poly", {"coeffs": [3, -2, 1], "lower": -1, "upper": 2},
     "计算定积分 ∫_{-1}^{2} (x² - 2x + 3) dx。"),
    ("I3", "poly", {"coeffs": [0, 0, 5], "lower": 1, "upper": 2},
     "计算定积分 ∫_{1}^{2} (5x²) dx。"),
    ("I4", "gamma", {"n": 1, "rate": 2, "c": 1},
     "计算定积分 ∫₀^∞ x e^{-2x} dx。"),
    ("I5", "gamma", {"n": 9, "rate": 8, "c": 5},
     "计算定积分 ∫₀^∞ 5x⁹ e^{-8x} dx。"),
    ("I6", "gaussian_odd", {"k": 0, "rate": 2, "c": 3},
     "计算定积分 ∫₀^∞ 3x e^{-2x²} dx。"),
    ("I7", "gaussian_odd", {"k": 5, "rate": 1, "c": -2},
     "计算定积分 ∫₀^∞ -2x¹¹ e^{-1x²} dx。"),
    ("D1", "det", {"matrix": [[2, 1], [1, 3]]},
     "计算下列 2 阶矩阵的行列式：[[2, 1], [1, 3]]。"),
]

fails = 0
for name, template, params, expect in CASES:
    got = statement(template, params)
    if got == expect:
        print("[ OK ] %-3s %r" % (name, got))
        continue
    fails += 1
    print("[FAIL] %-3s" % name)
    print("       实际 %r" % got)
    print("       期望 %r" % expect)
    for i in range(min(len(got), len(expect))):
        if got[i] != expect[i]:
            print("       首次不同在第 %d 个字符：实际 %r / 期望 %r"
                  % (i, got[i], expect[i]))
            break
    else:
        print("       前缀相同，长度不同：实际 %d / 期望 %d" % (len(got), len(expect)))

print("\nRESULT:", "PASS" if fails == 0 else "FAIL (%d)" % fails)
sys.exit(1 if fails else 0)
