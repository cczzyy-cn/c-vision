"""跨进程输入互斥（``cvision.input_lock``）的契约测试。

为什么值得单独钉住：这是本插件里**唯一**保证「两个 DSH 实例不会同时驱动同一套鼠标键盘」的东西，
而它失效时**不会当场报错**——表现为两个会话偶尔互相点错窗口，是最难归因的一类故障。所以这里不但测
「拿到锁之后别人拿不到」，还测「锁机制不可用时要如实降级」「超时要如实报错」。

平台无关：Windows 走命名互斥体、macOS/Linux 走 flock，两边都由 stdlib 提供，无需第三方依赖，也不
碰真实的鼠标键盘。真正拿不到任何后端的环境（既非 win 也非 darwin/linux）会跳过需要互斥的用例。
"""

import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest import mock

from cvision import input_lock

#: 在子进程里「占着锁不放」的小脚本：打印 ready（证明真的拿到了锁）再睡指定秒数。
_HOLDER = (
    "import sys, time\n"
    "from cvision import input_lock\n"
    "ctx = input_lock.input_lock(label='holder')\n"
    "ctx.__enter__()\n"
    "print('ready', flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean_env():
    """把「关掉互斥」的环境变量摘掉——否则这个测试文件会被自己的环境静默跳过。"""
    env = dict(os.environ)
    env.pop(input_lock.DISABLE_ENV, None)
    env.pop(input_lock.TIMEOUT_ENV, None)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


@contextmanager
def holding_in_another_process(seconds: float = 3.0):
    """在**另一个进程**里持有输入锁，yield 出来之后杀掉它。

    必须跨进程：同进程内 Windows 互斥体对同一线程是可重入的，同进程测等于什么都没测。
    """
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(seconds)],
        cwd=_REPO_ROOT,
        env=_clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = child.stdout.readline().strip()
        if ready != "ready":
            err = child.stderr.read()
            child.kill()
            raise AssertionError(f"子进程没能拿到锁：stdout={ready!r} stderr={err}")
        yield child
    finally:
        child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            pass
        for pipe in (child.stdout, child.stderr):
            try:
                pipe.close()
            except Exception:  # noqa: BLE001 - 收尾而已
                pass


def _backend_available() -> bool:
    return bool(input_lock.status().get("available"))


class TestStatus(unittest.TestCase):
    def test_status_reports_a_real_backend(self):
        """体检口径与 capture_backends 一致：``available`` 必须是**真探测**的结果。"""
        s = input_lock.status()
        self.assertIn(s["backend"], ("windows-mutex", "posix-flock", "none"))
        self.assertIsInstance(s["available"], bool)
        if s["available"]:
            self.assertEqual(s["reason"], "")
            self.assertNotEqual(s["backend"], "none")
        else:
            self.assertNotEqual(s["reason"], "", "不可用时必须说清原因")

    def test_status_exposes_the_holder_and_the_foreground_budget(self):
        """用户问「为什么卡住」时，status 要能回答：谁在持锁、抓图那条路能等多久。"""
        s = input_lock.status()
        self.assertIn("holder", s)
        self.assertIn("foreground_timeout_s", s)
        self.assertLess(s["foreground_timeout_s"], s["timeout_s"], "抓图那条路必须等得更短")

    def test_status_holder_is_empty_when_nobody_holds_it(self):
        self.assertEqual(input_lock.status()["holder"], {})

    def test_status_is_available_on_supported_platforms(self):
        if not (sys.platform.startswith("win") or sys.platform in ("darwin", "linux")):
            self.skipTest(f"{sys.platform} 上没有实现")
        self.assertTrue(_backend_available(), input_lock.status())

    def test_disabled_by_env_is_reported_not_hidden(self):
        """环境变量关掉互斥后，status 要**如实**说没有这项保证，而不是继续报 available。"""
        with mock.patch.dict(os.environ, {input_lock.DISABLE_ENV: "0"}):
            s = input_lock.status()
        self.assertFalse(s["available"])
        self.assertIn(input_lock.DISABLE_ENV, s["disabled_by"])


class TestHolderLiveness(unittest.TestCase):
    """持有者记录的**存活判定**：记录可能残留自上一个已退出的持锁进程，别把排查带偏。"""

    def test_unknown_pid_shapes_are_not_guessed(self):
        """读不出/非法 pid 一律返回 None（不知道），绝不猜成「已退出」。"""
        for bad in (None, "", "abc", 0, -1):
            self.assertIsNone(input_lock._pid_alive(bad), f"{bad!r} 不该被判定成任何确定结论")

    def test_current_process_is_alive(self):
        self.assertIs(input_lock._pid_alive(os.getpid()), True)

    def test_exited_process_is_reported_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.assertIs(input_lock._pid_alive(child.pid), False)

    def test_holder_reports_alive_flag(self):
        with input_lock.input_lock(timeout_s=2.0, label="holder-liveness"):
            info = input_lock.holder()
        self.assertEqual(info.get("pid"), os.getpid())
        self.assertIs(info.get("alive"), True)
        self.assertEqual(input_lock.holder(), {}, "释放后不该还有持有者")

    def test_timeout_message_flags_a_stale_record(self):
        """指名一个已退出的 pid 时必须说明「记录可能是它留下的」，否则排查会被带偏。"""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        msg = str(input_lock.InputLockTimeout(1.0, {"pid": child.pid, "label": "gone"}))
        self.assertIn("该 pid 已退出", msg)


class TestReentrancy(unittest.TestCase):
    """不可重入：Windows 互斥体是线程递归的，重入会「成功」却不提供任何额外排他性。"""

    def setUp(self):
        if not _backend_available():
            self.skipTest("本机没有可用的跨进程锁后端")

    def test_nested_acquisition_in_the_same_thread_is_rejected(self):
        with input_lock.input_lock(timeout_s=2.0, label="outer"):
            with self.assertRaises(RuntimeError) as ctx:
                with input_lock.input_lock(timeout_s=2.0, label="inner"):
                    self.fail("重入不该成功")
            self.assertIn("不可重入", str(ctx.exception))
            self.assertIn("outer", str(ctx.exception), "报错要说清已经持有的是哪一把")
            # 内层没拿成，也不能把外层的记录/锁弄坏。
            self.assertEqual(input_lock.holder().get("label"), "outer")
        # 退出后必须能重新取（否则一次误用就永久卡死）。
        with input_lock.input_lock(timeout_s=2.0, label="again") as lock:
            self.assertTrue(lock.acquired)


class TestForegroundGuard(unittest.TestCase):
    """抓图侧的惰性守卫：**真的**会动前台时才取，抓完（并还原后）释放。"""

    def setUp(self):
        if not _backend_available():
            self.skipTest("本机没有可用的跨进程锁后端")

    def test_lazy_until_ensure(self):
        guard = input_lock.ForegroundGuard(label="抓取窗口 0x1234")
        self.assertIsNone(guard.state, "没 ensure 之前不该取锁")
        self.assertEqual(input_lock.holder(), {}, "没 ensure 之前不该留下持有者记录")
        guard.ensure("需要还原被最小化的窗口")
        try:
            self.assertTrue(guard.state.acquired)
            self.assertEqual(input_lock.holder().get("label"), "抓取窗口 0x1234")
        finally:
            guard.release()
        self.assertIsNone(guard.state)
        self.assertEqual(input_lock.holder(), {})

    def test_ensure_is_idempotent(self):
        """同一次抓图里可能有多处要动前台，第二次 ensure 必须是空操作（也就不会撞上重入保护）。"""
        guard = input_lock.ForegroundGuard()
        guard.ensure("第一处")
        try:
            guard.ensure("第二处")  # 不抛 = 通过
            self.assertEqual(guard.reason, "第一处", "原因保持第一次的，便于解释为什么取锁")
        finally:
            guard.release()

    def test_release_without_ensure_is_a_noop(self):
        input_lock.ForegroundGuard().release()  # 不抛 = 通过

    def test_timeout_raises_foreground_busy_with_actionable_text(self):
        with holding_in_another_process(3.0):
            guard = input_lock.ForegroundGuard(timeout_s=0.3, label="抓取窗口 0x1234")
            with self.assertRaises(input_lock.ForegroundBusy) as ctx:
                guard.ensure("兜底读屏需要把目标窗口置前")
            message = str(ctx.exception)
        self.assertIn("本次抓取**未执行**", message)
        self.assertIn("兜底读屏需要把目标窗口置前", message, "要说清是哪种抓法触发的")
        self.assertIn("pid=", message, "要点名是谁在操作电脑")
        self.assertIn("不传 maximize", message, "要给一条可执行的替代做法")

    def test_degraded_lock_is_surfaced_not_hidden(self):
        """锁机制不可用时守卫要**降级放行**，但把原因留在 state 里（抓图不至于整个不可用）。"""
        with mock.patch.object(input_lock, "_make_backend", return_value=(None, "CreateMutexW 失败")):
            guard = input_lock.ForegroundGuard(timeout_s=0.1)
            guard.ensure("需要最大化窗口")
            try:
                self.assertFalse(guard.state.acquired)
                self.assertIn("CreateMutexW", guard.state.degraded)
            finally:
                guard.release()


class TestTimeoutResolution(unittest.TestCase):
    def test_explicit_wins_over_env(self):
        with mock.patch.dict(os.environ, {input_lock.TIMEOUT_ENV: "99"}):
            self.assertEqual(input_lock._resolve_timeout(0.5), 0.5)

    def test_env_wins_over_default(self):
        with mock.patch.dict(os.environ, {input_lock.TIMEOUT_ENV: "3.5"}):
            self.assertEqual(input_lock._resolve_timeout(None), 3.5)

    def test_bad_env_falls_back_to_default(self):
        """环境变量写错不该让输入类工具整个炸掉——退回默认值，行为可预期。"""
        with mock.patch.dict(os.environ, {input_lock.TIMEOUT_ENV: "abc"}):
            self.assertEqual(input_lock._resolve_timeout(None), input_lock.DEFAULT_TIMEOUT_S)


class TestExclusion(unittest.TestCase):
    def setUp(self):
        if not _backend_available():
            self.skipTest("本机没有可用的跨进程锁后端")

    def test_other_process_blocks_and_timeout_is_reported(self):
        """核心契约：别的进程持锁时**拿不到**，且超时要如实说清「是谁占着、等了多久」。"""
        with holding_in_another_process(3.0):
            with self.assertRaises(input_lock.InputLockTimeout) as ctx:
                with input_lock.input_lock(timeout_s=0.3, label="本测试"):
                    self.fail("不该拿到锁")
        message = str(ctx.exception)
        self.assertIn(input_lock.DEFAULT_NAME, message)
        self.assertIn("0.3", message)
        self.assertIn("pid=", message, "超时消息要点名持有者，否则调用方无从判断该等谁")
        self.assertIn("holder", message, "持有者记录里的 label 要带出来")

    def test_lock_is_released_after_the_holder_exits(self):
        """持有者结束（哪怕是被 kill）之后必须能立刻拿到——否则一次崩溃会永久卡住输入。"""
        with holding_in_another_process(30.0) as child:
            child.kill()
            child.wait(timeout=5)
        with input_lock.input_lock(timeout_s=2.0) as lock:
            self.assertTrue(lock.acquired)
            self.assertEqual(lock.backend, input_lock.status()["backend"])

    def test_owner_note_is_written_while_held_and_cleared_after(self):
        """持锁期间要留下「谁在持锁」，释放后要清掉——超时的调用方全靠它。"""
        with input_lock.input_lock(timeout_s=2.0, label="label-for-owner-test"):
            owner = input_lock._read_owner()
            self.assertEqual(owner.get("pid"), os.getpid())
            self.assertEqual(owner.get("label"), "label-for-owner-test")
        self.assertEqual(input_lock._read_owner(), {}, "释放后不该留下过期的持有者记录")

    def test_enabled_false_does_not_block_even_when_contended(self):
        """``--no-lock`` 的语义：明确跳过互斥（排错用），但必须**如实标注**跳过了。"""
        with holding_in_another_process(3.0):
            with input_lock.input_lock(enabled=False, label="跳过") as lock:
                self.assertFalse(lock.acquired)
                self.assertIn("--no-lock", lock.skipped)
                self.assertEqual(lock.note(), {"skipped": "调用方指定 --no-lock"})

    def test_env_can_disable_the_lock(self):
        with mock.patch.dict(os.environ, {input_lock.DISABLE_ENV: "false"}):
            with input_lock.input_lock() as lock:
                self.assertFalse(lock.acquired)
                self.assertIn(input_lock.DISABLE_ENV, lock.skipped)


class TestDegrade(unittest.TestCase):
    """锁机制本身不可用时的行为：**降级放行但如实标注**，不静默、也不把工具整个打死。"""

    def test_backend_failure_degrades_instead_of_raising(self):
        with mock.patch.object(input_lock, "_make_backend", return_value=(None, "CreateMutexW 失败")):
            with input_lock.input_lock(timeout_s=0.1) as lock:
                self.assertFalse(lock.acquired)
                self.assertIn("CreateMutexW", lock.degraded)
                self.assertEqual(lock.note(), {"degraded": "CreateMutexW 失败"})

    def test_acquire_error_is_not_swallowed(self):
        """建得出后端但**取锁过程本身**出错（不是超时）时必须抛出来——那是 bug，不是环境问题。"""

        class Boom:
            backend = "boom"

            def acquire(self, timeout_s):
                raise OSError("WaitForSingleObject 返回 0x1f")

            def release(self):  # pragma: no cover - 不该走到
                pass

            def close(self):
                pass

        with mock.patch.object(input_lock, "_make_backend", return_value=(Boom(), "")):
            with self.assertRaises(OSError):
                with input_lock.input_lock(timeout_s=0.1):
                    self.fail("不该进入临界区")


class TestNote(unittest.TestCase):
    def test_normal_case_adds_nothing(self):
        """一切正常时 note() 必须是空的：调用方的 JSON 契约不该被这把锁改形状。"""
        self.assertEqual(input_lock.LockState(backend="x", acquired=True, waited_s=0.01).note(), {})

    def test_waited_and_abandoned_are_reported_when_they_happened(self):
        """真等过/接手过崩溃进程的锁，就值得告诉调用方（这解释了「为什么这次慢了」）。"""
        note = input_lock.LockState(
            backend="x", acquired=True, waited_s=0.42, abandoned=True
        ).note()
        self.assertEqual(note, {"abandoned": True, "waited_s": 0.42})

    def test_timeout_message_reads_well_without_owner_info(self):
        msg = str(input_lock.InputLockTimeout(2.0, {}))
        self.assertIn("未能读到持有者信息", msg)
        self.assertIn("--lock-timeout", msg)

    def test_timeout_message_names_the_owner(self):
        msg = str(input_lock.InputLockTimeout(1.0, {"pid": 4242, "label": "click(1,2)"}))
        self.assertIn("pid=4242", msg)
        self.assertIn("click(1,2)", msg)


if __name__ == "__main__":
    unittest.main()
