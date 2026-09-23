"""规模边界（扩展验证）：≥1000 堆对象、200 函数版本、≥500 显式调用帧，
且运行态不依赖 Python 递归栈（在被刻意压低的 recursionlimit 下运行）。
"""

import sys
import unittest

from recenv import Machine, build_bundle, convert
from recenv import machine as machine_mod


class TestScale(unittest.TestCase):
    def test_500_plus_frames_without_python_stack(self):
        from recenv import ast_nodes as A

        body = A.If0(
            A.Var("n"), A.Int(0),
            A.Add(A.Var("n"),
                  A.Call(A.Var("sumdown"),
                         [A.Add(A.Var("n"), A.Int(-1))])),
        )
        e = A.LetRec(
            [A.RecBinding("sumdown", A.Lambda(["n"], body))],
            A.Call(A.Var("sumdown"), [A.Int(600)]),
        )
        pr = convert(e)

        peak = [0]
        orig = machine_mod.Machine._enter_call

        def spy(self, callee, args, *, bottom, side_ctx=None):
            orig(self, callee, args, bottom=bottom, side_ctx=side_ctx)
            if self.depth > peak[0]:
                peak[0] = self.depth

        machine_mod.Machine._enter_call = spy
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(300)  # 远低于 600：机器不能靠 Python 栈
        try:
            m = Machine(pr, step_limit=1_000_000, frame_limit=10_000)
            while m.run_segment(377) == "paused":
                pass
        finally:
            sys.setrecursionlimit(old_limit)
            machine_mod.Machine._enter_call = orig
        self.assertEqual(m.result, 600 * 601 // 2)
        self.assertGreaterEqual(peak[0], 600)

    def test_1000_plus_heap_objects(self):
        from recenv import ast_nodes as A

        # 每层递归：激活记录 + let 追加帧 + cell/闭包对象
        body = A.If0(
            A.Var("n"), A.Int(0),
            A.Let("t", A.Var("n"),
                  A.Call(A.Var("loop"),
                         [A.Add(A.Var("n"), A.Int(-1))])),
        )
        e = A.LetRec(
            [A.RecBinding("loop", A.Lambda(["n"], body))],
            A.Call(A.Var("loop"), [A.Int(550)]),
        )
        m = Machine(convert(e), step_limit=2_000_000, frame_limit=10_000)
        v, _ = m.run_to_completion()
        self.assertEqual(v, 0)
        self.assertGreaterEqual(len(m.heap), 1000)

    def test_200_function_versions(self):
        from recenv import ast_nodes as A

        def bump(ver):
            b = A.Lambda(
                ["x"],
                A.Set("k", A.Add(A.Var("k"),
                                 A.Add(A.Var("x"), A.Int(ver)))),
                ver=f"b@{ver}",
            )
            return A.Let(
                "k", A.Int(0),
                A.Let("b", b, A.Call(A.Var("b"), [A.Int(1)]))
            )

        bundle = build_bundle(convert(bump(1)))
        for ver in range(2, 201):
            bundle.import_program(convert(bump(ver)), f"v{ver}:")
        self.assertEqual(bundle.versions("b"), list(range(1, 201)))
        self.assertGreaterEqual(len(bundle.entries()), 200)
        # 内容哈希两两不同（代码字面量不同）
        hashes = {e.code_hash for e in bundle.entries() if e.unit == "b"}
        self.assertEqual(len(hashes), 200)

    def test_deep_run_completes_under_tight_python_stack(self):
        # 直接用低 recursionlimit 跑完整机器，确认无 Python 递归参与求值。
        from recenv import ast_nodes as A

        body = A.If0(
            A.Var("n"), A.Int(0),
            A.Add(A.Int(1),
                  A.Call(A.Var("f"), [A.Add(A.Var("n"), A.Int(-1))])),
        )
        e = A.LetRec(
            [A.RecBinding("f", A.Lambda(["n"], body))],
            A.Call(A.Var("f"), [A.Int(520)]),
        )
        old = sys.getrecursionlimit()
        sys.setrecursionlimit(200)
        try:
            m = Machine(convert(e), step_limit=1_000_000, frame_limit=10_000)
            v, _ = m.run_to_completion()
        finally:
            sys.setrecursionlimit(old)
        self.assertEqual(v, 520)


if __name__ == "__main__":
    unittest.main()
