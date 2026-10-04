# -*- coding: utf-8 -*-
"""问题代码: 用于验证复杂度/函数长度/参数个数三条门禁。"""


def too_complex(x):
    """圈复杂度 8 > 6。"""
    score = 0
    if x > 0:
        score += 1
    if x > 1:
        score += 2
    if x > 2:
        score += 3
    if x > 3:
        score += 4
    if x > 4:
        score += 5
    if x > 5:
        score += 6
    if x > 6:
        score += 7
    return score


def too_many_params(a, b, c, d, e):
    """参数 5 个 > 4。"""
    return a + b + c + d + e


def too_long(n):
    """函数 51 行 > 50。"""
    total = 0
    total += 1
    total += 2
    total += 3
    total += 4
    total += 5
    total += 6
    total += 7
    total += 8
    total += 9
    total += 10
    total += 11
    total += 12
    total += 13
    total += 14
    total += 15
    total += 16
    total += 17
    total += 18
    total += 19
    total += 20
    total += 21
    total += 22
    total += 23
    total += 24
    total += 25
    total += 26
    total += 27
    total += 28
    total += 29
    total += 30
    total += 31
    total += 32
    total += 33
    total += 34
    total += 35
    total += 36
    total += 37
    total += 38
    total += 39
    total += 40
    total += 41
    total += 42
    total += 43
    total += 44
    total += 45
    total += 46
    total += 47
    total += 48
    total += 49
    return total + n
