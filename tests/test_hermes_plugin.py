#!/usr/bin/env python3
"""aiduMEM Hermes MemoryProvider 插件回归测试。

这些用例**不需要真实 aiduMEM 服务**——HTTP 层全部打桩。它们守的是
「插件与宿主的契约」，也就是最容易悄悄失效的那一层：

1. 方法签名必须与宿主 `MemoryProvider` 基类逐字一致。宿主给 `sync_turn`
   加了 `messages=`、给 `on_memory_write` 加了 `metadata=` 之后，
   旧签名会在真实调用时抛 TypeError，而不是在启动时——静默到运行才炸。
2. tool schema 必须能被宿主 `normalize_tool_schema()` 解析出 name，
   否则严格 provider（如 DeepSeek）会因一个坏 schema 拒掉整个请求。
3. 服务不可达时必须降级为「无记忆」，绝不能让宿主对话失败。

宿主不在（开源用户只克隆了 aiduMEM）时整个文件 skip，不算失败。
"""

import inspect
import json
import os
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO / "integrations" / "hermes-plugin"

# 宿主 Hermes 源码位置：环境变量优先，否则试几个常见路径。
_HOST_CANDIDATES = [
    os.environ.get("HERMES_SRC"),
    "/hermes/hermes-agent",
    str(Path.home() / "hermes-agent"),
]
HOST = next((p for p in _HOST_CANDIDATES if p and Path(p, "agent", "memory_provider.py").is_file()), None)


def _load():
    """导入宿主基类 + 插件。返回 (MemoryProvider, normalize_tool_schema, plugin_module)。"""
    for p in (str(HOST), str(PLUGIN_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from agent.memory_manager import normalize_tool_schema  # type: ignore[import-not-found]
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]

    import aidumem  # type: ignore[import-not-found]

    return MemoryProvider, normalize_tool_schema, aidumem


@unittest.skipIf(HOST is None, "宿主 Hermes 源码不在本机（设 HERMES_SRC 可指定）")
class TestProviderContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base, normalize, mod = _load()
        cls.Base = base
        # 用 staticmethod 包一层：直接挂函数到类上，self.normalize 会被绑成
        # 方法而把 self 当第一个参数传进去。
        cls.normalize = staticmethod(normalize)
        cls.mod = mod
        cls.Provider = mod.AiduMemProvider

    # -- 契约 1：签名逐字一致 ------------------------------------------------

    def test_signatures_match_host_base_class(self):
        """签名漂移是运行时才炸的静默故障，必须在测试期拦住。"""
        methods = [
            "prefetch",
            "queue_prefetch",
            "sync_turn",
            "on_pre_compress",
            "on_memory_write",
            "on_session_end",
            "get_tool_schemas",
            "handle_tool_call",
            "backup_paths",
            "initialize",
            "system_prompt_block",
            "shutdown",
            "get_config_schema",
        ]
        drift = []
        for name in methods:
            base = getattr(self.Base, name, None)
            own = getattr(self.Provider, name, None)
            self.assertIsNotNone(base, f"基类缺少 {name}，宿主接口变了")
            self.assertIsNotNone(own, f"插件缺少 {name}")
            assert base is not None and own is not None  # 给类型检查器看
            if inspect.iscoroutinefunction(base) != inspect.iscoroutinefunction(own):
                drift.append(f"{name}: async/sync 不一致")
                continue
            sig_base, sig_own = str(inspect.signature(base)), str(inspect.signature(own))
            if sig_base != sig_own:
                drift.append(f"{name}: {sig_base} != {sig_own}")
        self.assertEqual(drift, [], "签名与宿主基类漂移")

    def test_no_unimplemented_abstract_methods(self):
        self.assertEqual(
            [m for m in getattr(self.Base, "__abstractmethods__", set())
             if getattr(self.Provider, m, None) is getattr(self.Base, m, None)],
            [],
        )

    def test_instantiable_without_config(self):
        p = self.Provider()
        self.assertEqual(p.name, "aidumem")

    # -- 契约 2：tool schema 宿主可解析 --------------------------------------

    def test_tool_schemas_normalize_with_names(self):
        p = self.Provider({})
        schemas = p.get_tool_schemas()
        self.assertEqual(len(schemas), 3)
        names = []
        for raw in schemas:
            norm = self.normalize(raw)
            self.assertIsNotNone(norm, f"宿主无法归一化 schema: {raw}")
            self.assertTrue(norm.get("name"), "归一化后没有 name，会被宿主跳过")
            self.assertIn("parameters", norm)
            names.append(norm["name"])
        self.assertEqual(
            sorted(names), ["aidumem_remember", "aidumem_search", "aidumem_status"]
        )

    def test_unknown_tool_returns_error_json(self):
        p = self.Provider({})
        out = json.loads(p.handle_tool_call("no_such_tool", {}))
        self.assertIn("error", out)

    # -- 契约 3：服务挂了必须降级，不能炸宿主 --------------------------------

    def _offline_provider(self):
        """指向一个必然连不上的端口。"""
        return self.Provider({"url": "http://127.0.0.1:1", "user_id": "test"})

    def test_prefetch_degrades_to_empty_when_offline(self):
        self.assertEqual(self._offline_provider().prefetch("任何查询", session_id="s"), "")

    def test_write_hooks_never_raise_when_offline(self):
        p = self._offline_provider()
        # 每个写钩子都走后台线程，主线程不应看到任何异常
        p.sync_turn("问题内容足够长", "回答", session_id="s", messages=[{"role": "user", "content": "x"}])
        p.on_memory_write("add", "user", "内容", metadata={"source": "test"})
        p.on_session_end([{"role": "user", "content": "x"}])
        self.assertEqual(p.on_pre_compress([{"role": "user", "content": "会被压掉的一轮"}]), "")
        p.shutdown()

    def test_tools_return_error_json_when_offline(self):
        p = self._offline_provider()
        for name, args in [
            ("aidumem_search", {"query": "x"}),
            ("aidumem_remember", {"content": "x"}),
            ("aidumem_status", {}),
        ]:
            out = json.loads(p.handle_tool_call(name, args))
            self.assertIn("error", out, f"{name} 离线时应返回 error JSON")

    def test_tools_validate_required_args(self):
        p = self.Provider({})
        self.assertIn("error", json.loads(p.handle_tool_call("aidumem_search", {"query": "  "})))
        self.assertIn("error", json.loads(p.handle_tool_call("aidumem_remember", {"content": ""})))

    # -- 配置来源 -----------------------------------------------------------

    def test_config_beats_env(self):
        os.environ["AIDUMEM_URL"] = "http://env:9999"
        os.environ["AIDUMEM_USER_ID"] = "env_user"
        try:
            p = self.Provider({"url": "http://cfg:1234", "user_id": "cfg_user"})
            self.assertEqual(p._client.base, "http://cfg:1234")
            self.assertEqual(p._client.user_id, "cfg_user")
            q = self.Provider({})
            self.assertEqual(q._client.base, "http://env:9999")
            self.assertEqual(q._client.user_id, "env_user")
        finally:
            os.environ.pop("AIDUMEM_URL", None)
            os.environ.pop("AIDUMEM_USER_ID", None)

    def test_backup_paths_returns_existing_dir_only(self):
        p = self.Provider({})
        os.environ["AIDUMEM_DATA_DIR"] = "/definitely/not/here"
        try:
            self.assertEqual(p.backup_paths(), [], "不存在的目录不该报进备份清单")
        finally:
            os.environ.pop("AIDUMEM_DATA_DIR", None)
        os.environ["AIDUMEM_DATA_DIR"] = str(REPO)
        try:
            self.assertEqual(p.backup_paths(), [str(REPO)])
        finally:
            os.environ.pop("AIDUMEM_DATA_DIR", None)

    # -- 注册入口 -----------------------------------------------------------

    def test_register_hands_provider_to_host(self):
        captured = []
        ctx = types.SimpleNamespace(register_memory_provider=captured.append)
        self.mod.register(ctx)
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], self.Provider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
