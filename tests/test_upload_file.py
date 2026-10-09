def test_upload_file_tool_exists():
    """Test that uploadFile MCP tool is registered."""
    import asyncio
    import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    tool_names = [tool.name for tool in tools]
    assert "uploadFile" in tool_names, f"uploadFile tool not found in {tool_names}"