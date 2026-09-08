from __future__ import annotations

__all__ = (
    "fix_package_names",
    "install_lazy_importer",
    "set_deprecated_aliases",
)

import ast
import inspect
import sys
import warnings
from importlib import import_module
from types import ModuleType
from typing import Any


def install_lazy_importer() -> bool:
    module_globals = sys._getframe(1).f_globals
    module_name = module_globals["__name__"]
    module_prefix = module_name + "."
    module = sys.modules[module_name]
    lazy_map, deprecated_aliases, submodule_names = _build_lazy_map(module)
    names = sorted(lazy_map)

    # Delete symbols that are not part of the API
    del module_globals["TYPE_CHECKING"]
    del module_globals["install_lazy_importer"]

    if not lazy_map and not deprecated_aliases:
        return False

    def __getattr__(name: str) -> Any:
        if new_name := deprecated_aliases.get(name):
            emit_deprecation_warning(module_name, name, new_name)
            target_mod, target_attr = new_name.rsplit(".", 1)
        elif name in submodule_names:
            target_mod, target_attr = "." + name, ""
        else:
            try:
                target_mod, target_attr = lazy_map[name]
            except KeyError:
                raise AttributeError(
                    f"module {module_name!r} has no attribute {name!r}"
                ) from None

        imported = import_module(target_mod, module_name)
        value = getattr(imported, target_attr) if target_attr else imported

        # patch the module name to match
        if (
            getattr(value, "__module__", "").startswith(module_prefix)
            and name not in deprecated_aliases
        ):
            value.__module__ = module_name

        module_globals[name] = value
        return value

    def __dir__() -> list[str]:
        return names

    module_globals["__dir__"] = __dir__
    module_globals["__getattr__"] = __getattr__
    module_globals.pop("fix_package_names", None)
    module_globals.pop("set_deprecated_aliases", None)
    return True


def fix_package_names() -> None:
    module_globals = sys._getframe(1).f_globals
    module_prefix = module_globals["__name__"] + "."
    del module_globals[fix_package_names.__name__]
    for value in module_globals.values():
        if modname := getattr(value, "__module__", ""):
            if modname.startswith(module_prefix):
                parts = modname.split(".")
                value.__module__ = ".".join(
                    part for part in parts if not part.startswith("_")
                )


def emit_deprecation_warning(module_name: str, name: str, target: str) -> None:
    warnings.warn(
        f"The {module_name}.{name} alias is deprecated, use {target} instead.",
        DeprecationWarning,
        stacklevel=3,
    )


def set_deprecated_aliases(aliases: dict[str, str]) -> None:
    module_globals = sys._getframe(1).f_globals
    module_name = module_globals["__name__"]
    del module_globals[set_deprecated_aliases.__name__]

    def __getattr__(name: str) -> Any:
        try:
            target = aliases[name]
        except KeyError:
            raise AttributeError(
                f"module {module_name!r} has no attribute {name!r}"
            ) from None

        emit_deprecation_warning(module_name, name, target)
        target_modname, attrname = target.rsplit(".", 1)
        module = import_module(target_modname)
        return getattr(module, attrname)

    sys.modules[module_name].__dict__["__getattr__"] = __getattr__


def _build_lazy_map(
    module: ModuleType,
) -> tuple[dict[str, tuple[str, str]], dict[str, str], list[str]]:
    try:
        source = inspect.getsource(module)
    except OSError:
        return {}, {}, []

    tree = compile(source, module.__file__ or "", "exec", ast.PyCF_ONLY_AST)
    assert isinstance(tree, ast.Module)
    out: dict[str, tuple[str, str]] = {}
    deprecated_aliases: dict[str, str] = {}
    submodule_names: list[str] = []

    for node in tree.body:
        if not isinstance(node, ast.If) or not _is_type_checking_block(node.test):
            continue

        for stmt in node.body:
            match stmt:
                case ast.ImportFrom():
                    if stmt.module is None:
                        submodule_names.extend(alias.name for alias in stmt.names)
                    else:
                        base = "." * stmt.level + (stmt.module or "")
                        for alias in stmt.names:
                            if alias.name == "*":
                                raise RuntimeError("star imports not supported")

                            exported = alias.asname or alias.name
                            out[exported] = (base, alias.name)
                case ast.Expr() if isinstance(stmt.value, ast.Call):
                    call = stmt.value
                    if (
                        isinstance(call.func, ast.Name)
                        and call.func.id == "set_deprecated_aliases"
                    ):
                        arg0 = call.args[0]
                        assert isinstance(arg0, ast.Dict)
                        for key, value in zip(arg0.keys, arg0.values, strict=True):
                            assert isinstance(key, ast.Constant)
                            assert isinstance(key.value, str)
                            assert isinstance(value, ast.Constant)
                            assert isinstance(value.value, str)
                            deprecated_aliases[key.value] = value.value

    return out, deprecated_aliases, submodule_names


def _is_type_checking_block(test: ast.AST) -> bool:
    if not isinstance(test, ast.BoolOp):
        return False

    subtest = test.values[0]
    match subtest:
        case ast.Name():
            return subtest.id == "TYPE_CHECKING"
        case ast.Attribute():
            return (
                isinstance(subtest.value, ast.Name)
                and subtest.value.id == "typing"
                and subtest.attr == "TYPE_CHECKING"
            )
        case _:
            return False
