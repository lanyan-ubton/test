# -*- coding: utf-8 -*-
"""干净代码: 不应触发任何红灯。"""


def clamp(value, low, high):
    """把 value 限制在 [low, high] 区间内。"""
    if value < low:
        return low
    if value > high:
        return high
    return value


class Calculator:
    """简单计算器: 方法首参 self 不计入参数上限。"""

    def add(self, a, b):
        return a + b

    def is_even(self, n):
        return n % 2 == 0
