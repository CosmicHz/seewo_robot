"""分层守卫：确保「业务与协议解耦」不会随时间腐化。

这是本次改造最关键的自动化约束 —— 把架构约定变成**可执行的检查**，
而不是文档里的一句话。

规则
----
1. ``seewo/models.py`` 与 ``seewo/errors.py`` **不得** import 任何平台专有模块
   （希沃协议层、旧 DAO、Flask、requests…）。
2. ``seewo/core/`` **不得** import 希沃适配层、接口层、集成层。
3. ``seewo/provider/base.py`` **不得** import 具体平台实现。
4. ``seewo/providers/seewo.py`` 是**唯一**允许 import 希沃协议层的地方。
5. 依赖方向只能向下：interfaces → integrations → core → provider → models。

违反任一条 → 测试失败。
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEEWO_ROOT = REPO_ROOT / "seewo"

#: 平台专有模块（希沃协议实现、旧 DAO、框架代码）
PLATFORM_SPECIFIC_MODULES = {
    "api",
    "login",
    "msg",
    "stu",
    "upload",
    "yunban",
    "yunban_token",
    "api_server",
    "client",
    "main",
    "mock_server",
    "message_service",
    "request_manager",
    "funcs",
    "init",
    "qrcode",
    "flask",
    "requests",
    "textual",
    "PIL",
    "pyperclip",
}

#: 各层允许 import 的「下层」包前缀
ALLOWED_DOWNWARD = {
    "seewo.models": {"seewo.errors"},
    "seewo.errors": set(),
    "seewo.provider.base": {"seewo.models", "seewo.errors"},
    "seewo.core.store": {"seewo.errors", "seewo.models"},
    "seewo.core.service": {
        "seewo.errors",
        "seewo.models",
        "seewo.provider.base",
        "seewo.core.store",
    },
    "seewo.providers.seewo": {
        "seewo.errors",
        "seewo.models",
        "seewo.provider.base",
        # 复用旧 DAO：这是**唯一**被允许的例外，见 docs/ARCHITECTURE.md
        "api",
        "login",
        "msg",
        "stu",
        "upload",
        "models",
        "funcs",
    },
    "seewo.integrations.base": {"seewo.core.service", "seewo.errors", "seewo.models"},
    "seewo.integrations.onebot11.adapter": {
        "seewo.core.service",
        "seewo.errors",
        "seewo.models",
        "seewo.integrations.base",
    },
    "seewo.integrations.satori.adapter": {
        "seewo.core.service",
        "seewo.errors",
        "seewo.models",
        "seewo.integrations.base",
    },
    "seewo.interfaces.rest": {
        "seewo.core.service",
        "seewo.errors",
        "seewo.models",
    },
    "seewo.sdk": {"seewo.models", "seewo.errors"},
}


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: pathlib.Path) -> set[str]:
    """解析一个文件 import 的所有模块名（相对导入解析为包内绝对名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = _module_name(path)
    package = module.rsplit(".", 1)[0] if path.name != "__init__.py" else module

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.add(node.module)
            else:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                target = f"{base}.{node.module}" if node.module else base
                found.add(target)
    return found


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ============================ 规则 1-4 ============================


class TestLayering:
    def test_no_platform_specific_imports_in_core_and_models(self):
        """core / models / errors / provider.base 不得碰希沃专有模块"""
        guarded = [
            SEEWO_ROOT / "models.py",
            SEEWO_ROOT / "errors.py",
            SEEWO_ROOT / "provider" / "base.py",
            SEEWO_ROOT / "core" / "store.py",
            SEEWO_ROOT / "core" / "service.py",
        ]
        violations: list[str] = []
        for path in guarded:
            for imported in _imported_modules(path):
                top = imported.split(".")[0]
                if top in PLATFORM_SPECIFIC_MODULES:
                    violations.append(f"{path.name} → {imported}")
        assert violations == [], "核心层不得依赖平台专有模块: " + "; ".join(violations)

    def test_provider_base_does_not_import_concrete_providers(self):
        """Provider 契约不得反向依赖具体平台实现"""
        for imported in _imported_modules(SEEWO_ROOT / "provider" / "base.py"):
            assert not imported.startswith("seewo.providers"), (
                f"provider/base.py 不得 import 具体平台实现: {imported}"
            )

    def test_core_does_not_import_interfaces_or_integrations(self):
        """核心层不得依赖上层"""
        for path in _python_files(SEEWO_ROOT / "core"):
            for imported in _imported_modules(path):
                assert not imported.startswith("seewo.interfaces"), (
                    f"{path.name} → {imported}"
                )
                assert not imported.startswith("seewo.integrations"), (
                    f"{path.name} → {imported}"
                )
                assert not imported.startswith("seewo.providers"), (
                    f"{path.name} → {imported}"
                )

    def test_only_seewo_provider_touches_seewo_dao(self):
        """`seewo/providers/seewo.py` 是唯一允许接触希沃协议层的地方"""
        offenders: list[str] = []
        for path in _python_files(SEEWO_ROOT):
            if path == SEEWO_ROOT / "providers" / "seewo.py":
                continue
            for imported in _imported_modules(path):
                top = imported.split(".")[0]
                if top in {"api", "login", "msg", "stu", "upload", "yunban"}:
                    offenders.append(f"{_module_name(path)} → {imported}")
        assert offenders == [], (
            "只有 seewo/providers/seewo.py 可以依赖希沃协议层: " + "; ".join(offenders)
        )

    def test_models_has_no_third_party_dependencies(self):
        """平台中立模型只依赖标准库 —— 保证任何框架都能复用"""
        for imported in _imported_modules(SEEWO_ROOT / "models.py"):
            top = imported.split(".")[0]
            assert top not in PLATFORM_SPECIFIC_MODULES, (
                f"models.py 不得依赖 {imported}"
            )
            assert top not in {"flask", "requests", "pydantic"}, (
                f"models.py 不得依赖第三方框架: {imported}"
            )


# ============================ 规则 5：依赖方向 ============================


class TestDependencyDirection:
    @pytest.mark.parametrize(
        ("module", "allowed"),
        sorted(ALLOWED_DOWNWARD.items()),
    )
    def test_module_imports_only_allowed_dependencies(self, module, allowed):
        """每个模块只 import 白名单内的下层模块"""
        path = REPO_ROOT / pathlib.Path(*module.split(".")).with_suffix(".py")
        if not path.exists():
            path = REPO_ROOT / pathlib.Path(*module.split(".")) / "__init__.py"
        if not path.exists():
            pytest.skip(f"{module} 尚未实现")

        violations: list[str] = []
        for imported in _imported_modules(path):
            if not imported.startswith("seewo"):
                continue
            if imported in allowed:
                continue
            # 同层模块间的横向依赖也允许（如 integrations 的 adapter → base）
            if any(imported.startswith(a + ".") for a in allowed):
                continue
            violations.append(imported)
        assert violations == [], f"{module} 依赖了不允许的模块: {violations}"

    def test_no_circular_imports_in_seewo_package(self):
        """包内不得存在「上层 import 下层」的逆向依赖。

        层号越小越靠底层（models 是地基，interfaces 是顶层）。
        允许：同层互引、向下引用。
        禁止：从高层引用低层以外的层（即逆向依赖），那正是循环依赖的温床。
        """
        layer_of = {
            "models": 0,
            "errors": 0,
            "provider": 1,
            "core": 2,
            "providers": 2,
            "integrations": 3,
            "interfaces": 4,
        }

        def _layer(module: str) -> int | None:
            """取模块所属层；不在分层表内（如 seewo.sdk）返回 None。"""
            parts = module.split(".")
            if len(parts) < 2:
                return None
            return layer_of.get(parts[1])

        violations: list[str] = []
        for path in _python_files(SEEWO_ROOT):
            module = _module_name(path)
            own = _layer(module)
            if own is None:
                continue
            for imported in _imported_modules(path):
                if not imported.startswith("seewo."):
                    continue
                other = _layer(imported)
                if other is None:
                    continue
                # 同层互引允许（如 core.store ↔ core.service）
                if other == own:
                    continue
                # 允许引用**更低**层；引用更高层 = 逆向依赖
                if other > own:
                    violations.append(f"{module} → {imported}")
        assert violations == [], "存在逆向（向上）依赖: " + "; ".join(violations)


def test_seewo_package_is_importable():
    """包能被正常导入（无循环 import、无缺失依赖）"""
    import seewo

    assert seewo.__version__
    for name in seewo.__all__:
        assert hasattr(seewo, name), f"seewo.{name} 缺失"


def test_no_syntax_errors():
    """所有新增文件都能被 Python 解析"""
    for path in _python_files(SEEWO_ROOT):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_todo_or_placeholder_in_new_code():
    """新代码不得留占位实现（TODO / NotImplementedError / pass 桩）"""
    offenders: list[str] = []
    for path in _python_files(SEEWO_ROOT):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "*")):
                continue
            if "TODO" in stripped or "FIXME" in stripped:
                offenders.append(f"{path.name}:{lineno} {stripped}")
    assert offenders == [], "新代码不应留 TODO/FIXME: " + "; ".join(offenders)


def test_python_version_compatible():
    """新代码用的语法需 Python 3.10+ 可解析（本项目要求 3.12）"""
    for path in _python_files(SEEWO_ROOT):
        ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
            feature_version=(3, 12),
        )


def test_no_sys_path_manipulation_in_package():
    """包内不得操作 sys.path（破坏可安装性）"""
    for path in _python_files(SEEWO_ROOT):
        text = path.read_text(encoding="utf-8")
        assert "sys.path" not in text, f"{path.name} 不得修改 sys.path"


def test_module_count_is_reasonable():
    """防止无节制增加模块数量（架构过度设计守卫）"""
    files = _python_files(SEEWO_ROOT)
    # 超过 30 个文件说明抽象层膨胀，需要重新审视
    assert len(files) <= 30, f"seewo/ 下有 {len(files)} 个文件，疑似过度设计"
