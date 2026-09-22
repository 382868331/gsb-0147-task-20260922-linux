"""差分测试：确定性生成的程序，由独立源解释器给出期望值，
再与目标解释器的返回值、副作用序列、精确步数逐一对照。

源解释器不导入 convert；往返一致在这里是核心验收手段之一，但期望值来源独立。
"""

import unittest

from recenv import convert, errors, eval_source, eval_target
from recenv import source_interp as si
from recenv import target_interp as ti

try:  # 兼容 discover -s tests（tests 在 sys.path 上）与 tests.xxx 包路径两种方式
    from proggen import Generator
except ImportError:  # pragma: no cover - 仅运行方式不同
    from .proggen import Generator


def _step_counts(src, program):
    s = si._SrcInterp(10_000)
    s.eval(src, None)
    t = ti._TargetInterp(program, 10_000)
    t.eval(program.main, ())
    return s.steps, t.steps


class TestDifferential(unittest.TestCase):
    def test_many_plain_programs(self):
        n_ok = 0
        n_skip = 0
        for seed in range(120):
            gen = Generator(1000 + seed)
            program_ast = gen.gen_program(max_nodes=180)
            try:
                s_val, s_ev = eval_source(program_ast, step_limit=10_000)
            except errors.RecenvError:
                # 生成器保证语法/作用域合法；运行期错误（如步数）跳过
                n_skip += 1
                continue
            ir = convert(program_ast)
            t_val, t_ev = eval_target(ir, step_limit=10_000)
            self.assertEqual(
                s_val,
                t_val,
                f"seed {seed}: value mismatch\nAST={program_ast!r}",
            )
            self.assertEqual(
                s_ev,
                t_ev,
                f"seed {seed}: events mismatch\nAST={program_ast!r}",
            )
            ss, ts = _step_counts(program_ast, ir)
            self.assertEqual(
                ss, ts, f"seed {seed}: step count mismatch {ss} != {ts}"
            )
            n_ok += 1
        self.assertGreater(n_ok, 80, f"too few successful runs: {n_ok}, skipped {n_skip}")

    def test_many_letrec_programs(self):
        n_ok = 0
        for seed in range(60):
            gen = Generator(5000 + seed)
            program_ast = gen.gen_letrec_program(max_nodes=220, n_funs=2)
            s_val, s_ev = eval_source(program_ast, step_limit=10_000)
            ir = convert(program_ast)
            t_val, t_ev = eval_target(ir, step_limit=10_000)
            self.assertEqual(s_val, t_val, f"seed {seed} value mismatch")
            self.assertEqual(s_ev, t_ev, f"seed {seed} events mismatch")
            ss, ts = _step_counts(program_ast, ir)
            self.assertEqual(ss, ts, f"seed {seed} step mismatch {ss} != {ts}")
            n_ok += 1
        self.assertEqual(n_ok, 60)

    def test_triple_mutual_recursion(self):
        gen = Generator(7777)
        program_ast = gen.gen_letrec_program(max_nodes=240, n_funs=3)
        s_val, s_ev = eval_source(program_ast, step_limit=10_000)
        ir = convert(program_ast)
        t_val, t_ev = eval_target(ir, step_limit=10_000)
        self.assertEqual((s_val, s_ev), (t_val, t_ev))
        ss, ts = _step_counts(program_ast, ir)
        self.assertEqual(ss, ts)


if __name__ == "__main__":
    unittest.main()
