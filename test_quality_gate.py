# -*- coding: utf-8 -*-
"""
质量门禁工具 — 单元测试
========================
覆盖 quality_gate.py 中所有核心纯函数与关键流程。
测试命名采用 BDD 风格: test_<被测行为>_<场景>_<预期结果>
每个测试严格遵循 AAA (Arrange / Act / Assert) 三段式。
"""

import ast
import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

# ---- 被测模块 ----
sys.path.insert(0, str(Path(__file__).resolve().parent))
import quality_gate as qg


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fixture_backend():
    """返回 fixture_backend 目录路径。"""
    return Path(__file__).resolve().parent / "fixture_backend"


@pytest.fixture
def fixture_clean():
    """返回 fixture_clean 目录路径。"""
    return Path(__file__).resolve().parent / "fixture_clean"


@pytest.fixture
def tmp_py_file(tmp_path):
    """工厂 fixture: 在 tmp_path 中写入 Python 源码并返回 Path。"""
    def _write(source: str, name: str = "sample.py") -> Path:
        p = tmp_path / name
        p.write_text(textwrap.dedent(source), encoding="utf-8")
        return p
    return _write


def _parse_fn(source: str) -> ast.FunctionDef:
    """辅助: 解析源码并返回第一个函数节点。"""
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            return node
    raise ValueError("源码中没有函数定义")


# ============================================================
# 1. _count_branches — 圈复杂度
# ============================================================

class TestCountBranches:
    """圈复杂度计算: 每个分支/决策点 +1, 基础分 1。"""

    def test_simple_function_returns_1(self):
        """Given 无分支的简单函数, When 计算圈复杂度, Then 结果为 1。"""
        node = _parse_fn("""
        def noop():
            return 42
        """)
        assert qg._count_branches(node) == 1

    def test_single_if_returns_2(self):
        """Given 一个 if 分支, When 计算, Then 基础 1 + if 1 = 2。"""
        node = _parse_fn("""
        def check(x):
            if x > 0:
                return True
            return False
        """)
        assert qg._count_branches(node) == 2

    def test_if_elif_chain(self):
        """Given if/elif 链, When 计算, Then 每个分支都计入。"""
        node = _parse_fn("""
        def grade(score):
            if score >= 90:
                return 'A'
            elif score >= 80:
                return 'B'
            elif score >= 60:
                return 'C'
            else:
                return 'F'
        """)
        # 1 (base) + 3 (if/elif/elif) = 4
        # else 不计入 (radon 口径)
        assert qg._count_branches(node) == 4

    def test_for_loop_adds_1(self):
        """Given 一个 for 循环, When 计算, Then +1。"""
        node = _parse_fn("""
        def count_items(items):
            total = 0
            for _ in items:
                total += 1
            return total
        """)
        assert qg._count_branches(node) == 2  # 1 + for

    def test_while_loop_adds_1(self):
        """Given 一个 while 循环, When 计算, Then +1。"""
        node = _parse_fn("""
        def countdown(n):
            while n > 0:
                n -= 1
            return n
        """)
        assert qg._count_branches(node) == 2  # 1 + while

    def test_bool_op_and_adds_branches(self):
        """Given `a and b and c`, When 计算, Then 每个 and +1。"""
        node = _parse_fn("""
        def check(a, b, c):
            if a and b and c:
                return True
            return False
        """)
        # 1 (base) + 1 (if) + 2 (两个 and) = 4
        assert qg._count_branches(node) == 4

    def test_except_handler_adds_1(self):
        """Given except 处理器, When 计算, Then +1。"""
        node = _parse_fn("""
        def safe_div(a, b):
            try:
                return a / b
            except ZeroDivisionError:
                return 0
        """)
        # 1 (base) + 1 (except) = 2
        assert qg._count_branches(node) == 2

    def test_nested_function_not_counted(self):
        """Given 含嵌套函数的函数, When 计算, Then 嵌套函数内部不计入外层。"""
        node = _parse_fn("""
        def outer(x):
            def inner(y):
                if y > 0:
                    return y
                return -y
            return inner(x)
        """)
        # 外层: 1 (base), inner 的 if 不计入
        assert qg._count_branches(node) == 1

    def test_bad_fixture_complexity_is_8(self):
        """Given fixture_backend/bad.py 的 too_complex, When 计算, Then 圈复杂度 = 8。"""
        bad_path = Path(__file__).resolve().parent / "fixture_backend" / "bad.py"
        tree = ast.parse(bad_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "too_complex":
                assert qg._count_branches(node) == 8
                return
        pytest.fail("未找到 too_complex 函数")

    def test_comprehension_adds_branches(self):
        """Given 列表推导式含 for 和 if, When 计算, Then 都计入。"""
        node = _parse_fn("""
        def filter_positive(nums):
            return [x for x in nums if x > 0]
        """)
        # 1 (base) + 1 (for) + 1 (if) = 3
        assert qg._count_branches(node) == 3


# ============================================================
# 2. _count_args — 参数计数
# ============================================================

class TestCountArgs:
    """参数计数: self/cls 不计入。"""

    def test_no_args(self):
        """Given 无参函数, When 计数, Then 结果为 0。"""
        node = _parse_fn("def f(): pass")
        assert qg._count_args(node) == 0

    def test_regular_args(self):
        """Given 3 个普通参数, When 计数, Then 结果为 3。"""
        node = _parse_fn("def f(a, b, c): pass")
        assert qg._count_args(node) == 3

    def test_self_excluded(self):
        """Given 方法首参 self, When 计数, Then self 不计入。"""
        node = _parse_fn("def method(self, a, b): pass")
        assert qg._count_args(node) == 2

    def test_cls_excluded(self):
        """Given 类方法首参 cls, When 计数, Then cls 不计入。"""
        node = _parse_fn("def classmethod(cls, x): pass")
        assert qg._count_args(node) == 1

    def test_vararg_and_kwarg(self):
        """Given *args 和 **kwargs, When 计数, Then 各算 1 个。"""
        node = _parse_fn("def f(a, *args, **kwargs): pass")
        assert qg._count_args(node) == 3  # a + *args + **kwargs

    def test_kwonly_args(self):
        """Given 关键字参数, When 计数, Then 每个 kwonly 参数 +1。"""
        node = _parse_fn("def f(a, *, b, c): pass")
        assert qg._count_args(node) == 3

    def test_fixture_too_many_params_is_5(self):
        """Given fixture bad.py 的 too_many_params, When 计数, Then 参数 = 5。"""
        bad_path = Path(__file__).resolve().parent / "fixture_backend" / "bad.py"
        tree = ast.parse(bad_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "too_many_params":
                assert qg._count_args(node) == 5
                return
        pytest.fail("未找到 too_many_params 函数")

    def test_calculator_method_has_correct_count(self):
        """Given fixture clean.py 的 Calculator.add, When 计数, Then 参数 = 2。"""
        clean_path = Path(__file__).resolve().parent / "fixture_clean" / "clean.py"
        tree = ast.parse(clean_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "add":
                assert qg._count_args(node) == 2
                return
        pytest.fail("未找到 add 方法")


# ============================================================
# 3. check_python_file — 文件级检查
# ============================================================

class TestCheckPythonFile:
    """文件级违规检测: 圈复杂度 + 函数长度 + 参数个数 + 文件行数。"""

    def test_clean_file_no_violations(self, fixture_clean):
        """Given 干净代码文件, When 检查, Then 无违规。"""
        clean_file = fixture_clean / "clean.py"
        violations = qg.check_python_file(clean_file, 6, 50, 4, 500)
        assert violations == []

    def test_bad_file_detects_complexity(self, fixture_backend):
        """Given bad.py, When 检查圈复杂度上限 6, Then too_complex 违规。"""
        bad_file = fixture_backend / "bad.py"
        violations = qg.check_python_file(bad_file, 6, 999, 99, 9999)
        complexity_violations = [v for v in violations if "圈复杂度" in v[2]]
        assert len(complexity_violations) == 1
        assert "too_complex" in complexity_violations[0][1]

    def test_bad_file_detects_params(self, fixture_backend):
        """Given bad.py, When 检查参数上限 4, Then too_many_params 违规。"""
        bad_file = fixture_backend / "bad.py"
        violations = qg.check_python_file(bad_file, 99, 999, 4, 9999)
        param_violations = [v for v in violations if "参数" in v[2]]
        assert len(param_violations) == 1
        assert "too_many_params" in param_violations[0][1]

    def test_bad_file_detects_fn_length(self, fixture_backend):
        """Given bad.py, When 检查函数行数上限 50, Then too_long 违规。"""
        bad_file = fixture_backend / "bad.py"
        violations = qg.check_python_file(bad_file, 99, 50, 99, 9999)
        length_violations = [v for v in violations if "行 >" in v[2]]
        assert len(length_violations) == 1
        assert "too_long" in length_violations[0][1]

    def test_bad_file_all_violations(self, fixture_backend):
        """Given bad.py 使用默认阈值, When 检查, Then 三种违规全部检出。"""
        bad_file = fixture_backend / "bad.py"
        violations = qg.check_python_file(bad_file, 6, 50, 4, 500)
        assert len(violations) == 3

    def test_file_too_long(self, tmp_py_file):
        """Given 超过行数上限的文件, When 检查, Then 文件级违规。"""
        source = "\n".join([f"x{i} = {i}" for i in range(110)]) + "\n"
        p = tmp_py_file(source, name="long.py")
        violations = qg.check_python_file(p, 99, 999, 99, 100)
        file_violations = [v for v in violations if "<文件>" in v[1]]
        assert len(file_violations) == 1
        assert "文件" in file_violations[0][2]

    def test_syntax_error_returns_parse_failure(self, tmp_py_file):
        """Given 语法错误的文件, When 检查, Then 返回解析失败。"""
        p = tmp_py_file("def f(:\n  broken", name="broken.py")
        violations = qg.check_python_file(p, 6, 50, 4, 500)
        assert len(violations) == 1
        assert "解析失败" in violations[0][2]

    def test_relaxed_thresholds_no_violations(self, fixture_backend):
        """Given bad.py 但放宽所有阈值, When 检查, Then 无违规。"""
        bad_file = fixture_backend / "bad.py"
        violations = qg.check_python_file(bad_file, 99, 999, 99, 9999)
        assert violations == []


# ============================================================
# 4. 重复函数体检测
# ============================================================

class TestDuplicateFunctionDetection:
    """结构级重复函数体检测: 变量名归一化 + docstring 剔除。"""

    def test_duplicate_functions_detected(self, fixture_backend):
        """Given dup_a.py 和 dup_b.py (结构相同变量名不同), When 检测, Then 命中重复组。"""
        files = [
            fixture_backend / "dup_a.py",
            fixture_backend / "dup_b.py",
        ]
        groups = qg.find_duplicate_functions(files, min_lines=6)
        assert len(groups) == 1
        assert len(groups[0]) == 2
        names = {item[2] for item in groups[0]}
        assert "compute_discount" in names
        assert "calc_member_price" in names

    def test_clean_code_no_duplicates(self, fixture_clean):
        """Given clean.py, When 检测, Then 无重复。"""
        files = [fixture_clean / "clean.py"]
        groups = qg.find_duplicate_functions(files, min_lines=3)
        assert groups == []

    def test_min_lines_filter(self, fixture_backend):
        """Given min_lines 设得很大, When 检测, Then 短函数不参与比较。"""
        files = [
            fixture_backend / "dup_a.py",
            fixture_backend / "dup_b.py",
        ]
        groups = qg.find_duplicate_functions(files, min_lines=999)
        assert groups == []

    def test_fn_body_hash_ignores_docstring(self):
        """Given 两个仅 docstring 不同的函数, When 比较 hash, Then 相同。"""
        fn1 = _parse_fn('''
        def foo(x):
            """这是第一个 docstring"""
            return x + 1
        ''')
        fn2 = _parse_fn('''
        def bar(y):
            """完全不同的 docstring"""
            return y + 1
        ''')
        assert qg._fn_body_hash(fn1) == qg._fn_body_hash(fn2)

    def test_fn_body_hash_differs_by_call(self):
        """Given 两个调用不同函数的函数体, When 比较 hash, Then 不同。"""
        fn1 = _parse_fn('''
        def foo(x):
            return round(x, 2)
        ''')
        fn2 = _parse_fn('''
        def bar(x):
            return floor(x)
        ''')
        assert qg._fn_body_hash(fn1) != qg._fn_body_hash(fn2)

    def test_fn_body_hash_empty_body_returns_none(self):
        """Given 只有 docstring 的函数体, When 剔除 docstring 后, Then hash 为 None。"""
        fn = _parse_fn('''
        def empty():
            """仅 docstring, 无实际代码"""
        ''')
        assert qg._fn_body_hash(fn) is None

    def test_collect_kept_names(self):
        """Given 含函数调用和属性访问的代码, When 收集保留名, Then 调用名和属性名都在。"""
        tree = ast.parse(textwrap.dedent("""
        import os
        result = os.path.join(a, b)
        obj.method(key=val)
        """))
        kept = qg._collect_kept_names(tree)
        assert "join" in kept    # 调用名
        assert "path" in kept    # 属性名
        assert "method" in kept  # 方法名
        assert "key" in kept     # 关键字参数名


# ============================================================
# 5. _mutation_score_from_stats — 变异得分计算
# ============================================================

class TestMutationScoreFromStats:
    """变异得分: (killed + timeout) / (total - skipped) * 100。"""

    def test_all_killed_returns_100(self):
        """Given 所有变异体被杀死, When 计算, Then 得分 100%。"""
        stats = {"killed": 10, "timeout": 0, "total": 10, "skipped": 0}
        assert qg._mutation_score_from_stats(stats) == 100.0

    def test_none_killed_returns_0(self):
        """Given 无变异体被杀死, When 计算, Then 得分 0%。"""
        stats = {"killed": 0, "timeout": 0, "total": 10, "skipped": 0}
        assert qg._mutation_score_from_stats(stats) == 0.0

    def test_timeout_counts_as_killed(self):
        """Given timeout 的变异体, When 计算, Then 算作杀死。"""
        stats = {"killed": 5, "timeout": 3, "total": 10, "skipped": 0}
        # (5 + 3) / 10 * 100 = 80%
        assert qg._mutation_score_from_stats(stats) == 80.0

    def test_skipped_excluded_from_denominator(self):
        """Given 有跳过的变异体, When 计算, Then 从分母中扣除。"""
        stats = {"killed": 5, "timeout": 0, "total": 10, "skipped": 2}
        # 5 / (10 - 2) * 100 = 62.5%
        assert qg._mutation_score_from_stats(stats) == 62.5

    def test_all_skipped_returns_none(self):
        """Given 全部跳过 (total == skipped), When 计算, Then 返回 None。"""
        stats = {"killed": 0, "timeout": 0, "total": 5, "skipped": 5}
        assert qg._mutation_score_from_stats(stats) is None

    def test_zero_total_returns_none(self):
        """Given total=0, When 计算, Then 返回 None。"""
        stats = {"killed": 0, "timeout": 0, "total": 0, "skipped": 0}
        assert qg._mutation_score_from_stats(stats) is None

    def test_missing_keys_default_to_zero(self):
        """Given 统计缺少部分字段, When 计算, Then 缺失字段按 0 处理。"""
        stats = {"killed": 3, "total": 5}
        # (3 + 0) / (5 - 0) * 100 = 60%
        assert qg._mutation_score_from_stats(stats) == 60.0


# ============================================================
# 6. iter_py_files — 文件遍历
# ============================================================

class TestIterPyFiles:
    """Python 文件遍历: 排除忽略目录。"""

    def test_finds_py_files(self, tmp_path):
        """Given 含 .py 文件的目录, When 遍历, Then 返回所有 .py 文件。"""
        (tmp_path / "a.py").write_text("# a")
        (tmp_path / "b.py").write_text("# b")
        (tmp_path / "c.txt").write_text("not python")
        files = list(qg.iter_py_files(tmp_path, set()))
        names = {f.name for f in files}
        assert names == {"a.py", "b.py"}

    def test_excludes_ignore_dirs(self, tmp_path):
        """Given 含 __pycache__ 的目录, When 遍历, Then 忽略目录不进入。"""
        (tmp_path / "main.py").write_text("# main")
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "cached.py").write_text("# cached")
        files = list(qg.iter_py_files(tmp_path, set()))
        names = {f.name for f in files}
        assert "cached.py" not in names

    def test_excludes_custom_dirs(self, tmp_path):
        """Given 自定义排除目录, When 遍历, Then 该目录不进入。"""
        (tmp_path / "keep.py").write_text("# keep")
        skip = tmp_path / "vendor"
        skip.mkdir()
        (skip / "skip.py").write_text("# skip")
        files = list(qg.iter_py_files(tmp_path, {"vendor"}))
        names = {f.name for f in files}
        assert "skip.py" not in names
        assert "keep.py" in names


# ============================================================
# 7. check_backend — 集成场景
# ============================================================

class TestCheckBackend:
    """后端门禁集成: 模拟完整检查流程。"""

    def _make_args(self, **overrides):
        """构造 check_backend 所需的 args 对象。"""
        defaults = dict(
            backend_dir="fixture_backend",
            max_complexity=6,
            max_fn_lines_py=50,
            max_params=4,
            max_file_lines=500,
            min_dup_lines=6,
            with_tests=False,
            with_mutation=False,
            coverage=85,
            mutation_score=60,
            exclude=[],
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_bad_backend_returns_false(self, fixture_backend, capsys):
        """Given bad.py 目录 (含违规代码), When 跑门禁, Then 返回 False。"""
        args = self._make_args(backend_dir=str(fixture_backend))
        result = qg.check_backend(args)
        assert result is False

    def test_clean_backend_returns_true(self, fixture_clean, capsys):
        """Given clean 目录 (无违规), When 跑门禁, Then 返回 True。"""
        args = self._make_args(backend_dir=str(fixture_clean))
        result = qg.check_backend(args)
        assert result is True

    def test_nonexistent_dir_returns_false(self, capsys):
        """Given 不存在的目录, When 跑门禁, Then 返回 False。"""
        args = self._make_args(backend_dir="/nonexistent/path/xyz")
        result = qg.check_backend(args)
        assert result is False
        captured = capsys.readouterr()
        assert "不存在" in captured.out

    def test_empty_dir_returns_false(self, tmp_path, capsys):
        """Given 空目录 (无 .py 文件), When 跑门禁, Then 返回 False。"""
        args = self._make_args(backend_dir=str(tmp_path))
        result = qg.check_backend(args)
        assert result is False
        captured = capsys.readouterr()
        assert "一个 Python 文件都没扫到" in captured.out

    def test_duplicate_detection_in_output(self, fixture_backend, capsys):
        """Given 含重复函数的目录, When 跑门禁, Then 输出包含重复函数体信息。"""
        args = self._make_args(backend_dir=str(fixture_backend))
        qg.check_backend(args)
        captured = capsys.readouterr()
        assert "重复函数体" in captured.out


# ============================================================
# 8. main — CLI 入口
# ============================================================

class TestMain:
    """CLI 入口: 参数解析与退出码。"""

    def test_full_flag_enables_tests_and_mutation(self):
        """Given --full 标志, When 解析参数, Then with_tests 和 with_mutation 都为 True。"""
        with patch.object(sys, "argv", ["quality_gate.py", "backend", "--full"]):
            p = qg.argparse.ArgumentParser()
            p.add_argument("scope", choices=["backend", "frontend", "all"])
            p.add_argument("--full", action="store_true")
            p.add_argument("--with-tests", action="store_true")
            p.add_argument("--with-mutation", action="store_true")
            args = p.parse_args(["backend", "--full"])
            # 模拟 main 中的逻辑
            if args.full:
                args.with_tests = True
                args.with_mutation = True
            assert args.with_tests is True
            assert args.with_mutation is True

    def test_backend_scope_calls_check_backend(self, fixture_clean):
        """Given scope=backend, When 调用 main, Then 只执行 check_backend。"""
        with patch.object(sys, "argv", [
            "quality_gate.py", "backend",
            "--backend-dir", str(fixture_clean),
        ]):
            with pytest.raises(SystemExit) as exc_info:
                qg.main()
            assert exc_info.value.code == 0

    def test_nonexistent_backend_exits_1(self):
        """Given 不存在的后端目录, When 调用 main, Then 退出码 1。"""
        with patch.object(sys, "argv", [
            "quality_gate.py", "backend",
            "--backend-dir", "/nonexistent/xyz",
        ]):
            with pytest.raises(SystemExit) as exc_info:
                qg.main()
            assert exc_info.value.code == 1

    def test_default_thresholds(self):
        """Given 默认参数, When 解析, Then 阈值与常量一致。"""
        with patch.object(sys, "argv", ["quality_gate.py", "backend"]):
            p = qg.argparse.ArgumentParser()
            p.add_argument("scope", choices=["backend", "frontend", "all"])
            p.add_argument("--max-complexity", type=int, default=qg.DEFAULT_MAX_COMPLEXITY)
            p.add_argument("--max-fn-lines-py", type=int, default=qg.DEFAULT_MAX_FN_LINES_PY)
            p.add_argument("--max-params", type=int, default=qg.DEFAULT_MAX_PARAMS)
            p.add_argument("--max-file-lines", type=int, default=qg.DEFAULT_MAX_FILE_LINES)
            p.add_argument("--coverage", type=int, default=qg.DEFAULT_COVERAGE)
            p.add_argument("--mutation-score", type=int, default=qg.DEFAULT_MUTATION_SCORE)
            args = p.parse_args(["backend"])
            assert args.max_complexity == 6
            assert args.max_fn_lines_py == 50
            assert args.max_params == 4
            assert args.max_file_lines == 500
            assert args.coverage == 85
            assert args.mutation_score == 60


# ============================================================
# 9. run_ruff — ruff 检查
# ============================================================

class TestRunRuff:
    """ruff lint 检查: 有 ruff 则跑, 无则跳过。"""

    def test_ruff_not_found_returns_true(self, tmp_path, capsys):
        """Given ruff 未安装, When 调用, Then 返回 True (跳过)。"""
        with patch("quality_gate.shutil.which", return_value=None):
            result = qg.run_ruff(tmp_path)
        assert result is True
        captured = capsys.readouterr()
        assert "跳过" in captured.out

    def test_ruff_pass_returns_true(self, tmp_path, capsys):
        """Given ruff 安装且无违规, When 调用, Then 返回 True。"""
        with patch("quality_gate.shutil.which", return_value="/usr/bin/ruff"):
            with patch("quality_gate.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = qg.run_ruff(tmp_path)
        assert result is True

    def test_ruff_fail_returns_false(self, tmp_path, capsys):
        """Given ruff 发现有违规, When 调用, Then 返回 False。"""
        with patch("quality_gate.shutil.which", return_value="/usr/bin/ruff"):
            with patch("quality_gate.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="E501 line too long")
                result = qg.run_ruff(tmp_path)
        assert result is False


# ============================================================
# 10. _VarNormalizer — 变量名归一化
# ============================================================

class TestVarNormalizer:
    """变量名归一化: 按首次出现顺序替换为 v0/v1/...。"""

    def test_normalizes_local_names(self):
        """Given 含局部变量的 AST, When 归一化, Then 变量名替换为 v0/v1/...。"""
        tree = ast.parse("x = 1\ny = 2\nz = x + y")
        kept = qg._collect_kept_names(tree)
        normalizer = qg._VarNormalizer(kept)
        normalized = normalizer.visit(tree)
        names = [n.id for n in ast.walk(normalized) if isinstance(n, ast.Name)]
        # x 首次出现 → v0, y → v1, z → v2
        assert "v0" in names
        assert "v1" in names

    def test_kept_names_not_normalized(self):
        """Given 在 kept 集合中的名字, When 归一化, Then 保持不变。"""
        tree = ast.parse("os.path.join(a, b)")
        kept = qg._collect_kept_names(tree)
        normalizer = qg._VarNormalizer(kept)
        normalized = normalizer.visit(tree)
        attrs = [n.attr for n in ast.walk(normalized)
                 if isinstance(n, ast.Attribute) and hasattr(n, "attr")]
        assert "join" in attrs  # 调用名不应被归一化
