"""应用自己起的后台线程：登记在册，才能被等、被看见。

服务端有几条「提交即返回」的路径（回放、用例、批量套件、启动播种），真正的工作在后台
线程里跑，请求不等它们——这是承诺，不是妥协。代价是随之而来的纪律：**后台工作必须能被
看见**。能说清「现在还有谁在跑」的调用方，才有可能在换库、换 engine 或关停之前等它们落地。

这不是抽象的洁癖。测试隔离要给每个测试换一份新的 SQLite 库，而 engine 是按**当前**设置
懒建的（`db.get_engine`）。一个没被登记的后台线程会在下一个测试里重建 engine，于是连到
**别人的库**上去写：症状是后台日志里的 `no such table: cases`，或者某个测试干等 90s 之后
超时。issue #18 就是这一条——播种线程是裸 `threading.Thread`，不在任何线程池里，drain
永远看不见它。

因此这里只做一件事：把「起一个后台线程」变成登记在册的动作。线程仍然是 daemon、仍然
立刻返回、仍然不阻塞服务启动；变的只是它不再隐身。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

#: 等后台线程落地时的默认上限。等待本身不能变成新的挂死来源，所以它有尽头。
DRAIN_TIMEOUT_SECONDS = 60.0


class BackgroundThreads:
    """登记 + 等待应用起的后台线程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: list[tuple[str, threading.Thread]] = []

    def start(self, name: str, target: Callable[[], None]) -> threading.Thread:
        """起一个登记在册的后台线程，返回该线程。语义与 `Thread(...).start()` 一致。"""

        thread = threading.Thread(target=target, name=name, daemon=True)
        with self._lock:
            # 顺手清掉已经结束的：登记表不该随运行时长无限增长。
            self._threads = [item for item in self._threads if item[1].is_alive()]
            self._threads.append((name, thread))
            # 在锁里 start：`Thread.start()` 只等到线程开始引导（`_started` 置位）就返回，
            # 不等目标函数跑完，因此不会把调用方卡在线程体上。放进锁里是为了让「登记」与
            # 「开跑」之间没有缝——否则 drain 可能正好在这个缝里漏掉它。
            thread.start()
        return thread

    def live(self) -> list[tuple[str, threading.Thread]]:
        """此刻还活着的后台线程（名字 + 线程对象）。"""

        with self._lock:
            return [(name, thread) for name, thread in self._threads if thread.is_alive()]

    def names(self) -> list[str]:
        """此刻还活着的后台线程名。"""

        return [name for name, _ in self.live()]

    def join_all(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> list[str]:
        """等所有还活着的后台线程落地；返回超时后仍没落地的线程名（空列表=都落地了）。"""

        deadline = time.monotonic() + timeout
        while True:
            live = self.live()
            if not live:
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return [name for name, _ in live]
            # 循环而不是只 join 一轮：等待期间可能又起了新的线程（播种线程自己也可能触发
            # 别的后台路径），只等第一轮会漏掉它们。
            for _name, thread in live:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))


#: 模块级单例：一个进程一份登记表。
registry = BackgroundThreads()


def start(name: str, target: Callable[[], None]) -> threading.Thread:
    """起一个登记在册的后台线程。应用代码用它，而不是直接 `threading.Thread(...).start()`。"""

    return registry.start(name, target)


def names() -> list[str]:
    """此刻还在跑的后台线程名。"""

    return registry.names()


def join_all(timeout: float = DRAIN_TIMEOUT_SECONDS) -> list[str]:
    """等所有后台线程落地；返回超时后仍没落地的名字（空列表=都落地了）。"""

    return registry.join_all(timeout)
