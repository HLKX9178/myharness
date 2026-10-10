"""MCPClient：封装单个 MCP server 的工具发现与调用。"""

import re
from typing import Any


# 模型工具名只允许 [a-zA-Z0-9_-]
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    """
    规范化 MCP 名称：把不在模型工具名字母表的字符替换为下划线。

    这样可以避免：
    - docs.one/get.version 和 docs_one/get_version 映射到同一个名字
    - 特殊字符破坏工具名格式

    Args:
        name: 原始名称（server 名或工具名）

    Returns:
        规范化后的名称

    Raises:
        ValueError: 规范化后为空字符串
    """
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized or not normalized.strip("_"):
        raise ValueError(f"MCP name cannot normalize to empty: {name!r}")
    return normalized


class MCPClient:
    """
    MCP server 的客户端抽象。

    职责：
    1. 保存 tools/list 的发现结果
    2. 提供 tools/call 的调用入口
    3. 错误转为字符串返回（契约 1）

    本章使用进程内模拟 server 展示协议边界。
    真实 stdio/HTTP transport 留给后续扩展。
    """

    def __init__(self, name: str):
        """
        初始化客户端。

        Args:
            name: Server 名称（用于日志与错误信息）
        """
        self.name = name
        self.tools: list[dict[str, Any]] = []
        self._handlers: dict[str, callable] = {}

    def register(
        self,
        tool_defs: list[dict[str, Any]],
        handlers: dict[str, callable],
    ) -> None:
        """
        注册工具定义与调用 handler（模拟 tools/list 响应）。

        Args:
            tool_defs: 工具定义列表（MCP 格式）
            handlers: {tool_name: callable} 映射

        Raises:
            ValueError: 工具名为空、重复，或 handler 缺失
        """
        names = [tool.get("name") for tool in tool_defs]

        # 检查：所有工具必须有非空名称
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError(
                f"MCP server {self.name!r}: Every tool needs a non-empty name"
            )

        # 检查：工具名不能重复
        if len(set(names)) != len(names):
            raise ValueError(
                f"MCP server {self.name!r}: Duplicate tool names found"
            )

        # 检查：所有工具都有对应的 handler
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(
                f"MCP server {self.name!r}: Missing handlers for: {', '.join(missing)}"
            )

        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """
        调用工具（对应 MCP tools/call）。

        Args:
            tool_name: 工具名（server 原始名称，不带 mcp__ 前缀）
            args: 工具参数

        Returns:
            工具输出字符串（契约 1：永远返回字符串）
        """
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}' on server {self.name!r}"

        try:
            result = handler(**args)
            return str(result)
        except Exception as exc:
            # 契约 4：错误是数据，不是异常
            return f"MCP error: {type(exc).__name__}: {exc}"
