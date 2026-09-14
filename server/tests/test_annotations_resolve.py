"""每个模块的注解都必须能解析出来。

`from __future__ import annotations` 让注解变成惰性字符串：写了一个没导入的名字，
import 照样成功、pytest 照样全绿，直到有人真的去看注解。而那件事并不稀奇——
`typing.get_type_hints`、FastAPI 的依赖解析、dataclass/pydantic 的字段推导都会走这一步，
踩到时是一个 `NameError`，而不是一条能看懂的报错。

这个文件就是那次「真的去看」：把注解逐个解析一遍。它抓到的是 `Callable` 这类漏导入
（本仓库真的发生过一次），而且比 lint 更直接——它验的是运行时会踩的那条路径。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module_names(package: str, directory: Path) -> list[str]:
    names = [package]
    for info in pkgutil.walk_packages([str(directory)], prefix=f"{package}."):
        names.append(info.name)
    return names


def _targets(module: typing.Any) -> list[tuple[str, typing.Any]]:
    """模块里所有「自带注解」的调用点：模块级函数，以及各方法。

    刻意只收函数，不收类：get_type_hints 对类会沿 MRO 走到底，把三方基类的惰性注解也算
    进来（例如 langchain 的 BaseTool，它只存在于 TYPE_CHECKING 分支），于是别人的书写
    习惯会变成我们这里的红。这个文件只对「我们自己写的名字」负责。
    """

    found: list[tuple[str, typing.Any]] = []
    for name, value in vars(module).items():
        if getattr(value, "__module__", None) != module.__name__:
            continue  # 只检查这个模块自己定义的东西（导入进来的不算）
        if inspect.isfunction(value):
            found.append((name, value))
        elif inspect.isclass(value):
            # 类自己的注解用本模块的命名空间解析（与 get_type_hints 对「自己的类」做的事
            # 一样，只是不走 MRO）。字符串形态就是惰性注解，这里必须显式求值。
            for field, annotation in (value.__dict__.get("__annotations__") or {}).items():
                if isinstance(annotation, str):
                    found.append((f"{name}.{field}", annotation))
            for member_name, member in vars(value).items():
                if getattr(member, "__module__", None) == module.__name__ and inspect.isfunction(
                    member
                ):
                    found.append((f"{name}.{member_name}", member))
    return found


def _resolve(module: typing.Any, value: typing.Any) -> None:
    """解析一个目标：函数交给 get_type_hints，惰性注解的字符串用本模块命名空间求值。"""

    if isinstance(value, str):
        eval(value, dict(vars(module)))  # noqa: S307 - 求值的正是模块自己的命名空间
        return
    typing.get_type_hints(value)


@pytest.fixture(scope="module")
def server_modules() -> list[str]:
    return _module_names("afr_server", ROOT / "server" / "afr_server")


def test_server_module_annotations_all_resolve(server_modules: list[str]) -> None:
    """服务端每个模块的每个注解都要能解析出一个真实类型。"""

    unresolved: list[str] = []
    checked = 0
    for module_name in server_modules:
        module = importlib.import_module(module_name)
        for name, value in _targets(module):
            checked += 1
            try:
                _resolve(module, value)
            except Exception as exc:  # noqa: BLE001 - 任何解析失败都要被看见
                unresolved.append(f"{module_name}.{name}: {type(exc).__name__}: {exc}")

    assert checked > 20, f"只找到 {checked} 个对象，这个检查大概没有真的跑起来"
    assert unresolved == [], "有注解解析不出来（通常是漏了一个 import）：\n" + "\n".join(unresolved)


def test_sdk_module_annotations_all_resolve() -> None:
    """SDK 同样要能解析——它是被独立分发的那个包，漏导入的代价更高。"""

    package = "agent_flight_recorder"
    directory = ROOT / "sdk" / package
    modules = _module_names(package, directory)

    unresolved: list[str] = []
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name, value in _targets(module):
            try:
                _resolve(module, value)
            except Exception as exc:  # noqa: BLE001
                unresolved.append(f"{module_name}.{name}: {type(exc).__name__}: {exc}")

    assert unresolved == [], "有注解解析不出来（通常是漏了一个 import）：\n" + "\n".join(unresolved)
