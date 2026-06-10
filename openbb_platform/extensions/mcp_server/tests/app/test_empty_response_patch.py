"""Tests for the OpenAPITool empty-response (HTTP 204) patch.

openbb-core converts EmptyDataError into a bodyless HTTP 204; without the
patch, fastmcp's OpenAPITool returns no structured content and the MCP SDK
rejects the call with "outputSchema defined but no structured output
returned". The patch must turn such responses into a structured empty
result, while leaving real payloads untouched.
"""

# pylint: disable=protected-access

import pytest
from fastapi import FastAPI, Response
from fastmcp import FastMCP
from pydantic import BaseModel

from openbb_mcp_server.utils.empty_response_patch import (
    EMPTY_DATA_MESSAGE,
    patch_openapi_tool_empty_responses,
)


class OBBjectLike(BaseModel):
    """Minimal stand-in for openbb-core's OBBject response model."""

    results: list | None = None
    warnings: list | None = None


def _make_tool_server(handler, path: str, operation_id: str) -> FastMCP:
    app = FastAPI()
    app.get(path, response_model=OBBjectLike, operation_id=operation_id)(handler)
    return FastMCP.from_fastapi(app=app)


@pytest.mark.asyncio
async def test_204_returns_structured_empty_result():
    """A bodyless 204 must yield {"results": [], "warnings": [...]}."""
    patch_openapi_tool_empty_responses()

    def empty_endpoint():
        return Response(status_code=204)

    mcp = _make_tool_server(empty_endpoint, "/api/v1/economy/cpi", "economy_cpi")
    listed = await mcp.list_tools()
    tool = await mcp.get_tool(listed[0].name)
    assert tool.output_schema is not None

    result = await tool.run({})

    structured = result.structured_content
    assert structured is not None
    if "result" in structured and "results" not in structured:
        structured = structured["result"]
    assert structured["results"] == []
    assert structured["warnings"][0]["category"] == "EmptyDataError"
    assert structured["warnings"][0]["message"] == EMPTY_DATA_MESSAGE


@pytest.mark.asyncio
async def test_non_empty_response_passes_through():
    """Real JSON payloads must not be replaced by the fallback."""
    patch_openapi_tool_empty_responses()

    def data_endpoint():
        return OBBjectLike(results=[{"x": 1}])

    mcp = _make_tool_server(data_endpoint, "/api/v1/economy/ok", "economy_ok")
    listed = await mcp.list_tools()
    tool = await mcp.get_tool(listed[0].name)

    result = await tool.run({})

    assert result.structured_content is not None
    assert result.structured_content["results"] == [{"x": 1}]


def test_patch_is_idempotent():
    """Applying the patch twice must not double-wrap OpenAPITool.run."""
    # pylint: disable=import-outside-toplevel
    from fastmcp.server.providers.openapi import OpenAPITool

    patch_openapi_tool_empty_responses()
    first = OpenAPITool.run
    patch_openapi_tool_empty_responses()
    assert OpenAPITool.run is first
