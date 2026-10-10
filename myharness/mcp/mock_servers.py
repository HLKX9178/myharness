"""模拟 MCP server（用于测试与演示）。

真实的 MCP transport (stdio/HTTP) 不在本阶段实现。
这里提供两个进程内 mock server 展示协议边界。
"""

from myharness.mcp.client import MCPClient


def create_mock_docs_server() -> MCPClient:
    """
    创建模拟的文档搜索 server。

    提供工具：
    - search(query): 搜索文档
    - get_version(): 获取 API 版本
    """
    server = MCPClient("docs")
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


def create_mock_deploy_server() -> MCPClient:
    """
    创建模拟的部署管理 server。

    提供工具：
    - trigger(service): 触发部署
    - status(service): 查看部署状态
    """
    server = MCPClient("deploy")
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"destructiveHint": True},
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


# 可用的 mock server 工厂
MOCK_SERVERS = {
    "docs": create_mock_docs_server,
    "deploy": create_mock_deploy_server,
}
