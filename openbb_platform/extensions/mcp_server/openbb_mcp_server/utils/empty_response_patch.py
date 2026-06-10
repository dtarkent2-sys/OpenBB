"""Convert empty HTTP responses (204 / empty-body 2xx) into structured MCP output.

OpenBB's REST layer (openbb-core `exception_handlers`) converts
``EmptyDataError`` into a bodyless HTTP 204. fastmcp's ``OpenAPITool.run``
treats any 2xx as success and tries ``response.json()``; an empty body raises
``json.JSONDecodeError`` so it falls back to ``ToolResult(content="")`` with
no structured content. Because OpenBB tools declare an ``outputSchema``, the
MCP SDK then fails the call with:

    Output validation error: outputSchema defined but no structured output returned

which turns every legitimately-empty query (e.g. ``economy_cpi`` with a
start_date beyond the available data) into an opaque error.

This module wraps ``OpenAPITool.run`` at the class level: when the tool has
an output schema but the HTTP response produced no structured content and no
text body, it returns a well-formed empty result instead::

    {"results": [], "warnings": [{"category": "EmptyDataError", ...}]}

Non-empty text bodies are passed through untouched so real (non-JSON)
responses are never masked.
"""

from __future__ import annotations

from typing import Any

EMPTY_DATA_MESSAGE = (
    "No data found for the given query. Try adjusting the parameters."
)
EMPTY_DATA_WARNING = {
    "category": "EmptyDataError",
    "message": EMPTY_DATA_MESSAGE,
}

_PATCH_FLAG = "_openbb_empty_response_patch"


def patch_openapi_tool_empty_responses() -> None:
    """Idempotently wrap ``OpenAPITool.run`` with an empty-response fallback."""
    # pylint: disable=import-outside-toplevel
    from fastmcp.server.providers.openapi import OpenAPITool
    from fastmcp.tools.tool import ToolResult

    if getattr(OpenAPITool, _PATCH_FLAG, False):
        return

    original_run = OpenAPITool.run

    async def run_with_empty_fallback(
        self, arguments: dict[str, Any]
    ) -> ToolResult:
        result = await original_run(self, arguments)

        # Only intervene when the SDK would reject the result: an output
        # schema is declared but the call produced no structured content.
        if self.output_schema is None:
            return result
        if getattr(result, "structured_content", None) is not None:
            return result

        # Pass through non-empty text bodies (e.g. plain-text responses);
        # only a 204 / empty-body 2xx qualifies as "no data".
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text and text.strip():
                return result

        payload: dict[str, Any] = {
            "results": [],
            "warnings": [dict(EMPTY_DATA_WARNING)],
        }
        structured = (
            {"result": payload}
            if self.output_schema.get("x-fastmcp-wrap-result")
            else payload
        )
        return ToolResult(content=EMPTY_DATA_MESSAGE, structured_content=structured)

    OpenAPITool.run = run_with_empty_fallback
    setattr(OpenAPITool, _PATCH_FLAG, True)
