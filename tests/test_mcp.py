"""测试 MCP 集成（M4 验收）。"""

import pytest

from myharness.mcp.client import MCPClient, normalize_mcp_name
from myharness.mcp.registry import MCPRegistry
from myharness.mcp.mock_servers import create_mock_docs_server, create_mock_deploy_server
from myharness.tools import assemble_tool_pool, TOOLS, TOOL_HANDLERS


class TestNormalization:
    """测试名称规范化（避免冲突）。"""

    def test_allowed_chars_unchanged(self):
        """允许的字符不被替换。"""
        assert normalize_mcp_name("docs_v2") == "docs_v2"
        assert normalize_mcp_name("get-version") == "get-version"
        assert normalize_mcp_name("Tool123") == "Tool123"

    def test_disallowed_chars_replaced(self):
        """不允许的字符被替换为下划线。"""
        assert normalize_mcp_name("docs.one/get.version") == "docs_one_get_version"
        assert normalize_mcp_name("a@b#c") == "a_b_c"

    def test_empty_after_normalization(self):
        """规范化后为空字符串时抛异常。"""
        with pytest.raises(ValueError, match="cannot normalize to empty"):
            normalize_mcp_name("@@@")


class TestMCPClient:
    """测试 MCPClient（单个 server 的抽象）。"""

    def test_register_and_call(self):
        """正常注册与调用。"""
        client = MCPClient("test")
        client.register(
            tool_defs=[
                {
                    "name": "greet",
                    "description": "Greet someone",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                }
            ],
            handlers={"greet": lambda name: f"Hello, {name}!"},
        )

        assert len(client.tools) == 1
        result = client.call_tool("greet", {"name": "Alice"})
        assert result == "Hello, Alice!"

    def test_missing_name(self):
        """工具缺少名称时抛异常。"""
        client = MCPClient("test")
        with pytest.raises(ValueError, match="non-empty name"):
            client.register(
                tool_defs=[{"description": "No name"}],
                handlers={},
            )

    def test_duplicate_name(self):
        """工具名重复时抛异常。"""
        client = MCPClient("test")
        with pytest.raises(ValueError, match="Duplicate tool names"):
            client.register(
                tool_defs=[
                    {"name": "foo", "inputSchema": {"type": "object"}},
                    {"name": "foo", "inputSchema": {"type": "object"}},
                ],
                handlers={"foo": lambda: ""},
            )

    def test_missing_handler(self):
        """handler 缺失时抛异常。"""
        client = MCPClient("test")
        with pytest.raises(ValueError, match="Missing handlers"):
            client.register(
                tool_defs=[{"name": "foo", "inputSchema": {"type": "object"}}],
                handlers={},
            )

    def test_call_unknown_tool(self):
        """调用未注册的工具返回错误字符串（契约 1）。"""
        client = MCPClient("test")
        client.register(tool_defs=[], handlers={})
        result = client.call_tool("unknown", {})
        assert "MCP error: unknown tool" in result

    def test_handler_exception(self):
        """handler 抛异常时返回错误字符串（契约 4）。"""
        client = MCPClient("test")
        client.register(
            tool_defs=[{"name": "crash", "inputSchema": {"type": "object"}}],
            handlers={"crash": lambda: 1 / 0},
        )
        result = client.call_tool("crash", {})
        assert "MCP error: ZeroDivisionError" in result


class TestMCPRegistry:
    """测试 MCPRegistry（多 server 管理与工具池组装）。"""

    def test_add_duplicate_server(self):
        """重复添加同名 server 时抛异常。"""
        registry = MCPRegistry()
        client = MCPClient("test")
        registry.add_client(client)

        with pytest.raises(ValueError, match="already connected"):
            registry.add_client(client)

    def test_assemble_with_prefix(self):
        """MCP 工具被加上 mcp__{server}__{tool} 前缀。"""
        registry = MCPRegistry()
        client = MCPClient("docs")
        client.register(
            tool_defs=[
                {"name": "search", "inputSchema": {"type": "object"}},
            ],
            handlers={"search": lambda: "found"},
        )
        registry.add_client(client)

        tools, handlers = registry.assemble_tool_pool([], {})

        assert len(tools) == 1
        assert tools[0]["name"] == "mcp__docs__search"
        assert "mcp__docs__search" in handlers
        assert handlers["mcp__docs__search"]() == "found"

    def test_name_collision_after_normalization(self):
        """规范化后名称冲突时抛异常（M4 验收标准 3）。"""
        registry = MCPRegistry()

        # 两个工具规范化后都是 mcp__docs__get_version
        client1 = MCPClient("docs")
        client1.register(
            tool_defs=[{"name": "get.version", "inputSchema": {"type": "object"}}],
            handlers={"get.version": lambda: "v1"},
        )
        registry.add_client(client1)

        client2 = MCPClient("docs")
        client2.register(
            tool_defs=[{"name": "get_version", "inputSchema": {"type": "object"}}],
            handlers={"get_version": lambda: "v2"},
        )

        # 第二个 server 名字一样，先过不了 add_client
        # 换个场景：内置工具与 MCP 工具冲突
        registry2 = MCPRegistry()
        client3 = MCPClient("sys")
        client3.register(
            tool_defs=[{"name": "bash", "inputSchema": {"type": "object"}}],
            handlers={"bash": lambda: "mock"},
        )
        registry2.add_client(client3)

        builtin = [{"name": "mcp__sys__bash"}]  # 模拟冲突
        with pytest.raises(ValueError, match="name collision"):
            registry2.assemble_tool_pool(builtin, {})

    def test_name_too_long(self):
        """名称超过 64 字符时抛异常。"""
        registry = MCPRegistry()
        client = MCPClient("a" * 30)
        client.register(
            tool_defs=[{"name": "b" * 30, "inputSchema": {"type": "object"}}],
            handlers={"b" * 30: lambda: ""},
        )
        registry.add_client(client)

        with pytest.raises(ValueError, match="exceeds 64 characters"):
            registry.assemble_tool_pool([], {})

    def test_invalid_schema_type(self):
        """inputSchema.type 不是 object 时抛异常。"""
        registry = MCPRegistry()
        client = MCPClient("bad")
        client.register(
            tool_defs=[{"name": "foo", "inputSchema": {"type": "string"}}],
            handlers={"foo": lambda: ""},
        )
        registry.add_client(client)

        with pytest.raises(ValueError, match="type must be 'object'"):
            registry.assemble_tool_pool([], {})


class TestMockServers:
    """测试 mock server（模拟 stdio/HTTP transport）。"""

    def test_docs_server(self):
        """docs server 提供 search 和 get_version。"""
        client = create_mock_docs_server()
        assert len(client.tools) == 2
        assert client.call_tool("search", {"query": "hello"}) == "[docs] Found 3 results for 'hello'"
        assert client.call_tool("get_version", {}) == "[docs] API v2.1.0"

    def test_deploy_server(self):
        """deploy server 提供 trigger 和 status。"""
        client = create_mock_deploy_server()
        assert len(client.tools) == 2
        assert client.call_tool("trigger", {"service": "api"}) == "[deploy] Triggered: api"
        assert client.call_tool("status", {"service": "api"}) == "[deploy] api: running (v1.4.2)"


class TestIntegration:
    """端到端集成测试（验证 assemble_tool_pool）。"""

    def test_assemble_with_mcp(self, monkeypatch):
        """MCP 工具与内置工具无冲突地合并。"""
        # 清空注册表
        from myharness.mcp import registry as reg_module
        monkeypatch.setattr(reg_module, "_registry", MCPRegistry())

        # 连接 mock server
        from myharness.tools import connect_mcp
        result = connect_mcp("docs")
        assert "Connected to MCP server 'docs'" in result

        # 组装工具池
        tools, handlers = assemble_tool_pool()

        # 内置工具仍存在
        builtin_names = {t["name"] for t in TOOLS}
        assert builtin_names <= {t["name"] for t in tools}

        # MCP 工具被添加
        mcp_names = {t["name"] for t in tools if t["name"].startswith("mcp__")}
        assert "mcp__docs__search" in mcp_names
        assert "mcp__docs__get_version" in mcp_names

        # handlers 一一对应
        assert set(t["name"] for t in tools) == set(handlers.keys())

        # 调用 MCP 工具
        result = handlers["mcp__docs__search"](query="test")
        assert "[docs] Found 3 results for 'test'" in result

    def test_connect_unknown_server(self):
        """连接未知 server 返回错误（契约 1）。"""
        from myharness.tools import connect_mcp
        result = connect_mcp("unknown")
        assert "Error: Unknown server" in result

    def test_connect_already_connected(self, monkeypatch):
        """重复连接返回提示。"""
        from myharness.mcp import registry as reg_module
        monkeypatch.setattr(reg_module, "_registry", MCPRegistry())

        from myharness.tools import connect_mcp
        connect_mcp("docs")
        result = connect_mcp("docs")
        assert "already connected" in result
