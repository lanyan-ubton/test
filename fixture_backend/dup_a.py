# -*- coding: utf-8 -*-
"""与 dup_b.py 中的函数结构完全相同, 应被重复函数体检测命中。"""


def compute_discount(price, user_level):
    """docstring 不参与结构指纹。"""
    base = 0.9
    if user_level == "vip":
        base = 0.8
    if price > 1000:
        base -= 0.05
    result = price * base
    return round(result, 2)
