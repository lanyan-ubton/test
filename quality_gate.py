#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
质量门禁工具 (quality-gate)
============================
思路来源: Bob 大叔 —— "Agent 可以忘掉提示词第 80 条规定, 却绕不过一个失败的测试。"
把提示词里的君子协定(圈复杂度/函数长度/覆盖率/变异得分)变成跑不掉的自动化检查。
CI 动不了没关系: 本地跑 + 让 AI 交付前必须跑, 效果一样。

用法:
  python quality_gate.py backend   # FastAPI 侧静态检查: ruff + 圈复杂度/函数长度/
                                   #   参数个数/文件行数 + 重复函数体检测
  python quality_gate.py frontend  # Next.js 侧: eslint 复杂度/体积硬规则
  python quality_gate.py all       # 全部静态检查
  python quality_gate.py backend --with-tests     # + pytest 覆盖率门禁 (>= --coverage%)
  python quality_gate.py backend --with-mutation  # + mutmut 变异得分门禁 (>= --mutation-score%)
  python quality_gate.py all --full               # 静态 + 测试 + 变异 全跑
  python quality_gate.py backend --max-complexity 8        # 放宽阈值
  python quality_gate.py frontend --frontend-coverage 80   # 前端也启用覆盖率门禁

退出码: 0 = 全绿, 1 = 有红灯。AI 工具(Aider/Claude Code)靠退出码判断能不能算"完成"。

目标环境: Linux 服务器 (CI/容器均可直接跑)。
依赖哲学: 静态检查零第三方依赖(纯 AST);
  ruff / pytest-cov / mutmut / stryker 有则用, 没有则跳过并提示怎么装。
  注意: mutmut 3 依赖 fork, 仅限 Linux/macOS 运行。
  前端变异测试依赖 stryker 配置 (stryker.config.json), 未配置则跳过。
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------- 阈值默认值 (Bob 给 Agent 的宽松档) ----------
DEFAULT_MAX_COMPLEXITY = 6      # 圈复杂度上限 (人类 4, Agent 放宽到 6)
DEFAULT_MAX_FN_LINES_PY = 50    # Python 函数行数上限
DEFAULT_MAX_PARAMS = 4          # 函数参数个数上限 (与前端 ESLint max-params 对齐)
DEFAULT_MAX_FILE_LINES = 500    # 单文件行数上限
DEFAULT_MIN_DUP_LINES = 6       # 重复函数体检测: 函数至少这么长才参与比较, 0=关闭
DEFAULT_COVERAGE = 85           # 算法服务覆盖率门禁 (%)
DEFAULT_MUTATION_SCORE = 60     # 变异得分门禁 (%) —— 变异测试比覆盖率难, 60 是务实起点

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".next",
    "dist", "build", ".mypy_cache", ".pytest_cache", "migrations",
    "mutants",  # mutmut 生成的变异体目录, 不是业务代码
}


# ============================================================
# Python 侧: 纯 AST 实现, 零依赖
# ============================================================

def _count_branches(node):
    """计算单个函数/方法的 McCabe 圈复杂度(近似 radon 口径), 不下钻嵌套函数。"""
    score = 1
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        # 嵌套函数/lambda 单独计算, 不计入外层
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While,
                              ast.ExceptHandler, ast.IfExp, ast.Assert)):
            score += 1
        elif isinstance(child, ast.BoolOp):  # a and b and c 算 2 个分支
            score += len(child.values) - 1
        elif isinstance(child, ast.comprehension):  # 推导式的 for 和 if 都算
            score += 1 + len(child.ifs)
        elif hasattr(ast, "match_case") and isinstance(child, ast.match_case):
            score += 1
        stack.extend(ast.iter_child_nodes(child))
    return score


def _count_args(node):
    """统计函数参数个数: 各类参数各算 1 个; 方法首参 self/cls 不算。"""
    a = node.args
    n = (len(getattr(a, "posonlyargs", [])) + len(a.args) + len(a.kwonlyargs)
         + (1 if a.vararg else 0) + (1 if a.kwarg else 0))
    if a.args and a.args[0].arg in ("self", "cls"):
        n -= 1
    return n


def check_python_file(path, max_complexity, max_fn_lines, max_params, max_file_lines):
    """返回该文件的违规列表 [(行号, 函数名, 问题描述)]。"""
    violations = []
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError) as e:
        return [(0, "<文件>", f"解析失败: {e}")]

    n_lines = len(source.splitlines())
    if n_lines > max_file_lines:
        violations.append((0, "<文件>", f"文件 {n_lines} 行 > {max_file_lines} 行"))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cc = _count_branches(node)
        lines = (node.end_lineno or node.lineno) - node.lineno + 1
        if cc > max_complexity:
            violations.append((node.lineno, node.name,
                               f"圈复杂度 {cc} > {max_complexity}"))
        if lines > max_fn_lines:
            violations.append((node.lineno, node.name,
                               f"函数 {lines} 行 > {max_fn_lines} 行"))
        n_args = _count_args(node)
        if n_args > max_params:
            violations.append((node.lineno, node.name,
                               f"参数 {n_args} 个 > {max_params} 个"))
    return violations


def iter_py_files(root, excludes):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in IGNORE_DIRS and d not in excludes]
        for f in filenames:
            if f.endswith(".py"):
                yield Path(dirpath) / f


# ---------- 重复函数体检测 (结构级, 零误报口径) ----------

def _collect_kept_names(tree):
    """收集不参与归一化的名字: 调用名/属性名/关键字参数名/import 名。
    这些决定语义 —— round(x) 和 floor(x) 不是重复。"""
    kept = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                kept.add(f.id)
            elif isinstance(f, ast.Attribute):
                kept.add(f.attr)
        elif isinstance(n, ast.Attribute):
            kept.add(n.attr)
        elif isinstance(n, ast.keyword):
            if n.arg:
                kept.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                kept.add(a.asname or a.name.split(".")[0])
    return kept


class _VarNormalizer(ast.NodeTransformer):
    """把局部变量名按首次出现顺序替换为 v0/v1/...,
    使"改了变量名的 copy-paste"也能被结构指纹命中。"""

    def __init__(self, kept):
        self.kept = kept
        self.seen = {}

    def _token(self, name):
        if name not in self.seen:
            self.seen[name] = "v%d" % len(self.seen)
        return self.seen[name]

    def visit_Name(self, node):
        if node.id not in self.kept:
            node.id = self._token(node.id)
        return node


def _fn_body_hash(node):
    """函数体结构指纹: 剔除 docstring -> 局部变量名归一化 -> AST dump 的 sha256。
    忽略函数名/行号/局部变量命名 —— 这正是 copy-paste 的典型伪装;
    而常量/调用目标/属性名参与指纹, 保证"结构相似"不等于"结构相同"。"""
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # 剔除 docstring
    if not body:
        return None
    mod = ast.Module(body=body, type_ignores=[])
    _VarNormalizer(_collect_kept_names(mod)).visit(mod)
    dump = ast.dump(mod, annotate_fields=False, include_attributes=False)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _iter_top_functions(tree):
    """产出模块级函数与类方法, 不深入函数体内的嵌套函数(避免重复计数)。"""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child
            elif isinstance(child, ast.ClassDef):
                stack.append(child)


def find_duplicate_functions(files, min_lines):
    """返回重复函数组 [[(文件, 行号, 函数名), ...], ...]。
    判定: 函数体 AST 结构完全一致 且 函数长度 >= min_lines。"""
    groups = {}
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue  # 解析失败的文件在 check_python_file 里已单独红灯
        for node in _iter_top_functions(tree):
            n = (node.end_lineno or node.lineno) - node.lineno + 1
            if n < min_lines:
                continue
            h = _fn_body_hash(node)
            if h:
                groups.setdefault(h, []).append((f, node.lineno, node.name))
    return [g for g in groups.values() if len(g) > 1]


# ============================================================
# 后端: ruff / 静态指标 / pytest 覆盖率 / mutmut 变异测试
# ============================================================

def run_ruff(target):
    if not shutil.which("ruff"):
        print("  [跳过] 未找到 ruff, pip install ruff 后可启用 lint 检查")
        return True
    r = subprocess.run(["ruff", "check", str(target)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout)
        return False
    print("  [通过] ruff check")
    return True


def _pytest_available():
    try:
        import pytest  # noqa: F401
        import pytest_cov  # noqa: F401
        return True
    except ImportError:
        return False


def run_pytest_coverage(target, min_cov):
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--cov=.", "-q",
         f"--cov-fail-under={min_cov}", "--cov-report=term-missing"],
        cwd=target)
    return r.returncode == 0


def _mutation_score_from_stats(stats):
    """官方口径(mutmut badge 同款): (killed + timeout) / (total - skipped) * 100。
    timeout 算杀死 —— 变异体导致测试挂起同样说明断言抓住了异常; 无可评分变异体返回 None。"""
    killed = int(stats.get("killed", 0))
    timeout_n = int(stats.get("timeout", 0))
    total = int(stats.get("total", 0))
    skipped = int(stats.get("skipped", 0))
    tested = total - skipped
    if tested <= 0:
        return None
    return (killed + timeout_n) / tested * 100.0


def run_mutation_backend(target, min_score):
    """mutmut 变异得分门禁 (需 Linux/macOS: mutmut 3 依赖 fork)。
    得分公式采用官方口径: (killed + timeout) / (total - skipped) * 100。
    timeout 算"杀死"是因为变异体导致测试挂起同样说明测试抓住了异常。"""
    if not shutil.which("mutmut"):
        print("  [跳过] 未找到 mutmut, pip install mutmut 后可启用变异测试门禁")
        return True

    print("  [运行] mutmut 变异测试 (每个变异体跑一次相关测试, 可能很慢; 进度透传)")
    r = subprocess.run(["mutmut", "run"], cwd=target)
    if r.returncode != 0:
        print("  [红灯] mutmut run 失败 (先确认 pytest 能全绿, 再跑变异)")
        return False

    # 机器可读统计: mutmut export-cicd-stats -> mutants/mutmut-cicd-stats.json
    subprocess.run(["mutmut", "export-cicd-stats"],
                   cwd=target, capture_output=True)
    stats_path = target / "mutants" / "mutmut-cicd-stats.json"
    if not stats_path.exists():
        print("  [红灯] 未生成 mutants/mutmut-cicd-stats.json (无可用变异数据)")
        return False
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"  [红灯] 解析 mutmut 统计失败: {e}")
        return False

    killed = int(stats.get("killed", 0))
    survived = int(stats.get("survived", 0))
    suspicious = int(stats.get("suspicious", 0))
    total = int(stats.get("total", 0))
    score = _mutation_score_from_stats(stats)
    if score is None:
        print("  [警告] 没有可评分的变异体 (total-skipped=0), 跳过得分判定")
        return True

    print(f"  [指标] 变异得分 {score:.1f}% — killed={killed} timeout={int(stats.get('timeout', 0))} "
          f"survived={survived} suspicious={suspicious} total={total}")
    if stats.get("check_was_interrupted_by_user"):
        print("  [红灯] 上次 mutmut 运行被中断, 结果不完整, 请重跑")
        return False
    if score < min_score:
        print(f"  [红灯] 变异得分 {score:.1f}% < {min_score}% — "
              f"存活的变异体意味着测试断言不够强")
        return False
    print(f"  [通过] 变异得分 >= {min_score}%")
    return True


def check_backend(args):
    print(f"\n=== 后端门禁 ({args.backend_dir}) ===")
    ok = True
    target = Path(args.backend_dir)
    if not target.is_dir():
        print(f"  [红灯] 目录不存在: {target} —— 路径错了门禁就形同虚设, 宁可误报也不静默放行")
        return False

    # 1. ruff
    if not run_ruff(target):
        ok = False

    # 2. 圈复杂度 + 函数长度 + 参数个数 + 文件行数
    n_files, n_violations = 0, 0
    files = list(iter_py_files(target, set(args.exclude)))
    for f in files:
        n_files += 1
        for lineno, name, msg in check_python_file(
                f, args.max_complexity, args.max_fn_lines_py,
                args.max_params, args.max_file_lines):
            n_violations += 1
            print(f"  [红灯] {f}:{lineno} {name}() — {msg}")
    print(f"  [扫描] {n_files} 个 Python 文件, {n_violations} 处违规 "
          f"(复杂度上限 {args.max_complexity}, 函数上限 {args.max_fn_lines_py} 行, "
          f"参数上限 {args.max_params} 个, 文件上限 {args.max_file_lines} 行)")
    if n_files == 0:
        print("  [红灯] 一个 Python 文件都没扫到 —— 空目录/路径错误不能算全绿")
        return False
    if n_violations:
        ok = False

    # 3. 重复函数体检测 (结构级)
    if args.min_dup_lines > 0:
        dup_groups = find_duplicate_functions(files, args.min_dup_lines)
        for group in dup_groups:
            n_violations += 1
            where = ", ".join(f"{f}:{ln} {nm}()" for f, ln, nm in group)
            print(f"  [红灯] 重复函数体 (结构相同, >= {args.min_dup_lines} 行): {where}")
        if dup_groups:
            ok = False
            print(f"  [扫描] 发现 {len(dup_groups)} 组重复函数体")

    # 4. 覆盖率 (算法服务必须守)
    if args.with_tests:
        if not _pytest_available():
            print("  [跳过] 未找到 pytest/pytest-cov, "
                  "pip install pytest pytest-cov 后可启用覆盖率门禁")
        else:
            print(f"  [运行] pytest + 覆盖率门禁 >= {args.coverage}%")
            if not run_pytest_coverage(target, args.coverage):
                print("  [红灯] 测试失败或覆盖率不达标")
                ok = False
            else:
                print("  [通过] 测试与覆盖率")
    else:
        print("  [提示] 加 --with-tests 可同时跑 pytest 覆盖率门禁")

    # 5. 变异测试 (存活变异体 = 断言强度不够, 覆盖率测不出来)
    if args.with_mutation:
        if not run_mutation_backend(target, args.mutation_score):
            ok = False
    else:
        print("  [提示] 加 --with-mutation 可启用 mutmut 变异得分门禁 "
              f"(阈值 {args.mutation_score}%)")

    return ok


# ============================================================
# 前端侧: ESLint flat config, 与公司项目配置互不干扰
# ============================================================

STRYKER_CONFIG_FILES = ("stryker.config.json", "stryker.config.mjs",
                        "stryker.conf.json", "stryker.conf.mjs")


def run_mutation_frontend(target, min_score):
    """Stryker 变异门禁: 有配置才跑, 阈值以 stryker 配置内 thresholds.break 为准
    (低于 break 时 stryker 退出码非 0, 本门禁直接采用)。"""
    cfg = next((c for c in STRYKER_CONFIG_FILES if (target / c).exists()), None)
    if cfg is None:
        print("  [跳过] 未找到 stryker 配置 (npx stryker init 生成), 跳过前端变异测试")
        return True
    print(f"  [运行] stryker run (配置 {cfg}, 可能很慢; "
          f"建议 thresholds.break >= {min_score})")
    r = subprocess.run(["npx", "stryker", "run"], cwd=target)
    if r.returncode != 0:
        print(f"  [红灯] stryker 退出码非 0 (低于 thresholds.break 或运行失败, 口径见 {cfg})")
        return False
    print("  [通过] Stryker 变异测试")
    return True


def check_frontend(args):
    print(f"\n=== 前端门禁 ({args.frontend_dir}) ===")
    target = Path(args.frontend_dir)
    config = Path(__file__).parent / "eslint.quality.config.mjs"
    if not config.exists():
        print(f"  [错误] 找不到 {config}, 请与 quality_gate.py 放在同一目录")
        return False

    src = args.frontend_src
    cmd = ["npx", "eslint", "--no-warn-ignored",
           "--config", str(config), src]
    r = subprocess.run(cmd, cwd=target, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout + r.stderr).strip()
    if out:
        print(out)
    ok = r.returncode == 0
    print("  [通过] ESLint 复杂度规则" if ok else
          "  [红灯] ESLint 复杂度规则 (complexity<=6 / 函数<=80行 / 嵌套<=4层)")

    if args.with_tests:
        if args.frontend_coverage is not None:
            # 一次跑完: coverage 模式本身会执行全部测试, 行覆盖率低于阈值即非 0 退出
            print(f"  [运行] vitest run --coverage (行覆盖率门禁 >= {args.frontend_coverage}%)")
            r2 = subprocess.run(
                ["npx", "vitest", "run", "--coverage",
                 f"--coverage.thresholds.lines={args.frontend_coverage}"],
                cwd=target)
            if r2.returncode != 0:
                print("  [红灯] 前端测试失败或覆盖率不达标 "
                      "(缺 @vitest/coverage-v8 时: npm i -D @vitest/coverage-v8)")
                ok = False
            else:
                print(f"  [通过] 前端测试与覆盖率 >= {args.frontend_coverage}%")
        else:
            print("  [运行] vitest run")
            r2 = subprocess.run(["npx", "vitest", "run"], cwd=target)
            if r2.returncode != 0:
                print("  [红灯] 前端测试失败")
                ok = False

    if args.with_mutation:
        if not run_mutation_frontend(target, args.mutation_score):
            ok = False
    else:
        print("  [提示] 加 --with-mutation 可启用 Stryker 变异测试门禁 "
              "(需先 npx stryker init)")

    return ok


# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="质量门禁: 把君子协定变成跑不掉的检查")
    p.add_argument("scope", choices=["backend", "frontend", "all"])
    p.add_argument("--backend-dir", default="backend")
    p.add_argument("--frontend-dir", default="frontend")
    p.add_argument("--frontend-src", default="src")
    p.add_argument("--max-complexity", type=int, default=DEFAULT_MAX_COMPLEXITY,
                   help=f"圈复杂度上限 (默认 {DEFAULT_MAX_COMPLEXITY}, Bob 给 Agent 的宽松档)")
    p.add_argument("--max-fn-lines-py", type=int, default=DEFAULT_MAX_FN_LINES_PY)
    p.add_argument("--max-params", type=int, default=DEFAULT_MAX_PARAMS,
                   help=f"函数参数个数上限 (默认 {DEFAULT_MAX_PARAMS})")
    p.add_argument("--max-file-lines", type=int, default=DEFAULT_MAX_FILE_LINES,
                   help=f"单文件行数上限 (默认 {DEFAULT_MAX_FILE_LINES})")
    p.add_argument("--min-dup-lines", type=int, default=DEFAULT_MIN_DUP_LINES,
                   help=f"重复函数体检测的最小行数阈值, 0=关闭 (默认 {DEFAULT_MIN_DUP_LINES})")
    p.add_argument("--coverage", type=int, default=DEFAULT_COVERAGE,
                   help="后端覆盖率门禁 (%%)")
    p.add_argument("--mutation-score", type=int, default=DEFAULT_MUTATION_SCORE,
                   help=f"变异得分门禁 (%%, 默认 {DEFAULT_MUTATION_SCORE}; 前端 Stryker 以其配置内 thresholds.break 为准)")
    p.add_argument("--frontend-coverage", type=int, default=None,
                   help="前端覆盖率门禁 (%%); 传入后 --with-tests 以 vitest --coverage 运行并卡行覆盖率")
    p.add_argument("--with-tests", action="store_true",
                   help="同时跑 pytest 覆盖率 / vitest")
    p.add_argument("--with-mutation", action="store_true",
                   help="同时跑变异测试门禁 (后端 mutmut / 前端 stryker)")
    p.add_argument("--full", action="store_true",
                   help="= --with-tests --with-mutation, 全量门禁")
    p.add_argument("--exclude", action="append", default=[],
                   help="额外排除的目录名, 可多次使用")
    args = p.parse_args()

    if args.full:
        args.with_tests = True
        args.with_mutation = True

    ok = True
    if args.scope in ("backend", "all"):
        ok &= check_backend(args)
    if args.scope in ("frontend", "all"):
        ok &= check_frontend(args)

    print("\n" + "=" * 40)
    if ok:
        print("质量门禁: 全部通过 ✔  (可以交付/合并)")
        sys.exit(0)
    else:
        print("质量门禁: 存在红灯 ✘  (修复前不算完成)")
        sys.exit(1)


if __name__ == "__main__":
    main()
