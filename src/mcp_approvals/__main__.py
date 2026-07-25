"""Allow ``python -m mcp_approvals`` to start the stdio server."""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    main()
