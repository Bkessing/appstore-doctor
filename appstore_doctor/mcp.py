"""Entry point for `python3 -m appstore_doctor.mcp`.

The MCP server is a long-running JSON-RPC loop over stdio, not a one-shot
report like the CLI, so it gets its own entry point rather than a flag on
`__main__.py`'s argparse — bolting "read requests from stdin forever" onto a
parser built around "produce one report and exit" would make both harder to
follow for no real benefit. The implementation lives in mcp_server.py; this
file exists only so the module path reads the way `python3 -m package.mcp`
suggests it should.
"""

from .mcp_server import main

if __name__ == "__main__":
    main()
