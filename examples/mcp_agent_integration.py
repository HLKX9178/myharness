""" Agent Loop  MCP 


1.  MCP filesystem server
2.  MCP  agent_loop
3.  MCP 


-  @modelcontextprotocol/server-filesystem
  npm install -g @modelcontextprotocol/server-filesystem
"""

import sys
from pathlib import Path

#  myharness 
sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness import config
from myharness.mcp.stdio import create_stdio_client
from myharness.mcp.registry import get_registry


def main():
    """ MCP  agent loop"""
    print("=" * 60)
    print("MCP + Agent Loop ")
    print("=" * 60)

    # 1. 
    workspace = Path(__file__).parent / "mcp_agent_workspace"
    workspace.mkdir(exist_ok=True)

    print(f"\n[*] : {workspace}")

    # 
    (workspace / "data.txt").write_text("Some data", encoding="utf-8")
    (workspace / "README.md").write_text("# Test Project", encoding="utf-8")

    print("[+] ")

    # 2.  MCP server
    print("\n[*]  MCP filesystem server...")

    try:
        client = create_stdio_client(
            name="fs",
            command=[
                "npx",
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(workspace.absolute()),
            ],
        )

        print("[+] ")

        # 3.  registry
        registry = get_registry()
        registry.add_client(client)

        print(f"[+]  registry: {len(client.tools)}")

        # 4.  + MCP
        from myharness.tools import assemble_tool_pool

        tools, handlers = assemble_tool_pool(workspace)

        # 
        builtin_count = len([t for t in tools if not t['name'].startswith('fs__')])
        mcp_count = len([t for t in tools if t['name'].startswith('fs__')])

        print(f"\n[*] :")
        print(f"   : {builtin_count}")
        print(f"   MCP : {mcp_count}")
        print(f"   : {len(tools)}")

        # 5.  MCP 
        print("\n[*] MCP :")
        for tool in client.tools:
            print(f"   - fs__{tool['name']}: {tool.get('description', '')[:60]}")

        # 6. 
        print("\n" + "=" * 60)
        print("[*] ")
        print("=" * 60)

        #  handlers  agent_loop 
        print("\n1   fs__list_directory...")
        handler = handlers.get("fs__list_directory")
        if handler:
            result = handler(path=".")
            print(f"   :\n{result[:200]}")
        else:
            print("   :  handler")

        print("\n2   fs__read_file...")
        handler = handlers.get("fs__read_file")
        if handler:
            result = handler(path="README.md")
            print(f"   :\n{result}")
        else:
            print("   :  handler")

        # 7.  agent_loop 
        print("\n" + "=" * 60)
        print("[*]  Agent ")
        print("=" * 60)

        print("\n: ")
        print("Agent: [ fs__list_directory]")

        handler = handlers.get("fs__list_directory")
        if handler:
            result = handler(path=".")
            print(f"Agent :\n{result}")

        print("\n:  data.txt ")
        print("Agent: [ fs__read_file]")

        handler = handlers.get("fs__read_file")
        if handler:
            result = handler(path="data.txt")
            print(f"Agent :\n{result}")

        print("\n:  output.txt 'Hello MCP!'")
        print("Agent: [ fs__write_file]")

        handler = handlers.get("fs__write_file")
        if handler:
            result = handler(path="output.txt", content="Hello MCP!")
            print(f"Agent :\n{result}")

            # 
            output_file = workspace / "output.txt"
            if output_file.exists():
                content = output_file.read_text(encoding="utf-8")
                print(f"[+] : {content}")

        print("\n" + "=" * 60)
        print("[+] MCP  Agent Loop ")
        print("=" * 60)

        print("\n[*] :")
        print("   1.  myharness CLI: python -m myharness")
        print("   2. : connect_mcp('fs')")
        print("   3.  fs__* ")

    except Exception as exc:
        print(f"\n[!] : {exc}")
        print("\n[*] :")
        print("   1.  Node.js")
        print("   2.  MCP server:")
        print("      npm install -g @modelcontextprotocol/server-filesystem")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # 
        if 'client' in locals() and hasattr(client, '_transport'):
            client._transport.stop()
            print("\n[*]  MCP ")

    return 0


if __name__ == "__main__":
    sys.exit(main())
