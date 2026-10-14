"""MCP stdio 传输层。

通过 stdin/stdout 与外部 MCP server 进程通信。

协议：
- 请求和响应都是 JSON-RPC 2.0 格式
- 每条消息占一行（换行分隔）
- server 通过 subprocess 启动
"""

import json
import platform
import subprocess
from pathlib import Path
from typing import Any

from myharness.mcp.client import MCPClient


class StdioTransport:
    """
    MCP stdio 传输层。

    负责：
    1. 启动 server 进程
    2. 发送 JSON-RPC 请求
    3. 接收 JSON-RPC 响应
    4. 进程生命周期管理
    """

    def __init__(self, command: list[str], cwd: Path | None = None):
        """
        初始化传输层。

        Args:
            command: server 启动命令（例如 ["node", "server.js"]）
            cwd: 工作目录
        """
        self.command = command
        self.cwd = cwd
        self.process: subprocess.Popen | None = None
        self._next_id = 1

    def start(self) -> None:
        """启动 server 进程。"""
        if self.process is not None:
            raise RuntimeError("Transport already started")

        # Windows 上需要使用 .cmd 扩展名
        command = self.command.copy()
        if platform.system() == "Windows":
            if command[0] == "npx":
                command[0] = "npx.cmd"
            elif command[0] == "npm":
                command[0] = "npm.cmd"

        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.cwd,
        )

    def stop(self) -> None:
        """停止 server 进程。"""
        if self.process is None:
            return

        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            self.process = None

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """
        发送 JSON-RPC 请求并等待响应。

        Args:
            method: 方法名（例如 "tools/list"）
            params: 参数字典

        Returns:
            响应结果

        Raises:
            RuntimeError: 传输未启动、进程已退出、或响应格式错误
        """
        if self.process is None:
            raise RuntimeError("Transport not started")

        # 构造 JSON-RPC 请求
        request_id = self._next_id
        self._next_id += 1

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        # 发送请求（一行 JSON）
        request_line = json.dumps(request) + "\n"
        self.process.stdin.write(request_line)
        self.process.stdin.flush()

        # 接收响应（一行 JSON）
        response_line = self.process.stdout.readline()
        if not response_line:
            raise RuntimeError("MCP server closed connection")

        response = json.loads(response_line)

        # 检查响应
        if response.get("id") != request_id:
            raise RuntimeError(
                f"Response ID mismatch: expected {request_id}, got {response.get('id')}"
            )

        if "error" in response:
            error = response["error"]
            raise RuntimeError(
                f"MCP error: {error.get('message', 'unknown error')}"
            )

        return response.get("result")

    def __enter__(self):
        """上下文管理器：启动。"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器：停止。"""
        self.stop()


def create_stdio_client(
    name: str,
    command: list[str],
    cwd: Path | None = None,
) -> MCPClient:
    """
    创建通过 stdio 连接的 MCP client。

    Args:
        name: Server 名称
        command: 启动命令（例如 ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/path"]）
        cwd: 工作目录

    Returns:
        已连接的 MCPClient

    Raises:
        RuntimeError: 连接失败或工具发现失败
    """
    transport = StdioTransport(command, cwd)
    transport.start()

    try:
        # 1. 初始化握手
        init_result = transport.send_request(
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

        print(f"[MCP] Connected to {name}")
        print(f"      Server: {init_result.get('serverInfo', {}).get('name', 'unknown')}")

        # 2. 发现工具
        tools_result = transport.send_request("tools/list")
        tools = tools_result.get("tools", [])

        print(f"      Tools: {len(tools)} available")

        # 3. 创建 MCPClient 并注册工具
        client = MCPClient(name)

        # 转换 MCP 工具定义为内部格式
        tool_defs = []
        handlers = {}

        for tool in tools:
            tool_name = tool["name"]
            tool_defs.append({
                "name": tool_name,
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {"type": "object"}),
            })

            # 创建调用 handler（闭包捕获 transport 和 tool_name）
            def make_handler(t: StdioTransport, tn: str):
                def handler(**kwargs) -> str:
                    try:
                        result = t.send_request(
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

            handlers[tool_name] = make_handler(transport, tool_name)

        client.register(tool_defs, handlers)

        # 保存 transport 引用（避免被回收）
        client._transport = transport

        return client

    except Exception as exc:
        transport.stop()
        raise RuntimeError(f"Failed to connect to MCP server {name}: {exc}") from exc
