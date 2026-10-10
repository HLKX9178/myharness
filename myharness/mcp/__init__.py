"""MCP (Model Context Protocol) 集成。

提供：
1. MCPClient：封装工具发现与调用
2. connect_mcp：连接 server 的工具
3. assemble_tool_pool：动态组装工具池
"""

from myharness.mcp.client import MCPClient, normalize_mcp_name
from myharness.mcp.registry import MCPRegistry, connect_mcp, assemble_tool_pool

__all__ = [
    "MCPClient",
    "normalize_mcp_name",
    "MCPRegistry",
    "connect_mcp",
    "assemble_tool_pool",
]
