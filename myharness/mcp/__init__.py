"""MCP (Model Context Protocol) 集成。

提供：
1. MCPClient：封装工具发现与调用
2. connect_mcp：连接 server 的工具
3. assemble_tool_pool：动态组装工具池
4. get_registry：获取全局 registry 实例
"""

from myharness.mcp.client import MCPClient, normalize_mcp_name
from myharness.mcp.registry import MCPRegistry, connect_mcp, assemble_tool_pool, get_registry

__all__ = [
    "MCPClient",
    "normalize_mcp_name",
    "MCPRegistry",
    "connect_mcp",
    "assemble_tool_pool",
    "get_registry",
]
