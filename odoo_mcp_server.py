"""
Odoo MCP Server using JSON-RPC (json2 protocol)

This server provides MCP tools and resources for interacting with Odoo
using the JSON-RPC protocol via odoolib.

Environment Variables:
    ODOO_URL: Odoo server URL (default: http://localhost:8069)
    ODOO_DATABASE: Database name (default: odoo)
    ODOO_API_KEY: API key for authentication
    READONLY_MODE: Set to "true" to disable write operations (default: false)
"""

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import odoolib
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8019")
ODOO_DATABASE = os.getenv("ODOO_DATABASE", "your_database_key_here")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "your_api_key_here")
READONLY_MODE = os.getenv("READONLY_MODE", "false").lower() == "true"


# =============================================================================
# Helper Functions
# =============================================================================


def format_datetime(obj: Any) -> str:
    """Format datetime objects to ISO 8601 string."""
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, date):
        return obj.strftime("%Y-%m-%d")
    return str(obj)


# Write operations that should be blocked in READONLY_MODE
WRITE_METHODS = {"create", "write", "unlink", "copy"}

# Dangerous field types (excluded by default to avoid large base64 data)
DANGEROUS_FIELD_TYPES = {"binary", "image", "html"}


def check_readonly_mode(operation: str) -> None:
    """Check if operation is allowed in readonly mode."""
    if READONLY_MODE and operation in WRITE_METHODS:
        raise ToolError(
            f"Operation '{operation}' is not allowed in READONLY_MODE. "
            "Set READONLY_MODE=false to enable write operations."
        )


def build_record_url(model: str, record_id: int) -> str:
    """Build Odoo record URL for direct browser access."""
    return f"{ODOO_URL}/odoo/{model}/{record_id}"


def get_safe_fields(client: "OdooJsonRpcClient", model: str) -> list[str]:
    """取得排除危險欄位後的安全欄位列表

    使用 ORM fields_get() 取得欄位資訊，
    自動排除 binary、image、html 類型的欄位，
    避免回傳過大的 base64 資料。

    Args:
        client: Odoo JSON-RPC 客戶端
        model: 模型名稱 (e.g., 'res.partner')

    Returns:
        安全欄位名稱列表
    """
    # 使用 fields_get() 取得欄位類型資訊（效能較佳）
    fields_data = client.fields_get(model, attributes=["type"])
    return [
        field_name
        for field_name, field_info in fields_data.items()
        if field_info.get("type") not in DANGEROUS_FIELD_TYPES
    ]


# =============================================================================
# Odoo JSON-RPC Client
# =============================================================================

@dataclass
class OdooJsonRpcClient:
    """Wrapper for odoolib connection using JSON-RPC (json2) protocol."""

    connection: Any

    @classmethod
    def connect(cls, url: str, database: str, api_key: str) -> "OdooJsonRpcClient":
        """Create a new connection to Odoo using JSON-RPC."""
        # Parse host and port from URL
        url = url.rstrip("/")
        if url.startswith("https://"):
            host = url[8:]
            protocol = "json2+ssl"
        elif url.startswith("http://"):
            host = url[7:]
            protocol = "json2"
        else:
            host = url
            protocol = "json2"

        # Handle port
        port = 8069
        if ":" in host:
            host, port_str = host.rsplit(":", 1)
            port = int(port_str)

        connection = odoolib.get_connection(
            hostname=host,
            port=port,
            database=database,
            login="api",  # Using API key auth
            password=api_key,
            protocol=protocol,
        )
        return cls(connection=connection)

    def get_model(self, model_name: str):
        """Get a model proxy object."""
        return self.connection.get_model(model_name)

    def search(self, model: str, domain: list, limit: int = 100, offset: int = 0) -> list[int]:
        """Search for records matching the domain."""
        model_proxy = self.get_model(model)
        return model_proxy.search(domain, limit=limit, offset=offset)

    def search_count(self, model: str, domain: list) -> int:
        """Count records matching the domain."""
        model_proxy = self.get_model(model)
        return model_proxy.search_count(domain)

    def read(self, model: str, ids: list[int], fields: list[str] | None = None) -> list[dict]:
        """Read records by IDs."""
        model_proxy = self.get_model(model)
        if fields:
            return model_proxy.read(ids, fields)
        return model_proxy.read(ids)

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        """Search and read records in one call."""
        model_proxy = self.get_model(model)
        kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
        if fields:
            kwargs["fields"] = fields
        if order:
            kwargs["order"] = order
        return model_proxy.search_read(domain, **kwargs)

    def create(self, model: str, values: dict | list[dict]) -> int | list[int]:
        """Create new record(s). Supports single dict or list of dicts for batch creation."""
        model_proxy = self.get_model(model)
        return model_proxy.create(values)

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        """Update existing records."""
        model_proxy = self.get_model(model)
        return model_proxy.write(ids, values)

    def unlink(self, model: str, ids: list[int]) -> bool:
        """Delete records."""
        model_proxy = self.get_model(model)
        return model_proxy.unlink(ids)

    def execute(self, model: str, method: str, *args, **kwargs) -> Any:
        """Execute any method on a model."""
        model_proxy = self.get_model(model)
        return getattr(model_proxy, method)(*args, **kwargs)

    def fields_get(
        self,
        model: str,
        allfields: list[str] | None = None,
        attributes: list[str] | None = None,
    ) -> dict[str, dict]:
        """使用 ORM fields_get() 取得欄位定義。

        Args:
            model: 模型名稱 (e.g., 'res.partner')
            allfields: 指定要取得的欄位名稱列表，None 表示全部
            attributes: 指定要返回的屬性列表，None 表示全部

        Returns:
            欄位定義字典，key 為欄位名稱
        """
        model_proxy = self.get_model(model)
        kwargs = {}
        if allfields:
            kwargs["allfields"] = allfields
        if attributes:
            kwargs["attributes"] = attributes
        return model_proxy.fields_get(**kwargs)


# =============================================================================
# MCP Server Setup
# =============================================================================

# 全域共享連線（懶載入單例模式）
_client: OdooJsonRpcClient | None = None


def get_shared_client() -> OdooJsonRpcClient:
    """取得共享的 Odoo 客戶端（單例模式）

    連線只會在首次呼叫時建立，之後重複使用同一連線。
    """
    global _client
    if _client is None:
        _client = OdooJsonRpcClient.connect(ODOO_URL, ODOO_DATABASE, ODOO_API_KEY)
    return _client


mcp = FastMCP("Odoo MCP Server (JSON-RPC)", mask_error_details=True)


# =============================================================================
# Resources
# =============================================================================

@mcp.resource("odoo://models")
def list_models_resource(client: OdooJsonRpcClient = Depends(get_shared_client)) -> str:
    """List all available Odoo models."""
    records = client.search_read(
        "ir.model",
        [],
        fields=["model", "name"],
        limit=500,
    )
    return json.dumps(records, indent=2, ensure_ascii=False)


@mcp.resource("odoo://model/{model_name}")
def get_model_fields(model_name: str, client: OdooJsonRpcClient = Depends(get_shared_client)) -> str:
    """Get field information for a specific model using ORM fields_get()."""
    # 使用 fields_get() 取得更完整的欄位資訊
    fields_data = client.fields_get(
        model_name,
        attributes=["type", "string", "help", "required", "readonly", "store",
                   "selection", "comodel_name", "inverse_name", "domain"],
    )

    # 轉換為列表格式
    result = []
    for field_name, field_info in fields_data.items():
        field_dict = {"name": field_name}
        field_dict.update(field_info)
        result.append(field_dict)

    # 按欄位名稱排序
    result.sort(key=lambda x: x["name"])

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.resource("odoo://record/{model_name}/{record_id}")
def get_record(model_name: str, record_id: int, client: OdooJsonRpcClient = Depends(get_shared_client)) -> str:
    """Get a single record by ID (auto-excludes dangerous fields like binary/image/html)."""
    # 自動排除危險欄位（binary、image、html）
    fields = get_safe_fields(client, model_name)
    records = client.read(model_name, [int(record_id)], fields=fields)
    if records:
        return json.dumps(records[0], indent=2, ensure_ascii=False, default=format_datetime)
    return json.dumps({"error": "Record not found"})


# =============================================================================
# Tools
# =============================================================================

@mcp.tool()
def list_models(
    name_filter: str | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    List all available Odoo models.

    Args:
        name_filter: Optional filter for model name (e.g., 'sale' to find sale-related models)

    Returns:
        JSON string with model names and descriptions
    """
    domain = []
    if name_filter:
        domain = ["|", ("model", "ilike", name_filter), ("name", "ilike", name_filter)]
    records = client.search_read(
        "ir.model",
        domain,
        fields=["model", "name"],
        limit=500,
    )
    return json.dumps(records, indent=2, ensure_ascii=False)


# 預設返回的欄位屬性
DEFAULT_FIELD_ATTRIBUTES = [
    "type",
    "string",
    "help",
    "required",
    "readonly",
    "store",
    "selection",      # Selection 欄位的選項
    "comodel_name",   # Many2one/One2many/Many2many 關聯模型
    "inverse_name",   # One2many 反向欄位
    "domain",         # 關聯欄位的 domain
]


@mcp.tool()
def get_fields(
    model: str,
    field_filter: str | None = None,
    fields: list[str] | None = None,
    attributes: list[str] | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Get field information for an Odoo model using ORM fields_get().

    Args:
        model: Model name (e.g., 'res.partner')
        field_filter: Optional filter for field name (e.g., 'name' to find name-related fields)
        fields: Specific field names to retrieve (None = all fields)
        attributes: Field attributes to return (None = default attributes including
                   type, string, help, required, readonly, store, selection,
                   comodel_name, inverse_name, domain)

    Returns:
        JSON array of field definitions. Each field includes:
        - name: Field name
        - type: Field type (char, integer, many2one, selection, etc.)
        - string: Human-readable label
        - required: Whether field is required
        - readonly: Whether field is readonly
        - selection: List of [value, label] pairs (for selection fields)
        - comodel_name: Related model name (for relational fields)
    """
    # 使用預設屬性或自訂屬性
    attrs = attributes or DEFAULT_FIELD_ATTRIBUTES

    # 取得欄位資訊
    fields_data = client.fields_get(model, allfields=fields, attributes=attrs)

    # 轉換為列表格式，並加入欄位名稱
    result = []
    for field_name, field_info in fields_data.items():
        # 套用 field_filter 過濾
        if field_filter and field_filter.lower() not in field_name.lower():
            continue

        field_dict = {"name": field_name}
        field_dict.update(field_info)
        result.append(field_dict)

    # 按欄位名稱排序
    result.sort(key=lambda x: x["name"])

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def execute_method(
    model: str,
    method: str,
    args: list | None = None,
    kwargs: dict | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Execute any method on an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        method: Method name to execute
        args: Positional arguments for the method
        kwargs: Keyword arguments for the method

    Returns:
        JSON string with the method result

    Note:
        In READONLY_MODE, write methods (create, write, unlink, copy) are blocked.
    """
    check_readonly_mode(method)
    args = args or []
    kwargs = kwargs or {}
    result = client.execute(model, method, *args, **kwargs)
    return json.dumps(result, indent=2, ensure_ascii=False, default=format_datetime)


@mcp.tool()
def search_records(
    model: str,
    domain: list | None = None,
    fields: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Search for records in an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        domain: Odoo search domain (list of conditions). Examples:
            - Simple: [["name", "=", "John"]]
            - Multiple (AND): [["is_company", "=", True], ["active", "=", True]]
            - OR condition: ["|", ["name", "ilike", "test"], ["email", "ilike", "test"]]
            - any (Odoo 19+): [["order_line", "any", [["product_uom_qty", ">", 5]]]]
            - Operators: =, !=, >, >=, <, <=, like, ilike, in, not in, child_of, any
        fields: Fields to return (None = auto-exclude dangerous fields like binary/image/html)
        limit: Maximum number of records
        offset: Number of records to skip
        order: Sort order (e.g., 'name asc', 'create_date desc')

    Returns:
        JSON with records, total count, limit, and offset.
        Each record includes a '_url' field for direct browser access.
    """
    domain = domain or []

    # 當 fields=None 時，自動排除危險欄位（binary、image、html）
    if fields is None:
        fields = get_safe_fields(client, model)

    records = client.search_read(model, domain, fields=fields, limit=limit, offset=offset, order=order)
    total = client.search_count(model, domain)

    # Add URL to each record
    for record in records:
        if "id" in record:
            record["_url"] = build_record_url(model, record["id"])

    result = {
        "records": records,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    return json.dumps(result, indent=2, ensure_ascii=False, default=format_datetime)


@mcp.tool()
def count_records(
    model: str,
    domain: list | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Count records in an Odoo model matching the domain.

    Args:
        model: Model name (e.g., 'res.partner')
        domain: Odoo search domain (list of conditions). Examples:
            - Simple: [["active", "=", True]]
            - Multiple (AND): [["is_company", "=", True], ["country_id", "=", 1]]
            - OR condition: ["|", ["type", "=", "contact"], ["type", "=", "invoice"]]
            - any (Odoo 19+): [["order_line", "any", [["product_uom_qty", ">", 5]]]]
            - Operators: =, !=, >, >=, <, <=, like, ilike, in, not in, child_of, any

    Returns:
        JSON string with the count
    """
    domain = domain or []
    count = client.search_count(model, domain)
    return json.dumps({"model": model, "count": count}, indent=2)


@mcp.tool()
def read_records(
    model: str,
    ids: list[int],
    fields: list[str] | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Read specific records by their IDs.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to read
        fields: Fields to return (None = auto-exclude dangerous fields like binary/image/html)

    Returns:
        JSON string with the records. Each record includes a '_url' field
        for direct browser access to that record.
    """
    # 當 fields=None 時，自動排除危險欄位（binary、image、html）
    if fields is None:
        fields = get_safe_fields(client, model)

    records = client.read(model, ids, fields=fields)

    # Add URL to each record
    for record in records:
        if "id" in record:
            record["_url"] = build_record_url(model, record["id"])

    return json.dumps(records, indent=2, ensure_ascii=False, default=format_datetime)


@mcp.tool()
def create_record(
    model: str,
    values: dict | list[dict],
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Create new record(s) in an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        values: Dictionary of field values, or list of dicts for batch creation

    Returns:
        JSON string containing:
        - Single creation: {"id": int, "success": bool, "url": str}
        - Batch creation: {"ids": list[int], "count": int, "success": bool, "urls": list[str]}

        The 'url' field provides direct browser access to the created record(s).

    Note:
        In READONLY_MODE, this operation is blocked.
    """
    check_readonly_mode("create")
    result = client.create(model, values)
    if isinstance(values, list):
        ids = result if isinstance(result, list) else [result]
        return json.dumps({
            "ids": ids,
            "count": len(ids),
            "success": True,
            "urls": [build_record_url(model, id) for id in ids],
        }, indent=2)
    return json.dumps({
        "id": result,
        "success": True,
        "url": build_record_url(model, result),
    }, indent=2)


@mcp.tool()
def update_record(
    model: str,
    ids: list[int],
    values: dict,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Update existing records in an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to update
        values: Dictionary of field values to update

    Returns:
        JSON string with:
        - success: Boolean indicating operation result
        - updated_ids: List of record IDs
        - urls: List of browser URLs (only if success=True)
        - error: Error message (only if success=False)

    Note:
        In READONLY_MODE, this operation is blocked.
    """
    check_readonly_mode("write")
    result = client.write(model, ids, values)

    if result:
        return json.dumps({
            "success": True,
            "updated_ids": ids,
            "urls": [build_record_url(model, id) for id in ids],
        }, indent=2)
    else:
        return json.dumps({
            "success": False,
            "updated_ids": ids,
            "error": "Update operation failed",
        }, indent=2)


@mcp.tool()
def delete_record(
    model: str,
    ids: list[int],
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Delete records from an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to delete

    Returns:
        JSON string with success status
    """
    check_readonly_mode("unlink")
    result = client.unlink(model, ids)
    return json.dumps({"success": result, "deleted_ids": ids}, indent=2)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Odoo MCP Server (JSON-RPC)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport layer: stdio (CLI), http (Streamable HTTP), sse (Server-Sent Events)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP/SSE transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP/SSE transport")
    args = parser.parse_args()

    if READONLY_MODE:
        print("⚠️  READONLY_MODE is enabled. Write operations are disabled.")

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
