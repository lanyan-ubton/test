#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
质量门禁工具 (quality-gate)
============================
思路来源: Bob 大叔 —— "Agent 可以忘掉提示词第 80 条规定, 却绕不过一个失败的测试。"
把提示词里的君子协定(圈复杂度/函数长度/覆盖率/变异得分)变成跑不掉的自动化检查。
CI 动不了没关系: 本地跑 + 让 AI 交付前必须跑, 效果一样。

用法:
  python quality_gate.py                     # 自动检测当前目录所有语言
  python quality_gate.py --lang python       # 只检查 Python
  python quality_gate.py --lang java         # 只检查 Java
  python quality_gate.py --lang typescript   # 只检查 TypeScript
  python quality_gate.py --path backend      # 指定目标目录
  python quality_gate.py --with-tests        # + 测试覆盖率
  python quality_gate.py --with-mutation     # + 变异测试
  python quality_gate.py --full              # 全部 (静态 + 测试 + 变异)

退出码: 0 = 全绿, 1 = 有红灯。AI 工具(Aider/Claude Code)靠退出码判断能不能算"完成"。

目标环境: Linux 服务器 (CI/容器均可直接跑)。
依赖: 按检测语言而异 —— 见各语言 Checker 的 is_available() 说明。
  缺失必装工具时对应语言判红灯; 可选工具缺失时跳过并提示。
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
DEFAULT_MAX_FN_LINES = 50       # 函数行数上限
DEFAULT_MAX_FILE_LINES = 500    # 单文件行数上限
DEFAULT_MIN_DUP_LINES = 6       # 重复函数体检测: 函数至少这么长才参与比较, 0=关闭
DEFAULT_COVERAGE = 85           # 覆盖率门禁 (%)
DEFAULT_MUTATION_SCORE = 60     # 变异得分门禁 (%)

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".next",
    "dist", "build", ".mypy_cache", ".pytest_cache", "migrations",
    "mutants",  # mutmut 生成的变异体目录, 不是业务代码
}


# ============================================================
# 插件架构: 语言检测 + Checker 注册
# ============================================================

class Checker:
    """语言检查器基类。每种语言实现一个子类, 注册到 _CHECKERS 列表。"""
    name: str = ""
    extensions: set = set()

    def is_available(self):
        """返回 (bool, str): 该语言的必需工具是否就绪, 以及缺失提示。"""
        return True, ""

    def check(self, target: Path, args) -> bool:
        """执行检查, 返回是否全部通过。"""
        raise NotImplementedError

    def __repr__(self):
        return f"<{self.__class__.__name__}>"


_CHECKERS = []  # list[Checker]


def register(checker: Checker):
    _CHECKERS.append(checker)
    return checker


def detect_languages(target):
    """扫描目标目录, 返回有对应文件存在的已注册 Checker。"""
    found_exts = set()
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for f in filenames:
            ext = Path(f).suffix.lower()
            if ext:
                found_exts.add(ext)
    result = []
    for c in _CHECKERS:
        if c.extensions & found_exts:
            result.append(c)
    return result


# ============================================================
# 共享工具: AST 函数长度检查 + 重复函数体检测 (Python 专用)
# ============================================================

def check_python_file(path, max_fn_lines, max_file_lines):
    """返回该文件的违规列表 [(行号, 函数名, 问题描述)]。
    仅检查函数长度和文件长度 —— 圈复杂度/命名/参数个数已由 ruff 负责。"""
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
        lines = (node.end_lineno or node.lineno) - node.lineno + 1
        if lines > max_fn_lines:
            violations.append((node.lineno, node.name,
                               f"函数 {lines} 行 > {max_fn_lines} 行"))
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
    """函数体结构指纹: 剔除 docstring -> 局部变量名归一化 -> AST dump 的 sha256。"""
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body:
        return None
    mod = ast.Module(body=body, type_ignores=[])
    _VarNormalizer(_collect_kept_names(mod)).visit(mod)
    dump = ast.dump(mod, annotate_fields=False, include_attributes=False)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _iter_top_functions(tree):
    """产出模块级函数与类方法, 不深入函数体内的嵌套函数。"""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child
            elif isinstance(child, ast.ClassDef):
                stack.append(child)


def find_duplicate_functions(files, min_lines):
    """返回重复函数组 [[(文件, 行号, 函数名), ...], ...]。"""
    groups = {}
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in _iter_top_functions(tree):
            n = (node.end_lineno or node.lineno) - node.lineno + 1
            if n < min_lines:
                continue
            h = _fn_body_hash(node)
            if h:
                groups.setdefault(h, []).append((f, node.lineno, node.name))
    return [g for g in groups.values() if len(g) > 1]


# ============================================================
# Python Checker: ruff (lint/复杂度/命名) + AST (函数长度)
# ============================================================

class PythonChecker(Checker):
    name = "python"
    extensions = {".py"}

    def is_available(self):
        if not shutil.which("ruff"):
            return False, "pip install ruff"
        return True, ""

    def check(self, target, args):
        print(f"\n=== Python 门禁 ({target}) ===")
        ok = True

        # 1. ruff (lint + 圈复杂度 + 参数个数 + 命名)
        ruff_config = Path(__file__).parent / "ruff.quality.toml"
        if not ruff_config.exists():
            print(f"  [错误] 找不到 {ruff_config}")
            return False
        if not shutil.which("ruff"):
            print("  [错误] 未找到 ruff, 请先安装: pip install ruff")
            return False
        r = subprocess.run(
            ["ruff", "check", "--config", str(ruff_config), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(r.stdout)
            ok = False
        else:
            print("  [通过] ruff (lint + 圈复杂度 + 命名)")

        # 2. 函数长度 + 文件行数 (AST 补充)
        n_files, n_violations = 0, 0
        files = list(iter_py_files(target, set(args.exclude)))
        for f in files:
            n_files += 1
            for lineno, name, msg in check_python_file(
                    f, args.max_fn_lines, args.max_file_lines):
                n_violations += 1
                print(f"  [红灯] {f}:{lineno} {name}() — {msg}")
        if n_files > 0:
            print(f"  [扫描] {n_files} 个 Python 文件, {n_violations} 处违规 "
                  f"(函数上限 {args.max_fn_lines} 行, 文件上限 {args.max_file_lines} 行)")
        if n_files == 0:
            print("  [红灯] 一个 Python 文件都没扫到")
            return False
        if n_violations:
            ok = False

        # 3. 重复函数体检测
        if args.min_dup_lines > 0 and files:
            dup_groups = find_duplicate_functions(files, args.min_dup_lines)
            for group in dup_groups:
                where = ", ".join(f"{f}:{ln} {nm}()" for f, ln, nm in group)
                print(f"  [红灯] 重复函数体 (>= {args.min_dup_lines} 行): {where}")
            if dup_groups:
                ok = False
                print(f"  [扫描] 发现 {len(dup_groups)} 组重复函数体")

        # 4. 测试覆盖率
        if args.with_tests:
            try:
                import pytest  # noqa: F401
                import pytest_cov  # noqa: F401
                print(f"  [运行] pytest + 覆盖率门禁 >= {args.coverage}%")
                r2 = subprocess.run(
                    [sys.executable, "-m", "pytest", "--cov=.", "-q",
                     f"--cov-fail-under={args.coverage}",
                     "--cov-report=term-missing"], cwd=target)
                if r2.returncode != 0:
                    print("  [红灯] 测试失败或覆盖率不达标")
                    ok = False
                else:
                    print("  [通过] 测试与覆盖率")
            except ImportError:
                print("  [跳过] 未找到 pytest/pytest-cov, "
                      "pip install pytest pytest-cov 后可启用覆盖率门禁")
        else:
            print("  [提示] 加 --with-tests 可同时跑 pytest 覆盖率门禁")

        # 5. 变异测试
        if args.with_mutation:
            if not shutil.which("mutmut"):
                print("  [跳过] 未找到 mutmut, pip install mutmut 后可启用变异测试")
            else:
                print("  [运行] mutmut 变异测试")
                r3 = subprocess.run(["mutmut", "run"], cwd=target)
                if r3.returncode != 0:
                    print("  [红灯] mutmut run 失败")
                    ok = False
                else:
                    subprocess.run(["mutmut", "export-cicd-stats"],
                                   cwd=target, capture_output=True)
                    stats_path = target / "mutants" / "mutmut-cicd-stats.json"
                    if stats_path.exists():
                        stats = json.loads(stats_path.read_text(encoding="utf-8"))
                        killed = int(stats.get("killed", 0))
                        timeout_n = int(stats.get("timeout", 0))
                        total = int(stats.get("total", 0))
                        skipped = int(stats.get("skipped", 0))
                        tested = total - skipped
                        if tested > 0:
                            score = (killed + timeout_n) / tested * 100
                            print(f"  [指标] 变异得分 {score:.1f}%")
                            if score < args.mutation_score:
                                print(f"  [红灯] 变异得分 < {args.mutation_score}%")
                                ok = False
                            else:
                                print(f"  [通过] 变异得分 >= {args.mutation_score}%")
        else:
            print("  [提示] 加 --with-mutation 可启用 mutmut 变异得分门禁")

        return ok


# ============================================================
# TypeScript Checker: ESLint (复杂度/命名) + tsc (类型)
# ============================================================

STRYKER_CONFIG_FILES = ("stryker.config.json", "stryker.config.mjs",
                        "stryker.conf.json", "stryker.conf.mjs")


class TypeScriptChecker(Checker):
    name = "typescript"
    extensions = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}

    def is_available(self):
        if not shutil.which("npx"):
            return False, "需安装 Node.js (npm/npx)"
        return True, ""

    def check(self, target, args):
        print(f"\n=== TypeScript 门禁 ({target}) ===")
        ok = True
        eslint_config = Path(__file__).parent / "eslint.quality.config.mjs"
        if not eslint_config.exists():
            print(f"  [错误] 找不到 {eslint_config}")
            return False

        # 1. ESLint (复杂度 + 命名 + 体积)
        frontend_src = getattr(args, "frontend_src", "src")
        cmd = ["npx", "eslint", "--no-warn-ignored",
               "--config", str(eslint_config), frontend_src]
        r = subprocess.run(cmd, cwd=target, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout + r.stderr).strip()
        if out:
            print(out)
        if r.returncode != 0:
            ok = False
            print("  [红灯] ESLint 复杂度规则 (complexity<=6 / 函数<=80行 / 嵌套<=4层)")
        else:
            print("  [通过] ESLint 复杂度规则")

        # 2. 测试 + 覆盖率
        if args.with_tests:
            if getattr(args, "frontend_coverage", None) is not None:
                print(f"  [运行] vitest --coverage (>= {args.frontend_coverage}%)")
                r2 = subprocess.run(
                    ["npx", "vitest", "run", "--coverage",
                     f"--coverage.thresholds.lines={args.frontend_coverage}"],
                    cwd=target)
                if r2.returncode != 0:
                    print("  [红灯] 前端测试失败或覆盖率不达标")
                    ok = False
                else:
                    print(f"  [通过] 前端测试与覆盖率 >= {args.frontend_coverage}%")
            else:
                print("  [运行] vitest run")
                r2 = subprocess.run(["npx", "vitest", "run"], cwd=target)
                if r2.returncode != 0:
                    print("  [红灯] 前端测试失败")
                    ok = False

        # 3. 变异测试 (Stryker)
        if args.with_mutation:
            cfg = next((c for c in STRYKER_CONFIG_FILES
                        if (target / c).exists()), None)
            if cfg is None:
                print("  [跳过] 未找到 stryker 配置 (npx stryker init 生成)")
            else:
                print(f"  [运行] stryker run (配置 {cfg})")
                r3 = subprocess.run(["npx", "stryker", "run"], cwd=target)
                if r3.returncode != 0:
                    print("  [红灯] stryker 变异测试失败")
                    ok = False
                else:
                    print("  [通过] Stryker 变异测试")
        else:
            print("  [提示] 加 --with-mutation 可启用 Stryker 变异测试")

        return ok


# ============================================================
# Java Checker: Checkstyle (lint/命名) + PMD (复杂度/重复)
# ============================================================

class JavaChecker(Checker):
    name = "java"
    extensions = {".java"}

    def is_available(self):
        missing = []
        if not shutil.which("checkstyle"):
            missing.append("checkstyle")
        if not shutil.which("pmd"):
            missing.append("pmd")
        if missing:
            return False, f"需安装: {', '.join(missing)}"
        return True, ""

    def check(self, target, args):
        print(f"\n=== Java 门禁 ({target}) ===")
        ok = True

        # 1. Checkstyle (lint + 命名 + 函数长度)
        cs_config = Path(__file__).parent / "checkstyle-quality.xml"
        if not cs_config.exists():
            print(f"  [错误] 找不到 {cs_config}")
            return False
        if not shutil.which("checkstyle"):
            print("  [错误] 未找到 checkstyle, 请安装后确保 checkstyle 在 PATH 中")
            return False
        r = subprocess.run(
            ["checkstyle", "-c", str(cs_config), "-r", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            out = (r.stdout + r.stderr).strip()
            if out:
                print(out)
            ok = False
            print("  [红灯] Checkstyle (命名/函数长度/参数个数)")
        else:
            print("  [通过] Checkstyle")

        # 2. PMD (圈复杂度 + 重复代码)
        if shutil.which("pmd"):
            r2 = subprocess.run(
                ["pmd", "check", "-d", str(target),
                 "-R", "category/java/design.xml,category/java/bestpractices.xml",
                 "--fail-on-violation"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            out2 = (r2.stdout + r2.stderr).strip()
            if out2:
                print(out2)
            if r2.returncode != 0:
                ok = False
                print("  [红灯] PMD (复杂度 + 最佳实践)")
            else:
                print("  [通过] PMD")
        else:
            print("  [跳过] 未找到 pmd, 安装后可启用复杂度/最佳实践检查")

        # 3. 测试覆盖率
        if args.with_tests:
            if (target / "pom.xml").exists() and shutil.which("mvn"):
                print(f"  [运行] mvn test (覆盖率门禁 >= {args.coverage}%)")
                r3 = subprocess.run(
                    ["mvn", "test", "jacoco:check",
                     f"-Djacoco.check.coverage={args.coverage / 100}"],
                    cwd=target)
                if r3.returncode != 0:
                    print("  [红灯] 测试失败或覆盖率不达标")
                    ok = False
                else:
                    print("  [通过] 测试与覆盖率")
            elif (target / "build.gradle").exists() and shutil.which("gradle"):
                print(f"  [运行] gradle test jacocoTestCoverageVerification")
                r3 = subprocess.run(["gradle", "test",
                                     "jacocoTestCoverageVerification"],
                                    cwd=target)
                if r3.returncode != 0:
                    print("  [红灯] 测试失败或覆盖率不达标")
                    ok = False
                else:
                    print("  [通过] 测试与覆盖率")
            else:
                print("  [跳过] 未找到 mvn/gradle, 跳过 Java 测试覆盖率")
        else:
            print("  [提示] 加 --with-tests 可同时跑测试覆盖率门禁")

        return ok


# 注册所有语言检查器
register(PythonChecker())
register(TypeScriptChecker())
register(JavaChecker())


# ============================================================
# 主入口
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="质量门禁: 把君子协定变成跑不掉的检查")
    p.add_argument("--path", default=".",
                   help="项目根目录 (默认当前目录)")
    p.add_argument("--lang", default="all",
                   choices=["all", "python", "typescript", "java"],
                   help="指定检查语言, all=自动检测 (默认 all)")
    p.add_argument("--frontend-src", default="src",
                   help="前端源码目录 (TypeScript 检查用, 默认 src)")
    p.add_argument("--max-fn-lines", type=int, default=DEFAULT_MAX_FN_LINES,
                   help=f"函数行数上限 (默认 {DEFAULT_MAX_FN_LINES})")
    p.add_argument("--max-file-lines", type=int, default=DEFAULT_MAX_FILE_LINES,
                   help=f"单文件行数上限 (默认 {DEFAULT_MAX_FILE_LINES})")
    p.add_argument("--min-dup-lines", type=int, default=DEFAULT_MIN_DUP_LINES,
                   help=f"重复函数体检测最小行数, 0=关闭 (默认 {DEFAULT_MIN_DUP_LINES})")
    p.add_argument("--coverage", type=int, default=DEFAULT_COVERAGE,
                   help="覆盖率门禁 (%%)")
    p.add_argument("--mutation-score", type=int, default=DEFAULT_MUTATION_SCORE,
                   help=f"变异得分门禁 (%%, 默认 {DEFAULT_MUTATION_SCORE})")
    p.add_argument("--frontend-coverage", type=int, default=None,
                   help="前端行覆盖率门禁 (%%)")
    p.add_argument("--with-tests", action="store_true",
                   help="同时跑测试 + 覆盖率门禁")
    p.add_argument("--with-mutation", action="store_true",
                   help="同时跑变异测试门禁")
    p.add_argument("--full", action="store_true",
                   help="= --with-tests --with-mutation")
    p.add_argument("--exclude", action="append", default=[],
                   help="额外排除的目录名, 可多次使用")
    args = p.parse_args()

    if args.full:
        args.with_tests = True
        args.with_mutation = True

    target = Path(args.path).resolve()
    if not target.is_dir():
        print(f"[红灯] 目录不存在: {target}")
        sys.exit(1)

    # 确定要运行的检查器
    if args.lang == "all":
        checkers = detect_languages(target)
        if not checkers:
            print(f"[红灯] 在 {target} 中未检测到任何已知语言文件")
            sys.exit(1)
    else:
        checkers = [c for c in _CHECKERS if c.name == args.lang]
        if not checkers:
            print(f"[红灯] 未知的语言: {args.lang}")
            sys.exit(1)

    print(f"质量门禁 — 目标: {target}")
    print(f"检测到的语言: {', '.join(c.name for c in checkers)}")

    ok = True
    for c in checkers:
        avail, hint = c.is_available()
        if not avail:
            print(f"\n=== {c.name} 门禁 ===")
            print(f"  [错误] 缺少必需工具: {hint}")
            ok = False
            continue
        ok &= c.check(target, args)

    print("\n" + "=" * 40)
    if ok:
        print("质量门禁: 全部通过 ✔  (可以交付/合并)")
        sys.exit(0)
    else:
        print("质量门禁: 存在红灯 ✘  (修复前不算完成)")
        sys.exit(1)


if __name__ == "__main__":
    main()
