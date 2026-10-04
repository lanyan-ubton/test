# -*- coding: utf-8 -*-
"""copy-paste 自 dup_a.py —— 这正是重复检测要抓的坏味道。"""


def calc_member_price(amount, level):
    """函数名不同, 函数体结构完全一致。"""
    base = 0.9
    if level == "vip":
        base = 0.8
    if amount > 1000:
        base -= 0.05
    result = amount * base
    return round(result, 2)
