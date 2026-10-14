"""MCP Registry：管理已连接的 server 并组装动态工具池。"""

from typing import Any

from myharness.mcp.client import MCPClient, normalize_mcp_name


class MCPRegistry:
    """
    MCP server 注册表。

    职责：
    1. 管理已连接的 server
    2. 组装动态工具池（内置工具 + MCP 工具）
    3. 检测命名冲突
    4. 应用宿主侧授权策略
    """

    def __init__(self):
        """初始化注册表。"""
        self.clients: dict[str, MCPClient] = {}
        self.host_policy: dict[tuple[str, str], str] = {}
        self.tool_policies: dict[str, str] = {}

    def add_client(self, client: MCPClient) -> None:
        """
        添加已连接的 MCP client。

        Args:
            client: MCPClient 实例

        Raises:
            ValueError: server 名称已存在
        """
        if client.name in self.clients:
            raise ValueError(f"MCP server {client.name!r} already connected")
        self.clients[client.name] = client

    def register_stdio_server(self, name: str, transport) -> None:
        """
        注册 stdio MCP server。

        Args:
            name: Server 名称
            transport: StdioTransport 实例

        该方法会：
        1. 启动 transport（连接到 server 进程）
        2. 创建 MCPClient
        3. 发现工具
        4. 注册到 registry
        """
        from myharness.mcp.stdio import StdioTransport

        if not isinstance(transport, StdioTransport):
            raise TypeError("transport must be StdioTransport instance")

        # 启动连接
        transport.start()

        # 初始化握手
        transport.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "myharness",
                    "version": "1.0.0",
                },
            },
        )

        # 发现工具
        tools_result = transport.send_request("tools/list")
        tools = tools_result.get("tools", [])

        # 创建 client
        client = MCPClient(name)
        client.transport = transport  # 保存引用以便 cleanup

        # 准备工具定义和 handlers
        tool_defs = []
        handlers = {}

        for tool in tools:
            tool_name = tool["name"]
            tool_def = {
                "name": tool_name,
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {"type": "object"}),
            }
            tool_defs.append(tool_def)

            # Handler: 调用 transport.send_request("tools/call")
            def make_handler(tn):
                def handler(**kwargs):
                    try:
                        result = transport.send_request(
                            "tools/call",
                            {"name": tn, "arguments": kwargs},
                        )
                        # 提取 content
                        content = result.get("content", [])
                        if content and isinstance(content, list):
                            return "\n".join(
                                item.get("text", str(item))
                                for item in content
                                if isinstance(item, dict)
                            )
                        return str(result)
                    except Exception as exc:
                        return f"Error: {type(exc).__name__}: {exc}"
                return handler

            handlers[tool_name] = make_handler(tool_name)

        # 使用 register() 方法注册工具和 handlers
        client.register(tool_defs, handlers)

        # 添加到 registry
        self.add_client(client)

    def set_policy(self, server: str, tool: str, policy: str) -> None:
        """
        设置宿主侧授权策略。

        Args:
            server: Server 名称
            tool: 工具名称（原始名称）
            policy: "allow" | "confirm" | "deny"
        """
        if policy not in {"allow", "confirm", "deny"}:
            raise ValueError(f"Invalid policy: {policy!r}")
        self.host_policy[(server, tool)] = policy

    def assemble_tool_pool(
        self, builtin_tools: list[dict], builtin_handlers: dict[str, callable]
    ) -> tuple[list[dict], dict[str, callable], dict[str, str]]:
        """
        组装动态工具池（内置 + MCP）。

        Args:
            builtin_tools: 内置工具定义
            builtin_handlers: 内置工具 handler

        Returns:
            (tools, handlers, policies) 三元组：
            - tools: 合并后的工具定义列表
            - handlers: 合并后的 handler 字典
            - policies: {prefixed_name: policy} 映射

        Raises:
            ValueError: 命名冲突、名称过长、schema 非法
        """
        tools = list(builtin_tools)
        handlers = dict(builtin_handlers)
        policies: dict[str, str] = {}

        # 记录已用的名称及其来源（用于冲突诊断）
        origins = {
            tool["name"]: f"built-in tool {tool['name']!r}"
            for tool in builtin_tools
        }

        for server_name, client in self.clients.items():
            safe_server = normalize_mcp_name(server_name)

            for tool_def in client.tools:
                raw_name = tool_def["name"]
                safe_tool = normalize_mcp_name(raw_name)

                # 前缀：mcp__{server}__{tool}
                prefixed = f"mcp__{safe_server}__{safe_tool}"

                # 检查：名称不超过 64 字符（模型限制）
                if len(prefixed) > 64:
                    raise ValueError(
                        f"MCP tool name exceeds 64 characters: {prefixed!r}"
                    )

                # 检查：规范化后不能冲突
                origin = f"MCP tool {server_name!r}/{raw_name!r}"
                if prefixed in origins:
                    raise ValueError(
                        f"MCP tool name collision after normalization: "
                        f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                    )
                origins[prefixed] = origin

                # 检查：inputSchema 必须是 object 类型
                schema = tool_def.get("inputSchema", {})
                if not isinstance(schema, dict):
                    raise ValueError(f"Invalid inputSchema for {origin}: not a dict")
                if schema.get("type", "object") != "object":
                    raise ValueError(
                        f"Invalid inputSchema for {origin}: type must be 'object'"
                    )

                # 添加工具定义（模型视角）
                tools.append({
                    "name": prefixed,
                    "description": tool_def.get("description", ""),
                    "input_schema": schema,
                })

                # 添加 handler（路由到 MCPClient）
                # 注意：lambda 必须用默认参数捕获当前值，否则会全部指向最后一个
                handlers[prefixed] = (
                    lambda *, client=client, tool=raw_name, **kwargs:
                    client.call_tool(tool, kwargs)
                )

                # 应用宿主侧策略（不信任 server 的 readOnlyHint）
                policy = self.host_policy.get((server_name, raw_name), "confirm")
                policies[prefixed] = policy

        self.tool_policies = policies
        return tools, handlers, policies

    def cleanup(self) -> None:
        """
        清理所有连接的 MCP client。

        关闭所有 transport 连接，清空注册表。
        """
        for client in self.clients.values():
            # 如果 client 有 transport 属性（stdio），关闭它
            if hasattr(client, 'transport'):
                try:
                    client.transport.close()
                except Exception:
                    pass  # 静默失败

        self.clients.clear()
        self.host_policy.clear()
        self.tool_policies.clear()


# 全局注册表（单例）
_registry = MCPRegistry()


def get_registry() -> MCPRegistry:
    """获取全局 MCP 注册表。"""
    return _registry


def connect_mcp(name: str, client: MCPClient) -> str:
    """
    连接 MCP server（供模型调用的工具）。

    Args:
        name: Server 名称
        client: 已初始化的 MCPClient

    Returns:
        连接结果描述（契约 1：永远返回字符串）
    """
    registry = get_registry()
    try:
        registry.add_client(client)
        tool_names = ", ".join(tool["name"] for tool in client.tools)
        return (
            f"Connected to MCP server '{name}'. "
            f"Discovered {len(client.tools)} tools: {tool_names}"
        )
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def assemble_tool_pool(
    builtin_tools: list[dict], builtin_handlers: dict[str, callable]
) -> tuple[list[dict], dict[str, callable], dict[str, str]]:
    """
    组装动态工具池（便捷函数）。

    Args:
        builtin_tools: 内置工具定义
        builtin_handlers: 内置工具 handler

    Returns:
        (tools, handlers, policies) 三元组
    """
    registry = get_registry()
    return registry.assemble_tool_pool(builtin_tools, builtin_handlers)
