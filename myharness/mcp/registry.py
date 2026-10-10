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
