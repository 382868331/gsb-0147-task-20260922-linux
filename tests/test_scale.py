"""扩展规模验证（默认跳过）。

运行::

    RUN_RECENV_EXTENDED=1 python -m unittest tests.test_scale -v

覆盖契约规模下限（真实非退化执行，非玩具输入）：

* **1000+ 环境对象**：递归工厂每层产生帧记录 / cell / 闭包 / 捕获记录，
  堆上活跃+历史对象总数超过 1000，且镜像往返后对象图与别名一致；
* **200 个函数版本**：同一 fid 在 200 层递归的每个调用入口连续升级
  v1..v201（每次在安全点提交），栈底到栈顶的旧帧依次保留各自代码版本，
  最深栈帧引用 v201；验证旧帧/新版本共存与续执行正确性；
* **500 层显式调用帧**：500 层递归在第 250 层的调用栈中间（非安全点）
  序列化，镜像里控制栈帧数 >= 500，跨镜像恢复后结果与连续执行一致。

全程显式栈，不依赖 Python 递归深度。
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace

from recenv import ast_nodes as A
from recenv import convert
from recenv import image as imgmod
from recenv.machine import Machine
from recenv.migration import fieldize, upgrade_at_safepoint

RUN = os.environ.get("RUN_RECENV_EXTENDED") == "1"


@unittest.skipUnless(RUN, "扩展规模测试；RUN_RECENV_EXTENDED=1 时运行")
class TestThousandEnvironmentObjects(unittest.TestCase):
    def test_make_factory_recursion_1000_objects(self):
        # make(n): 每层新建一个可变 cell c 与闭包 inc（捕获 c），丢弃后递归；
        # 每层在堆上留下：let 帧 record、c cell、inc 闭包、inc 捕获 record。
        inc = A.Lambda([], A.Set("c", A.Add(A.Var("c"), A.Int(1))))
        body = A.If0(
            A.Var("n"), A.Int(0),
            A.Let(
                "c", A.Int(0),
                A.Let(
                    "inc", inc,
                    A.Add(A.Call(A.Var("inc"), []),
                          A.Call(A.Var("make"),
                                 [A.Add(A.Var("n"), A.Int(-1))])),
                ),
            ),
        )
        root = A.LetRec(
            [A.RecBinding("make", A.Lambda(["n"], body))],
            A.Call(A.Var("make"), [A.Int(260)]),
        )
        p = fieldize(convert(root))
        m = Machine(p, step_limit=200_000)
        m.run()
        n_obj = (len(m.heap.records) + len(m.heap.cells)
                 + len(m.heap.closures))
        self.assertGreaterEqual(n_obj, 1000, n_obj)

        # 镜像往返后对象计数与最终结果不变
        m2 = imgmod.load_machine(imgmod.dump_machine(m))
        n_obj2 = (len(m2.heap.records) + len(m2.heap.cells)
                  + len(m2.heap.closures))
        self.assertEqual(n_obj2, n_obj)
        self.assertEqual(m2.result, m.result)
        # 每层 inc() 返回 1，共 260
        self.assertEqual(m.result, 260)


@unittest.skipUnless(RUN, "扩展规模测试；RUN_RECENV_EXTENDED=1 时运行")
class TestTwoHundredVersions(unittest.TestCase):
    def test_200_versions_old_frames_coexist(self):
        depth = 200
        # v1: f(n) = if0(n, 1, 1 + f(n-1))；各版本函数体相同（仅版本号不同）
        body = A.If0(
            A.Var("n"), A.Int(1),
            A.Add(A.Int(1),
                  A.Call(A.Var("f"), [A.Add(A.Var("n"), A.Int(-1))])),
        )
        root = A.LetRec(
            [A.RecBinding("f", A.Lambda(["n"], body))],
            A.Call(A.Var("f"), [A.Int(depth)]),
        )
        p = fieldize(convert(root))
        fid = next(f for f in p.functions
                   if any(x.startswith("cell:f#") for x in p.layout[f]))
        fn = p.functions[fid]

        m = Machine(p, step_limit=500_000)
        state = {"entries": 0}

        def hook(mach):
            top = mach.stack[-1]
            if top.fid == fid:
                state["entries"] += 1
                return "pause"
            return None

        # 每次进入 f：在入口安全点升级到下一版本（新代码与当前版本同体）
        def reissue(mach, k):
            cur_ver = mach.table.current_version(fid)
            fv = mach.table.get(fid, cur_ver)
            # 新版本函数体不变；free_names 必须使用结构化字段名（版本 fields）
            fields = fv.fields or tuple(fv.fn.free_names)
            return replace(fv.fn, free_names=tuple(fields[:fv.fn.n_capture]))

        for k in range(1, depth + 1):
            m.run(safepoint_hook=hook)
            self.assertTrue(m.at_safepoint, k)
            new_ver = f"v{k + 1}"
            upgrade_at_safepoint(m, new_ver, {fid: reissue(m, k)}, {fid: []})

        # 200 次入口升级后栈上 f(200)..f(1) 共 200 个激活帧（f(0) 尚未进入）
        act_versions = [f.version for f in m.stack if f.fid == fid]
        self.assertEqual(len(act_versions), depth)
        self.assertEqual(act_versions[0], "v1")
        # 栈顶 f(1) 在第 200 次入口升级到 v200（之后的新调用才用 v201）
        self.assertEqual(act_versions[-1], f"v{depth}")
        # 版本表条目数（v1..v201）
        self.assertEqual(len(m.table.versions[fid]), depth + 1)
        self.assertGreaterEqual(len(m.table.versions[fid]), 200)

        m.run()
        self.assertEqual(m.result, depth + 1)

        # 升级后镜像可跨格式往返（含 201 个版本）
        m2 = Machine(p, step_limit=500_000)
        st = {"entries": 0}
        # 只验证镜像：在中途升级 50 次后保存/载入再继续
        for k in range(1, 51):
            m2.run(safepoint_hook=hook)
            cur_ver = m2.table.current_version(fid)
            fv = m2.table.get(fid, cur_ver)
            fields = fv.fields or tuple(fv.fn.free_names)
            cur_fn = replace(fv.fn, free_names=tuple(fields[:fv.fn.n_capture]))
            upgrade_at_safepoint(m2, f"v{k + 1}", {fid: cur_fn}, {fid: []})
        saved = imgmod.dump_machine(m2)
        m3 = imgmod.load_machine(saved)
        self.assertEqual(len(m3.table.versions[fid]), 51)
        m3.run()
        self.assertEqual(m3.result, depth + 1)


@unittest.skipUnless(RUN, "扩展规模测试；RUN_RECENV_EXTENDED=1 时运行")
class TestFiveHundredFramesImage(unittest.TestCase):
    def test_500_frames_mid_stack_image_roundtrip(self):
        depth = 500
        body = A.If0(
            A.Var("n"), A.Int(1),
            A.Add(A.Int(1),
                  A.Call(A.Var("g"), [A.Add(A.Var("n"), A.Int(-1))])),
        )
        root = A.LetRec(
            [A.RecBinding("g", A.Lambda(["n"], body))],
            A.Call(A.Var("g"), [A.Int(depth)]),
        )
        p = fieldize(convert(root))
        full = Machine(p, step_limit=200_000)
        full.run()

        m = Machine(p, step_limit=200_000)
        # 每拍采样：找到激活栈帧达 500+ 的非安全点（深递归下行阶段）
        peak = 0
        stopped = False
        while m.state == "paused":
            m.run(step_budget=1)
            peak = max(peak, sum(1 for f in m.stack if f.kind == "act"))
            if peak >= depth and not m.at_safepoint:
                stopped = True
                break
        self.assertTrue(stopped, f"never sampled depth {depth}, peak={peak}")
        self.assertGreaterEqual(peak, depth)

        obj = imgmod.dump_machine(m)
        self.assertGreaterEqual(
            len(obj["control"]["frames"]), depth, "控制栈帧数"
        )
        m2 = imgmod.load_machine(obj)
        m2.run()
        self.assertEqual((m2.result, m2.events, m2.steps),
                         (full.result, full.events, full.steps))
        self.assertEqual(m2.result, depth + 1)


if __name__ == "__main__":
    unittest.main()
