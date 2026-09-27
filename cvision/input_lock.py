"""跨进程「输入互斥」：把「同一时刻只有一个进程在驱动鼠标键盘」从口头约定变成真的。

为什么需要：插件自己在**一个进程内**是串行的——工具调用有 DSH 的 exclusive 屏障、常驻 server 是
单请求队列。但**跨进程**完全没有任何约束：两个 DSH 实例、另一个进程里的子代理、或者用户自己的
脚本，各自 spawn 一个 ``cli_input`` 就在同一套鼠标键盘上并发下发。这时「先置前、再点击」会互相
插队：A 把目标窗口置前后，B 又把它自己的窗口置前，A 的点击就落在 B 的窗口上——**坐标全对，错的是
窗口**，这是最难自查的一类失败。

做法（不引入守护进程、不落状态机）：
- **Windows**：命名互斥体（``CreateMutexW`` + ``WaitForSingleObject``）。它由内核持有，持有者被
  kill / 崩溃时**自动释放**（下一个人拿到的是 ``WAIT_ABANDONED``）——这正是「抢互斥锁一旦没释放
  就把插件卡死」的解药，所以这里敢用真互斥。
- **macOS / Linux**：``fcntl.flock`` 锁一个临时文件；flock 同样随进程结束自动释放。

刻意的取舍（每条都有理由，别顺手改掉）：
- 用**会话内**命名空间（不加 ``Global\\`` 前缀）：``Global\\`` 需要 ``SeCreateGlobalPrivilege``，
  普通交互进程拿不到；而需要互斥的是**同一个交互桌面**上的进程，它们本来就在同一个会话里。
- 拿不到锁**不无限等**：超时即抛 :class:`InputLockTimeout`（如实报错），由调用方决定重试还是放弃。
  内核互斥体不保证 FIFO，改成「一直排队」只会让人先在宿主侧超时，白等一场。
- 锁机制本身不可用（异常环境）时**降级放行**，但把原因记在 :class:`LockState` 与 :func:`status`
  里——静默失去互斥保证比报错更危险，可它也不该把「只是想点一下」的用户整个打死。

**锁的边界：一个资源 = 一套鼠标键盘 + 前台窗口 + Z 序**

这三样是耦合的（一次点击会改前台，改前台又会让另一次纯键盘输入打错窗口），所以按**资源**划边界，
而不是按「哪个工具动了输入」：

| 动作 | 取锁 | 为什么 |
| --- | --- | --- |
| 输入类（click/drag/scroll/type/keys/focus/剪贴板写与读） | 是（:data:`DEFAULT_TIMEOUT_S`） | 直接驱动设备、改前台；读剪贴板也算，因为非 ASCII 输入会**临时改写**剪贴板 |
| 抓图且**会**改前台/窗口状态（``maximize=True``、还原最小化窗口、兜底读屏） | 是（:data:`FOREGROUND_TIMEOUT_S`，短） | 与输入抢的是同一个前台/Z 序；它正好能插进「已校验坐标、还没点下去」的那个窗口期 |
| 抓图但不改状态（WGC / PrintWindow 成功、整屏抓取） | 否 | 纯只读，锁进去只会平白阻塞 |
| OCR / list_windows / screen_info / status / wait_changed / wait_stable | 否 | 只读；``wait_*`` 还会在 Python 侧阻塞 10–45s，锁进去等于把整个桌面串行化 |
| ``cli_snip``（系统截图 UI） | 否 | 人工交互，属「用户 vs agent」；这类冲突的一贯策略是**如实提示 busy**，不是抢锁 |

纪律（将来要给别的动作加锁时请遵守）：
1. 只有一把锁时不必操心顺序；一旦要加第二把，**必须固定获取顺序并写进这份文档**，或者干脆复用同一把。
2. **绝不在持锁期间等待用户输入或回调宿主**：持有者卡住会把所有人挡在门外（虽然进程崩溃会自动释放）。
3. **不可重入**：Windows 互斥体是线程递归的、flock 同 fd 也可重入，重入会「成功」却**不提供任何额外
   排他性**。所以本模块主动报错（见 :func:`_claim_reentry`），而不是放任一个悄悄失效的假设。

用法::

    from cvision import input_lock

    with input_lock.input_lock(label="click(400,300)") as lock:
        if lock.degraded:
            ...  # 如实告知调用方：这次没有跨进程保证
        do_the_click()

抓图侧用「惰性守卫」——真的会动前台时才取：

    guard = input_lock.ForegroundGuard(label="抓取窗口 0x1234")
    guard.ensure("需要还原被最小化的窗口")
    ...
    guard.release()
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

#: 锁对象名（Windows 互斥体名 / 临时文件名的主体）。
DEFAULT_NAME = "cvision-input"

#: 输入类动作的等待上限（秒）。宿主侧输入类工具的超时是 30s，这里必须**明显**小于它，
#: 好给动作本身留下时间（``锁等待 + 动作 < 工具超时``）。这条关系由 JS 测试
#: 「输入锁的等待上限必须给动作留出足够时间」机械校验，别只靠记性。
DEFAULT_TIMEOUT_S = 10.0

#: 抓图侧（会改前台/窗口状态时）的等待上限（秒）：比输入短得多。它只是为了避免「抓图把对方正在
#: 校准的前台搅乱」，不值得让一次截图等 10s；拿不到就尽快如实失败。
FOREGROUND_TIMEOUT_S = 2.0

#: 调长/调短等待上限的环境变量（``--lock-timeout`` 优先）。
TIMEOUT_ENV = "CVISION_INPUT_LOCK_TIMEOUT"

#: 置为 0/false/no/off 时**关掉**互斥（排错用）。调用方的 ``--no-lock`` 同义。
DISABLE_ENV = "CVISION_INPUT_LOCK"

#: POSIX 下非阻塞重试的间隔。
_POLL_S = 0.05


class InputLockTimeout(RuntimeError):
    """在超时内没拿到输入锁——**如实报错**，不排到天荒地老。"""

    def __init__(self, timeout_s: float, owner: dict | None = None):
        self.timeout_s = timeout_s
        self.owner = dict(owner or {})
        super().__init__(_timeout_message(timeout_s, self.owner))


class ForegroundBusy(RuntimeError):
    """抓图需要临时改前台/窗口状态，但另一个进程正在驱动输入设备。

    与 :class:`InputLockTimeout` 分开，因为「我拿不到锁」和「我这次抓图要动前台」是两件事，
    调用方（``capture/windows.py``）要给出针对抓图的解释，而不是复用输入动作那段文案。
    """

    def __init__(self, reason: str, timeout_s: float, owner: dict | None = None):
        self.reason = reason
        self.timeout_s = timeout_s
        self.owner = dict(owner or {})
        super().__init__(_foreground_busy_message(reason, timeout_s, self.owner))


def _describe_owner(owner: dict) -> str:
    """把持有者信息写成一句人话；读不到就如实说读不到，不编。"""
    pid = owner.get("pid")
    if not pid:
        return "另一个进程（未能读到持有者信息）"
    parts = [f"pid={pid}"]
    since = owner.get("since")
    if since:
        try:
            parts.append("自 " + time.strftime("%H:%M:%S", time.localtime(float(since))) + " 起")
        except (TypeError, ValueError, OSError):
            pass
    label = str(owner.get("label") or "").strip()
    if label:
        parts.append(f"执行 {label}")
    # 记录可能是**上一个**持锁进程留下的（它刚崩溃、而新持有者还没来得及写下自己的记录）。
    # 这时点名一个已退出的 pid 会把排查带偏，所以查一下存活再决定要不要说明。
    if _pid_alive(pid) is False:
        parts.append("该 pid 已退出，这条记录可能是它留下的")
    return "另一个进程（" + "，".join(parts) + "）"


def _timeout_message(timeout_s: float, owner: dict) -> str:
    return (
        f"{_describe_owner(owner)}正持有输入锁（{DEFAULT_NAME}）；已等待 {timeout_s:g}s 仍未取得，"
        "本次输入**未执行**——两个进程同时驱动同一套鼠标键盘会互相插队（先 `see` 的窗口被对方换掉，"
        "点击就落到对方的窗口上）。"
        f"通常是另一个 DSH 会话/实例正在操作电脑：等它做完再试，或用环境变量 {TIMEOUT_ENV}"
        "（或 CLI 的 --lock-timeout）调长等待时间。确实要跳过互斥（仅供排错）用 --no-lock。"
    )


def _foreground_busy_message(reason: str, timeout_s: float, owner: dict) -> str:
    return (
        f"抓图需要临时改动前台/窗口状态（{reason}），但{_describe_owner(owner)}正持有输入锁"
        f"（{DEFAULT_NAME}）；已等待 {timeout_s:g}s，本次抓取**未执行**。"
        "此刻去置前/还原会把对方正在校准的前台搅乱：它算好的坐标会落到别的窗口上，我们抓到的画面与"
        "元素坐标也会一起错（画面是遮挡者的、坐标还是目标窗口的）。稍后重试，或改用不需要置前的抓法"
        "（不传 maximize、或换一个 WGC/PrintWindow 抓得下来的窗口）。"
    )


def _temp_dir() -> str:
    return tempfile.gettempdir()


def _lock_path() -> str:
    return os.path.join(_temp_dir(), f"{DEFAULT_NAME}.lock")


def _owner_path() -> str:
    return os.path.join(_temp_dir(), f"{DEFAULT_NAME}.owner.json")


def _write_owner(label: str, backend: str) -> None:
    """记下「谁在持锁」。纯诊断用途，因此**尽力而为**：写不进去也不影响互斥本身。"""
    try:
        payload = {"pid": os.getpid(), "since": time.time(), "label": str(label or ""), "backend": backend}
        with open(_owner_path(), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:  # noqa: BLE001 - 诊断信息，失败了不该影响动作
        pass


def _clear_owner() -> None:
    try:
        os.unlink(_owner_path())
    except Exception:  # noqa: BLE001 - 可能已被清理/从未写下
        pass


def _read_owner() -> dict:
    try:
        with open(_owner_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 读不到就当没有（超时消息会如实说读不到）
        return {}


def _pid_alive(pid) -> bool | None:
    """那个 pid 还在不在；**判断不了时返回 None**（绝不把「不知道」说成「已退出」）。

    为什么要它：持有者记录是文件，可能残留自上一个**已经退出**的持锁进程（新持有者刚拿到锁、
    还没来得及写下自己的记录）。这时点名一个已退出的 pid 会把排查带偏，所以要如实区分三种情况。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None  # 已退出、或只是权限不够——分不清就别说
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 探测失败＝不知道
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在，只是不归我们管
    except OSError:
        return None
    return True


def holder() -> dict:
    """当前锁的持有者记录（读不到就是空 dict），附一个 ``alive``：pid 还在不在。

    ``alive`` 为 None 表示**判断不了**（权限/平台），调用方要如实呈现，别当成「已退出」。
    """
    owner = _read_owner()
    if not owner:
        return {}
    return {**owner, "alive": _pid_alive(owner.get("pid"))}


class _WindowsMutex:
    """Windows 命名互斥体。内核对象，持有者消失时自动释放。"""

    backend = "windows-mutex"

    def __init__(self, name: str):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32
        # bInitialOwner=False：只创建/打开，所有权靠下面的 WaitForSingleObject 去取。
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), f"CreateMutexW({name}) 失败")
        self._handle = handle
        self.name = name

    def acquire(self, timeout_s: float) -> str:
        ms = int(max(0.0, float(timeout_s)) * 1000)
        rc = self._kernel32.WaitForSingleObject(self._handle, ms)
        if rc == 0x0:
            return "acquired"
        # WAIT_ABANDONED：上一个持有者没释放就死了，内核把所有权交给了我们——按「拿到」处理，
        # 否则一个崩溃的进程会永久挡住之后的每一次输入。
        if rc == 0x80:
            return "abandoned"
        if rc == 0x102:
            return "timeout"
        raise OSError(self._ctypes.get_last_error(), f"WaitForSingleObject 返回 0x{rc:x}")

    def release(self) -> None:
        try:
            self._kernel32.ReleaseMutex(self._handle)
        except Exception:  # noqa: BLE001 - 释放失败也要继续走 close
            pass

    def close(self) -> None:
        try:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None


class _PosixFlock:
    """macOS / Linux：flock 一个临时文件。随进程结束自动释放。"""

    backend = "posix-flock"

    def __init__(self, path: str):
        import fcntl

        self._fcntl = fcntl
        self.name = path
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)

    def acquire(self, timeout_s: float) -> str:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                self._fcntl.flock(self._fd, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                return "acquired"
            except OSError:
                if time.monotonic() >= deadline:
                    return "timeout"
                time.sleep(_POLL_S)

    def release(self) -> None:
        try:
            self._fcntl.flock(self._fd, self._fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            os.close(self._fd)
        except Exception:  # noqa: BLE001
            pass


def _make_backend(name: str):
    """建一个锁对象；返回 ``(backend, reason)``，建不出来时 backend 为 None 并给出原因。"""
    if sys.platform.startswith("win"):
        try:
            return _WindowsMutex(name), ""
        except Exception as e:  # noqa: BLE001 - 建不出来就降级，原因如实上报
            return None, f"{type(e).__name__}: {e}"
    if sys.platform in ("darwin", "linux"):
        try:
            return _PosixFlock(_lock_path()), ""
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
    return None, f"{sys.platform} 上还没有实现跨进程输入互斥"


def _resolve_timeout(timeout_s: float | None = None) -> float:
    """等待上限：显式参数 > 环境变量 > 默认值。"""
    if timeout_s is not None:
        try:
            return max(0.0, float(timeout_s))
        except (TypeError, ValueError):
            pass
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_S


def _disabled_reason(enabled: bool) -> str:
    """互斥被关掉的原因；没关就返回空串。"""
    if not enabled:
        return "调用方指定 --no-lock"
    raw = os.environ.get(DISABLE_ENV, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return f"环境变量 {DISABLE_ENV}={raw}"
    return ""


#: 重入标记按**线程**记：Windows 互斥体（与 flock 的同一 fd）都是按线程/描述符可重入的，所以
#: 「同一线程里又取一次」才是那个会静默失效的形状；不同线程本来就该老实阻塞在系统锁上。
_reentry = threading.local()


def _claim_reentry(name: str, label: str = "") -> None:
    """标记本线程已持有锁；重入直接报错，而不是放任一个悄悄失效的假设。

    记录里带上 label（在干什么），这样报错能说清「已经持有的那把正在做什么」，而不是只报一个锁名。
    """
    held = getattr(_reentry, "held", "")
    if held:
        raise RuntimeError(
            f"输入锁不可重入：本线程已持有 {held}，又试图再取 {name}。"
            "Windows 互斥体是线程递归的、flock 同 fd 也可重入——重入会「成功」，但**不提供任何"
            "额外的排他性**，继续下去只会让人以为自己在排他。要串起两件事，请把它们放进**同一个**"
            "input_lock 临界区。"
        )
    _reentry.held = f"{name}（执行 {label}）" if label else name


def _release_reentry() -> None:
    _reentry.held = ""


@dataclass
class LockState:
    """一次取锁的实况——存在的意义是让调用方**如实回报**，而不只是拿到一个 bool。"""

    backend: str = "none"
    acquired: bool = False
    waited_s: float = 0.0
    abandoned: bool = False
    #: 非空 = 没有跨进程保证：锁机制本身不可用（降级放行）
    degraded: str = ""
    #: 非空 = 调用方主动关掉了互斥（--no-lock / 环境变量）
    skipped: str = ""

    def note(self) -> dict:
        """给调用方塞进返回 JSON 的一小段说明；**一切正常时是空的**（不污染既有契约）。"""
        if self.skipped:
            return {"skipped": self.skipped}
        if self.degraded:
            return {"degraded": self.degraded}
        out: dict = {}
        if self.abandoned:
            out["abandoned"] = True
        if self.waited_s >= 0.2:  # 等过才值得说；没等过就别给调用方添噪音
            out["waited_s"] = round(self.waited_s, 3)
        return out


@contextmanager
def input_lock(*, timeout_s: float | None = None, enabled: bool = True, name: str = DEFAULT_NAME, label: str = ""):
    """取「驱动输入设备」的跨进程锁；超时抛 :class:`InputLockTimeout`。

    :param timeout_s: 等待上限（秒）；None → 环境变量 → :data:`DEFAULT_TIMEOUT_S`。
    :param enabled: False 时**不取锁**（--no-lock），并在 :class:`LockState` 里如实标注。
    :param name: 锁对象名（一般不用改）。
    :param label: 写进持有者记录的「在干什么」，超时的调用方能看见是谁占着。
    :yields: :class:`LockState`。
    :raises RuntimeError: 本线程重入（见 :func:`_claim_reentry`）。
    """
    skipped = _disabled_reason(enabled)
    if skipped:
        yield LockState(skipped=skipped)
        return

    timeout = _resolve_timeout(timeout_s)
    _claim_reentry(name, label)
    try:
        backend, reason = _make_backend(name)
        if backend is None:
            yield LockState(degraded=reason)
            return

        started = time.monotonic()
        try:
            outcome = backend.acquire(timeout)
            waited = time.monotonic() - started
            if outcome == "timeout":
                raise InputLockTimeout(timeout, _read_owner())
            _write_owner(label, backend.backend)
            try:
                yield LockState(
                    backend=backend.backend,
                    acquired=True,
                    waited_s=waited,
                    abandoned=(outcome == "abandoned"),
                )
            finally:
                _clear_owner()
                backend.release()
        finally:
            backend.close()
    finally:
        _release_reentry()


class ForegroundGuard:
    """抓图侧的**惰性**取锁：真的会改前台/窗口状态时才取，抓完（并还原后）释放。

    为什么惰性：绝大多数抓取（WGC / PrintWindow 成功、整屏抓取）是纯只读的，不该为了它们去抢输入锁
    ——那会平白让「另一个会话正在打字」时连截图都做不了。而**确实会**置前/还原的那几条分支必须取锁：
    它们和输入抢的是同一个前台/Z 序，而且正好能插进对方「已校验坐标、还没点下去」的那个窗口期。

    用法见模块文档；典型是 ``ensure()`` 在动前台之前调用、``release()`` 放在 ``finally`` 里
    （且**必须在把窗口状态还原之后**再释放）。
    """

    def __init__(self, *, timeout_s: float | None = None, label: str = ""):
        self._timeout_s = FOREGROUND_TIMEOUT_S if timeout_s is None else timeout_s
        self._label = label
        self._ctx = None
        self._state: LockState | None = None
        self._reason = ""

    def ensure(self, reason: str) -> None:
        """第一次调用时真正取锁；已持有则直接返回（不重复取，也就不会撞上重入保护）。

        :raises ForegroundBusy: 等待超时——**如实失败**，绝不冒着搅乱别人前台的风险硬抓。
        """
        if self._ctx is not None:
            return
        self._reason = reason
        ctx = input_lock(timeout_s=self._timeout_s, label=self._label or reason)
        try:
            self._state = ctx.__enter__()
        except InputLockTimeout as e:
            raise ForegroundBusy(reason, self._timeout_s, e.owner) from None
        self._ctx = ctx

    def release(self) -> None:
        """释放（没取过就什么都不做）。异常路径也要走到——由调用方的 ``finally`` 保证。"""
        ctx, self._ctx = self._ctx, None
        self._state = None
        if ctx is not None:
            ctx.__exit__(None, None, None)

    @property
    def state(self) -> LockState | None:
        """取锁实况（没取过或已释放时为 None）；调用方可据此如实回报「这次没有跨进程保证」。"""
        return self._state

    @property
    def reason(self) -> str:
        """为什么取了锁（没取过时为空串）——给报错文案与日志用。"""
        return self._reason


def status() -> dict:
    """机器可读的互斥能力状态——**真实探测**（真去建一次锁对象），不是「有没有这个模块」。

    与 ``capture_backends.wgc`` 同一套口径：``available`` 为 false 时 ``reason`` 要写清为什么，
    因为「没有跨进程互斥」这件事必须能被看出来，而不是等两个会话互相插队才发现。
    """
    disabled = _disabled_reason(True)
    info = {
        "name": DEFAULT_NAME,
        "timeout_s": _resolve_timeout(None),
        "foreground_timeout_s": FOREGROUND_TIMEOUT_S,
        "timeout_env": TIMEOUT_ENV,
        "disable_env": DISABLE_ENV,
        "disabled_by": disabled,
        # 谁在持锁（读不到就是 {}）。用户问「为什么卡住」时，这一项就能回答。
        "holder": holder(),
    }
    if disabled:
        return {**info, "backend": "none", "available": False, "reason": disabled}
    backend, reason = _make_backend(DEFAULT_NAME)
    if backend is None:
        return {**info, "backend": "none", "available": False, "reason": reason}
    name = backend.backend
    try:
        backend.close()
    except Exception:  # noqa: BLE001 - 探测对象关不掉也不影响结论
        pass
    return {**info, "backend": name, "available": True, "reason": ""}


if __name__ == "__main__":  # pragma: no cover - 快速自检
    print(json.dumps(status(), ensure_ascii=False, indent=2))
