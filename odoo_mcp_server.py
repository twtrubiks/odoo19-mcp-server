"""
Odoo MCP Server using JSON-RPC (json2 protocol)

This server provides MCP tools and resources for interacting with Odoo
using the JSON-RPC protocol via odoolib.

Environment Variables:
    ODOO_URL: Odoo server URL (default: http://localhost:8069)
    ODOO_DATABASE: Database name (default: odoo)
    ODOO_API_KEY: API key for authentication
"""

import os
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import odoolib
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.dependencies import Depends

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8019")
ODOO_DATABASE = os.getenv("ODOO_DATABASE", "your_database_key_here")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "your_api_key_here")


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
    ) -> list[dict]:
        """Search and read records in one call."""
        model_proxy = self.get_model(model)
        kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
        if fields:
            kwargs["fields"] = fields
        return model_proxy.search_read(domain, **kwargs)

    def create(self, model: str, values: dict) -> int:
        """Create a new record."""
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


# =============================================================================
# MCP Server Setup
# =============================================================================

@asynccontextmanager
async def get_odoo_client():
    """依賴注入: 獲取 Odoo 客戶端"""
    client = OdooJsonRpcClient.connect(ODOO_URL, ODOO_DATABASE, ODOO_API_KEY)
    yield client


mcp = FastMCP("Odoo MCP Server (JSON-RPC)")


# =============================================================================
# Resources
# =============================================================================

@mcp.resource("odoo://models")
def list_models(client: OdooJsonRpcClient = Depends(get_odoo_client)) -> str:
    """List all available Odoo models."""
    records = client.search_read(
        "ir.model",
        [],
        fields=["model", "name"],
        limit=500,
    )
    return json.dumps(records, indent=2, ensure_ascii=False)


@mcp.resource("odoo://model/{model_name}")
def get_model_fields(model_name: str, client: OdooJsonRpcClient = Depends(get_odoo_client)) -> str:
    """Get field information for a specific model."""
    records = client.search_read(
        "ir.model.fields",
        [("model", "=", model_name)],
        fields=["name", "field_description", "ttype", "required", "readonly"],
        limit=200,
    )
    return json.dumps(records, indent=2, ensure_ascii=False)


@mcp.resource("odoo://record/{model_name}/{record_id}")
def get_record(model_name: str, record_id: int, client: OdooJsonRpcClient = Depends(get_odoo_client)) -> str:
    """Get a single record by ID."""
    records = client.read(model_name, [int(record_id)])
    if records:
        return json.dumps(records[0], indent=2, ensure_ascii=False, default=str)
    return json.dumps({"error": "Record not found"})


# =============================================================================
# Tools
# =============================================================================

@mcp.tool()
def execute_method(
    model: str,
    method: str,
    args: list | None = None,
    kwargs: dict | None = None,
    client: OdooJsonRpcClient = Depends(get_odoo_client),
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
    """
    args = args or []
    kwargs = kwargs or {}
    result = client.execute(model, method, *args, **kwargs)
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def search_records(
    model: str,
    domain: list | None = None,
    fields: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    client: OdooJsonRpcClient = Depends(get_odoo_client),
) -> str:
    """
    Search for records in an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        domain: Search domain (e.g., [['is_company', '=', True]])
        fields: Fields to return (None for all)
        limit: Maximum number of records
        offset: Number of records to skip

    Returns:
        JSON string with matching records
    """
    domain = domain or []
    records = client.search_read(model, domain, fields=fields, limit=limit, offset=offset)
    return json.dumps(records, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def count_records(
    model: str,
    domain: list | None = None,
    client: OdooJsonRpcClient = Depends(get_odoo_client),
) -> str:
    """
    Count records in an Odoo model matching the domain.

    Args:
        model: Model name (e.g., 'res.partner')
        domain: Search domain (e.g., [['is_company', '=', True]])

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
    client: OdooJsonRpcClient = Depends(get_odoo_client),
) -> str:
    """
    Read specific records by their IDs.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to read
        fields: Fields to return (None for all)

    Returns:
        JSON string with the records
    """
    records = client.read(model, ids, fields=fields)
    return json.dumps(records, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def create_record(
    model: str,
    values: dict,
    client: OdooJsonRpcClient = Depends(get_odoo_client),
) -> str:
    """
    Create a new record in an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        values: Dictionary of field values

    Returns:
        JSON string with the new record ID
    """
    record_id = client.create(model, values)
    return json.dumps({"id": record_id, "success": True}, indent=2)


@mcp.tool()
def update_record(
    model: str,
    ids: list[int],
    values: dict,
    client: OdooJsonRpcClient = Depends(get_odoo_client),
) -> str:
    """
    Update existing records in an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to update
        values: Dictionary of field values to update

    Returns:
        JSON string with success status
    """
    result = client.write(model, ids, values)
    return json.dumps({"success": result, "updated_ids": ids}, indent=2)


@mcp.tool()
def delete_record(
    model: str,
    ids: list[int],
    client: OdooJsonRpcClient = Depends(get_odoo_client),
) -> str:
    """
    Delete records from an Odoo model.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to delete

    Returns:
        JSON string with success status
    """
    result = client.unlink(model, ids)
    return json.dumps({"success": result, "deleted_ids": ids}, indent=2)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    mcp.run()
