#!/usr/bin/env python3
"""MCP Integration Demo - Connect to a real MCP server.

This demonstrates:
1. Connecting to @modelcontextprotocol/server-filesystem
2. Discovering tools from the MCP server
3. Calling MCP tools through the agent loop
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness.mcp import get_registry
from myharness.mcp.stdio import StdioTransport
from myharness.tools import assemble_tool_pool


def main():
    print("=" * 70)
    print("MCP Integration Demo")
    print("=" * 70)

    # Setup workspace
    workspace = Path(__file__).parent / "mcp_demo_workspace"
    workspace.mkdir(exist_ok=True)

    # Create test file
    (workspace / "demo.txt").write_text("Hello from MCP!\n", encoding="utf-8")

    print(f"\n[1] Workspace: {workspace}")
    print(f"    Test file created: demo.txt")

    # Connect to MCP server
    print("\n[2] Connecting to MCP filesystem server...")
    print("    Command: npx -y @modelcontextprotocol/server-filesystem")
    print(f"    Allowed directory: {workspace}")

    transport = StdioTransport(
        command=["npx", "-y", "@modelcontextprotocol/server-filesystem", str(workspace)],
        cwd=workspace,
    )

    registry = get_registry()
    registry.register_stdio_server("fs", transport)

    print(f"    [OK] Connected")
    print(f"    [OK] Tools registered: {len(registry.clients['fs'].tools)}")

    # Assemble tool pool
    print("\n[3] Assembling tool pool...")
    tools, handlers = assemble_tool_pool(workspace)

    # Count MCP tools
    mcp_tools = [t for t in tools if t["name"].startswith("mcp__fs__")]
    print(f"    Total tools: {len(tools)}")
    print(f"    MCP tools: {len(mcp_tools)}")

    # List MCP tools
    print("\n[4] Available MCP tools:")
    for tool in mcp_tools[:5]:  # Show first 5
        print(f"    - {tool['name']}")
        desc = tool.get("description", "")[:60]
        print(f"      {desc}...")

    # Test tool calls
    print("\n" + "=" * 70)
    print("Testing MCP Tool Calls")
    print("=" * 70)

    # Test 1: List directory
    print("\n[Test 1] mcp__fs__list_directory")
    handler = handlers.get("mcp__fs__list_directory")
    if handler:
        result = handler(path=".")
        print(f"Result:\n{result}")
    else:
        print("ERROR: Handler not found")

    # Test 2: Read file
    print("\n[Test 2] mcp__fs__read_file")
    handler = handlers.get("mcp__fs__read_file")
    if handler:
        result = handler(path="demo.txt")
        print(f"Result:\n{result}")
    else:
        print("ERROR: Handler not found")

    # Test 3: Write file
    print("\n[Test 3] mcp__fs__write_file")
    handler = handlers.get("mcp__fs__write_file")
    if handler:
        result = handler(path="output.txt", content="Written by MCP tool!")
        print(f"Result: {result}")

        # Verify
        content = (workspace / "output.txt").read_text(encoding="utf-8")
        print(f"Verification: {content}")
    else:
        print("ERROR: Handler not found")

    # Cleanup
    print("\n" + "=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print("\nNext steps:")
    print("1. Run myharness CLI: python -m myharness")
    print("2. Use connect_mcp('fs') to connect this server")
    print("3. All fs__* tools will be available to the agent")

    registry.cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
