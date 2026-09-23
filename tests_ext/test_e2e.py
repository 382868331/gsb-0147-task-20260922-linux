"""端到端：在同一条执行流水线上同时用到三组新能力——

1. 调用栈中间在安全点暂停，保存为快照，在“全新机器”（仅由快照字节重建，
   不共享任何 Python 对象，等价于新进程）恢复；
2. 恢复后在安全点做版本表升级 + 显式字段迁移（rename + add-const），
   旧版本栈帧仍在栈上；
3. 旁路运行迁移后的新闭包修改共享 cell，再次暂停、再次跨快照恢复，
   最后旧栈恢复跑完。

期望值全部由手工小参考域逐式给出，不调用被测执行器反推。
"""

import json
import os
import sys
import unittest

from recenv import Machine, MigrationRules, convert, snapshot
from recenv.heap import ClosureObj, Ref

try:
    from tests_ext import progs
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from tests_ext import progs


def fresh_process(machine: Machine) -> Machine:
    """把机器序列化为 JSON 文本再恢复：丢弃全部 Python 对象身份，
    等价于在新进程中启动（demo.py 用真实子进程额外验证这一点）。"""
    return snapshot.loads(snapshot.dumps(machine))


def pause_at(machine, pred, cap=500):
    machine.start()
    for _ in range(cap):
        machine.run_segment(1)
        if pred(machine.call_trace()):
            return
    raise AssertionError("pause predicate never satisfied")


class TestEndToEndAllThreeCapabilities(unittest.TestCase):
    def test_pause_restore_migrate_pause_restore_finish(self):
        p1 = convert(progs.standalone_v1(3))
        p2 = convert(progs.standalone_v2(2, bias=100))
        rules = MigrationRules(rename={"c": "kount"}, add={"bias": 100})

        # --- 能力一：栈中暂停 → 快照 → 全新机器恢复 --------------------
        m = Machine(p1, step_limit=20_000)
        pause_at(m, lambda t: any(u == "bump" and v == 1 for _, u, v in t))
        bump1_oid = next(
            i for i in m.heap.all_oids()
            if isinstance(m.heap.get(Ref(i)), ClosureObj)
            and m.bundle.get(m.heap.get(Ref(i)).fkey).unit == "bump"
        )
        shared_cell = m.heap.record(m.heap.get(Ref(bump1_oid)).record).slots[0]

        m = fresh_process(m)  # 新进程 #1：仅持快照字节
        self.assertEqual(m.status, "paused")
        self.assertIn(
            ("bump", 1), [(u, v) for _, u, v in m.call_trace()]
        )
        # 新机器堆对象身份由 OID 重建：仍指同一逻辑 cell
        self.assertIsNotNone(m.heap.get(shared_cell))

        # --- 能力二：恢复后在安全点升级 + 字段迁移 ---------------------
        m.request_upgrade("bump", p2, rules)
        handles = m.apply_pending_upgrade()
        self.assertEqual(m.applied_upgrades, [("bump", 1, 2)])
        new_clo = next(
            m.heap.get(Ref(i)) for i in m.heap.all_oids()
            if isinstance(m.heap.get(Ref(i)), ClosureObj)
            and m.bundle.get(m.heap.get(Ref(i)).fkey).unit == "bump"
            and m.bundle.get(m.heap.get(Ref(i)).fkey).version == 2
        )
        nentry = m.bundle.get(new_clo.fkey)
        k_slot = m.heap.record(new_clo.record).slots[
            list(nentry.free_names).index("kount")
        ]
        self.assertEqual(k_slot, shared_cell)  # 别名保持，不是副本

        # 旧栈帧钉在 bump@1
        self.assertIn(("bump", 1), [(u, v) for _, u, v in m.call_trace()])

        # --- 能力三：迁移后的新闭包旁路修改共享 cell -------------------
        m.invoke(handles[0]["$self"], [1], side=True)
        side_val, side_events = m.run_side()
        self.assertEqual((side_val, side_events),
                         (101, [("set", "kount", 101)]))
        self.assertEqual(m.heap.cell(shared_cell).value, 101)

        # 再次栈中暂停（这里机器已自动回到 paused），再跨一次快照恢复
        self.assertEqual(m.status, "paused")
        m = fresh_process(m)  # 新进程 #2
        self.assertEqual(m.heap.cell(shared_cell).value, 101)
        self.assertEqual(m.applied_upgrades, [("bump", 1, 2)])

        # 旧栈恢复跑完
        value, events = m.run_to_completion()
        # 手工小参考（逐式）：
        #   旁路 bump@2(1) 写 0+1+100=101；
        #   旧 bump@1 的 IIFE 在安全点之后读 c=101，写 101+1=102；
        #   drive 余下两次迭代经改线槽进入 bump@2：102+1+100=203、
        #   203+1+100=304；drive 和=102+203+304=609；+末值 304 = 913。
        self.assertEqual(events, [
            ("set", "kount", 101),
            ("set", "c", 102),
            ("set", "kount", 203),
            ("set", "kount", 304),
        ])
        self.assertEqual(value, 913)
        self.assertEqual(m.heap.cell(shared_cell).value, 304)


class TestMutualGroupEndToEnd(unittest.TestCase):
    def test_mutual_group_pause_migrate_resume(self):
        # 互递归组 eo：暂停 → 新机器恢复 → 迁移（rename+add）→ 旧栈跑完。
        p1 = convert(progs.mutual_v1(5))
        p2 = convert(progs.mutual_v2(5, bias=1000))
        m = Machine(p1, step_limit=20_000)
        pause_at(m, lambda t: any(u == "eo" and v == 1 for _, u, v in t))
        m = fresh_process(m)

        def closures(machine, unit, version):
            return [
                machine.heap.get(Ref(i)) for i in machine.heap.all_oids()
                if isinstance(machine.heap.get(Ref(i)), ClosureObj)
                and machine.bundle.get(machine.heap.get(Ref(i)).fkey).unit == unit
                and machine.bundle.get(machine.heap.get(Ref(i)).fkey).version == version
            ]

        ev1 = next(c for c in closures(m, "eo", 1)
                   if m.bundle.get(c.fkey).member == "ev")
        c_cell = m.heap.record(ev1.record).slots[
            list(m.bundle.get(ev1.fkey).free_names).index("c")
        ]

        m.request_upgrade("eo", p2,
                          MigrationRules(rename={"c": "kount"}, add={"bias": 1000}))
        handles = m.apply_pending_upgrade()
        self.assertEqual(set(handles[0]), {"ev", "od"})
        m = fresh_process(m)  # 迁移后也可跨进程保存/恢复
        # 旧 eo@1 帧仍在栈上；继续旧版本跑完（与未升级的小参考一致）
        value, events = m.run_to_completion()
        self.assertEqual(value, 9)
        self.assertEqual(events,
                         [("set", "c", 1), ("set", "c", 2), ("set", "c", 3)])
        # 迁移产生的新闭包仍在堆上，且共享旧 cell
        ev2 = next(
            c for c in (m.heap.get(Ref(i)) for i in m.heap.all_oids())
            if isinstance(c, ClosureObj)
            and m.bundle.get(c.fkey).unit == "eo"
            and m.bundle.get(c.fkey).version == 2
            and m.bundle.get(c.fkey).member == "ev"
        )
        k = m.heap.record(ev2.record).slots[
            list(m.bundle.get(ev2.fkey).free_names).index("kount")
        ]
        self.assertEqual(k, c_cell)


if __name__ == "__main__":
    unittest.main()
