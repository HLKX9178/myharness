"""连接真实 MCP Server 的示例。

演示如何使用 stdio 传输连接外部 MCP server。

前提条件：
1. 安装 Node.js
2. 安装 MCP server：
   npm install -g @modelcontextprotocol/server-filesystem

用法：
    python examples/mcp_stdio_example.py
"""

import sys
from pathlib import Path

# 添加 myharness 到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness.mcp.stdio import create_stdio_client
from myharness.mcp.registry import MCPRegistry


def main():
    """连接并测试 MCP filesystem server。"""
    print("=" * 60)
    print("MCP Stdio 连接示例")
    print("=" * 60)

    # 选择一个安全的测试目录
    test_dir = Path(__file__).parent / "mcp_test_workspace"
    test_dir.mkdir(exist_ok=True)

    print(f"\n[*] 测试目录: {test_dir}")

    # 创建测试文件
    test_file = test_dir / "hello.txt"
    test_file.write_text("Hello from MCP!", encoding="utf-8")
    print(f"[+] 创建测试文件: {test_file.name}")

    print("\n[*] 连接 MCP filesystem server...")
    print("    提示：如果失败，请先安装:")
    print("    npm install -g @modelcontextprotocol/server-filesystem")
    print()

    try:
        # 连接 filesystem server
        # 参数：只允许访问 test_dir
        client = create_stdio_client(
            name="filesystem",
            command=[
                "npx",
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(test_dir.absolute()),
            ],
        )

        print("\n[+] 连接成功！\n")

        # 显示可用工具
        print("[*] 可用工具:")
        for i, tool in enumerate(client.tools, 1):
            print(f"   {i}. {tool['name']}")
            desc = tool.get('description', '')
            if desc:
                print(f"      {desc}")

        # 测试工具调用
        print("\n" + "=" * 60)
        print("[*] 测试工具调用")
        print("=" * 60)

        # 1. 列出文件
        print("\n[1] 列出目录内容...")
        try:
            result = client.call_tool("list_directory", {"path": "."})
            print(f"   结果:\n{result}")
        except Exception as exc:
            print(f"   错误: {exc}")

        # 2. 读取文件
        print("\n[2] 读取 hello.txt...")
        try:
            result = client.call_tool("read_file", {"path": "hello.txt"})
            print(f"   结果:\n{result}")
        except Exception as exc:
            print(f"   错误: {exc}")

        # 3. 写入文件
        print("\n[3] 写入 test.txt...")
        try:
            result = client.call_tool(
                "write_file",
                {"path": "test.txt", "content": "测试内容\nline 2\nline 3"}
            )
            print(f"   结果: {result}")

            # 验证文件
            written_file = test_dir / "test.txt"
            if written_file.exists():
                content = written_file.read_text(encoding="utf-8")
                print(f"   [+] 文件已创建，内容:\n{content}")
        except Exception as exc:
            print(f"   错误: {exc}")

        print("\n" + "=" * 60)
        print("[+] 测试完成！")
        print("=" * 60)

    except Exception as exc:
        print(f"\n[!] 连接失败: {exc}")
        print("\n[*] 解决方法:")
        print("   1. 确保已安装 Node.js: node --version")
        print("   2. 安装 MCP server:")
        print("      npm install -g @modelcontextprotocol/server-filesystem")
        print("   3. 如果在国内，可能需要代理:")
        print("      npm config set proxy http://127.0.0.1:7890")
        return 1

    finally:
        # 清理
        if 'client' in locals() and hasattr(client, '_transport'):
            client._transport.stop()
            print("\n[*] 已关闭连接")

    return 0


if __name__ == "__main__":
    sys.exit(main())
