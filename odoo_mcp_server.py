"""
Odoo MCP Server using JSON-RPC (json2 protocol)

This server provides MCP tools and resources for interacting with Odoo
using the JSON-RPC protocol via odoolib.

Environment Variables:
    ODOO_URL: Odoo server URL (default: http://localhost:8069)
    ODOO_DATABASE: Database name (default: odoo)
    ODOO_API_KEY: API key for authentication
    READONLY_MODE: Set to "true" to disable write operations (default: false)
    MCP_AUTH_TOKEN: Bearer token for HTTP/SSE transport authentication
        (optional; unset = no authentication, stdio is unaffected)
    UPLOAD_DIR: Directory that add_attachment's file_path mode is confined to
        (default: /shared/uploads). Set to "/" to allow any path.
    UPLOAD_MAX_BYTES: Max bytes for a single POST to the /upload endpoint
        (default: 25 MiB).
"""

__version__ = "1.0.0"

import argparse
import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import odoolib
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DATABASE = os.getenv("ODOO_DATABASE", "your_database_key_here")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "your_api_key_here")
READONLY_MODE = os.getenv("READONLY_MODE", "false").lower() == "true"
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN")

# add_attachment 的 file_path 模式會讀 MCP 主機的本地檔案，是本 server 唯一的
# 本機檔案讀取原語。若不設限，被 prompt injection 的 LLM 可用 file_path="/app/.env"
# 把 API key 等機密讀出、上傳成 Odoo 附件外洩（confused deputy）。因此把 file_path
# 限制在 UPLOAD_DIR 底下——預設對齊 compose 的 /shared/uploads 圖片傳遞通道。
# 純本機 stdio 若要放行任意路徑，明確設 UPLOAD_DIR=/ 自行放寬。
# （os.getenv(...) or ... 讓「未設」與「設為空字串」都落回預設，避免空字串 resolve 成 cwd。）
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR") or "/shared/uploads").resolve()

# /upload 端點單一檔案的大小上限（bytes），擋 disk-fill。預設 25 MiB。
UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES") or 25 * 1024 * 1024)

# prepare_upload 簽發的短效 upload token 有效期（秒）。
UPLOAD_TOKEN_TTL_SECONDS = 600


# =============================================================================
# Helper Functions
# =============================================================================


def resolve_upload_path(file_path: str) -> Path:
    """Resolve file_path and enforce that it stays inside UPLOAD_DIR.

    UPLOAD_DIR is the sole guard on add_attachment's local-file read: without
    it, a prompt-injected caller could read arbitrary host files (e.g. the
    server's own .env holding ODOO_API_KEY) and exfiltrate them as an Odoo
    attachment. resolve() collapses symlinks and '..', so a symlink planted
    inside UPLOAD_DIR that points outside it is also rejected.
    """
    base = UPLOAD_DIR.resolve()
    resolved = Path(file_path).resolve()
    if not resolved.is_relative_to(base):
        raise ToolError(
            f"file_path must resolve to a location inside UPLOAD_DIR ({base}); "
            f"refusing to read '{file_path}'. Move the file there, set UPLOAD_DIR "
            "to widen the allowed location, or pass the content via base64_data."
        )
    return resolved


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


def _sanitize_error_message(error: Exception) -> str:
    """Remove debug traceback from Odoo JSON-RPC error messages.

    Odoo errors often contain a JSON body with a "debug" field holding
    the full server-side traceback. This strips that field to reduce
    token usage and avoid leaking internal server paths.
    """
    msg = str(error)
    try:
        # Odoo RPC errors look like: "Unexpected status code 404: {json_body}"
        json_start = msg.index("{")
        prefix = msg[:json_start]
        body = json.loads(msg[json_start:])
        if isinstance(body, dict) and "debug" in body:
            body.pop("debug")
            return prefix + json.dumps(body, ensure_ascii=False)
    except (ValueError, json.JSONDecodeError):
        pass
    return msg


def handle_tool_errors(func):
    """Decorator to convert unhandled exceptions to ToolError.

    Ensures error messages pass through mask_error_details to the LLM,
    instead of being masked as generic "Internal error".
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            sanitized = _sanitize_error_message(e)
            raise ToolError(f"{func.__name__} failed: {sanitized}") from e

    return wrapper


def build_record_url(model: str, record_id: int) -> str:
    """Build Odoo record URL for direct browser access."""
    return f"{ODOO_URL.rstrip('/')}/odoo/{model}/{record_id}"


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
        # Parse URL using urlparse for robust handling (IPv6, paths, etc.)
        parsed = urlparse(url.rstrip("/"))

        # Determine protocol based on scheme
        protocol = "json2s" if parsed.scheme == "https" else "json2"

        # Extract host (hostname handles IPv6 correctly)
        host = parsed.hostname or parsed.netloc

        # Extract port with sensible defaults
        port = parsed.port or (443 if protocol == "json2s" else 8069)

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
        """Read records by IDs.

        Note: odoolib's read() returns dict for single ID, list for multiple IDs.
        This method normalizes the result to always return a list.
        """
        model_proxy = self.get_model(model)
        result = model_proxy.read(ids, fields) if fields else model_proxy.read(ids)

        # Normalize result: odoolib returns dict for single ID, list for multiple
        if isinstance(result, dict):
            return [result]
        return result

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

    def get_user_context(self) -> dict:
        """取得當前用戶的 context（包含 uid, lang, tz 等）"""
        return self.connection.get_user_context()

    def get_current_uid(self) -> int | None:
        """取得當前用戶 ID"""
        return self.get_user_context().get("uid")


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


# MCP_AUTH_TOKEN 為 opt-in 認證：設定後 HTTP/SSE 的 /mcp 端點要求
# Authorization: Bearer <token>，未帶或錯誤一律 401；stdio 模式不受 auth 影響。
# 必須在模組層級掛上（理由同 READONLY_MODE）：fastmcp run / dev 以 import
# 方式載入，不會執行 __main__。
# StaticTokenVerifier 官方標註「僅供開發測試」（token 明文存放），本專案接受此點：
# ODOO_API_KEY 本來就以明文存於同一份環境變數，shared secret 並未擴大暴露面。
# 注意 /health 是 custom route，不在 auth 保護範圍內（LB probe 需免認證）。
_auth = StaticTokenVerifier(tokens={MCP_AUTH_TOKEN: {"client_id": "odoo-mcp-client"}}) if MCP_AUTH_TOKEN else None

mcp = FastMCP(
    "Odoo MCP Server (JSON-RPC)",
    auth=_auth,
    mask_error_details=True,
    instructions=(
        "你只能操作 Odoo ERP 系統中的資料（銷售、財務、庫存、聯絡人等）。"
        "如果使用者的需求不在工具能力範圍內，請直接告知無法處理，不要反覆嘗試。"
        "不支援：系統管理、模組安裝、使用者權限管理、SQL 查詢、非 Odoo 相關任務。"
    ),
)


# =============================================================================
# Custom Routes
# =============================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for load balancers and monitoring."""
    return JSONResponse({"status": "healthy", "service": "odoo-mcp-server", "version": __version__})


def _make_upload_token() -> str | None:
    """從 MCP_AUTH_TOKEN 衍生短效 upload token（HMAC、自帶效期、無狀態）.

    prepare_upload 的回傳值會進 LLM context（transcript、對話摘要都留得下來），
    絕不能把 MCP_AUTH_TOKEN 本體交出去——那是整個 server 的 master key。改發衍生
    token：外洩最多換到「效期內對 /upload 寫檔」的權限。格式
    "<expiry_unix>.<hmac_sha256_hex>"，驗證只需重算 HMAC，server 不用保存任何狀態。
    """
    if not MCP_AUTH_TOKEN:
        return None
    expiry = str(int(time.time()) + UPLOAD_TOKEN_TTL_SECONDS)
    sig = hmac.new(MCP_AUTH_TOKEN.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _verify_upload_token(token: str) -> bool:
    """驗證 _make_upload_token 簽發的衍生 token：格式對、未過期、HMAC 相符."""
    if not MCP_AUTH_TOKEN:
        return False
    expiry, _, sig = token.partition(".")
    if not expiry.isdigit() or int(expiry) < time.time():
        return False
    expected = hmac.new(MCP_AUTH_TOKEN.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return secrets.compare_digest(sig, expected)


def _check_upload_auth(request) -> bool:
    """/upload 收兩種 Bearer：MCP_AUTH_TOKEN 本體，或 prepare_upload 簽發的短效衍生
    token（有設 MCP_AUTH_TOKEN 才驗，與 /mcp 一致；沒設＝不擋）.

    custom route 不受 MCP 的 StaticTokenVerifier 保護（/health 就是靠這點免認證），
    所以這個「會寫檔」的端點必須自己檢查 Bearer，否則等於開放任意人往主機寫檔。
    """
    if not MCP_AUTH_TOKEN:
        return True
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(token, MCP_AUTH_TOKEN) or _verify_upload_token(token)


def _safe_suffix(filename: str) -> str:
    """取原檔名的副檔名，僅接受短的 ASCII 英數（如 .png / .jpeg），否則回空字串.

    僅供顯示/辨識用途——實際磁碟檔名由 uuid 產生，副檔名不參與路徑安全。
    """
    tail = Path(filename).suffix[1:]
    if tail.isascii() and tail.isalnum() and len(tail) <= 10:
        return "." + tail.lower()
    return ""


@mcp.custom_route("/upload", methods=["POST"])
async def upload_file(request):
    """Out-of-band 檔案上傳，給跨機器的 add_attachment file_path 模式用.

    讓 client 用純 HTTP 把位元組直接推上來（不經 LLM token stream），server 存進
    UPLOAD_DIR 後回傳 file_path；client 再拿這個 path 呼叫 add_attachment，只有短路徑
    字串進 token stream。磁碟檔名由 uuid 產生，client 給的檔名絕不進入路徑，故無法逃出
    UPLOAD_DIR。有設 MCP_AUTH_TOKEN 時要求 Bearer token：master token 或 prepare_upload
    簽發的短效衍生 token 皆可（見 _check_upload_auth）。
    """
    if not _check_upload_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > UPLOAD_MAX_BYTES:
        return JSONResponse({"error": f"File exceeds UPLOAD_MAX_BYTES ({UPLOAD_MAX_BYTES})."}, status_code=413)

    try:
        form = await request.form(max_part_size=UPLOAD_MAX_BYTES)
    except MultiPartException:
        return JSONResponse({"error": "Malformed or oversized multipart upload."}, status_code=400)

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse({"error": "Missing multipart 'file' field."}, status_code=400)

    data = await upload.read()
    original_name = upload.filename or "upload"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{_safe_suffix(original_name)}"
    stored_path.write_bytes(data)

    return JSONResponse({"file_path": str(stored_path), "file_name": Path(original_name).name})


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
        attributes=[
            "type",
            "string",
            "help",
            "required",
            "readonly",
            "store",
            "selection",
            "comodel_name",
            "inverse_name",
            "domain",
        ],
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


@mcp.resource("odoo://user")
def get_current_user(client: OdooJsonRpcClient = Depends(get_shared_client)) -> str:
    """Get current logged-in user information."""
    uid = client.get_current_uid()
    if uid is None:
        return json.dumps({"error": "Not authenticated"})
    fields = get_safe_fields(client, "res.users")
    user_data = client.read("res.users", [uid], fields=fields)
    if user_data:
        user = user_data[0]
        user["_url"] = build_record_url("res.users", uid)
        return json.dumps(user, indent=2, ensure_ascii=False, default=format_datetime)
    return json.dumps({"error": "User not found"})


@mcp.resource("odoo://company")
def get_current_company(client: OdooJsonRpcClient = Depends(get_shared_client)) -> str:
    """Get current user's company information."""
    # 先取得當前用戶
    uid = client.get_current_uid()
    if uid is None:
        return json.dumps({"error": "Not authenticated"})
    user_data = client.read("res.users", [uid], fields=["company_id"])

    if user_data and user_data[0].get("company_id"):
        company_id = user_data[0]["company_id"][0]  # Many2one 返回 [id, name]
        fields = get_safe_fields(client, "res.company")
        company_data = client.read("res.company", [company_id], fields=fields)
        if company_data:
            company = company_data[0]
            company["_url"] = build_record_url("res.company", company_id)
            return json.dumps(company, indent=2, ensure_ascii=False, default=format_datetime)

    return json.dumps({"error": "Company not found"})


# =============================================================================
# Tools
# =============================================================================


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
@handle_tool_errors
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
    "selection",  # Selection 欄位的選項
    "comodel_name",  # Many2one/One2many/Many2many 關聯模型
    "inverse_name",  # One2many 反向欄位
    "domain",  # 關聯欄位的 domain
]


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
@handle_tool_errors
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


@mcp.tool(tags={"write"}, annotations={"destructiveHint": True})
@handle_tool_errors
def execute_method(
    model: str,
    method: str,
    args: list | None = None,
    kwargs: dict | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Execute any method on an Odoo model (general-purpose escape hatch).

    WARNING: This tool is NOT guarded by this server. Business methods
    (e.g. action_confirm, action_post, button_validate, toggle_active)
    can modify or irreversibly change data in Odoo. Before calling any
    method that changes state, make sure the user explicitly asked for it.

    Args:
        model: Model name (e.g., 'res.partner')
        method: Method name to execute
        args: Positional arguments for the method
        kwargs: Keyword arguments for the method

    Returns:
        JSON string with the method result

    Note:
        - 'unlink' is blocked here. Use the delete_record tool instead,
          which enforces a user confirmation step.
        - In READONLY_MODE, write tools are disabled at the module level
          (hidden from LLM and direct calls are rejected).
    """
    if method == "unlink":
        raise ToolError(
            "Method 'unlink' is not allowed via execute_method. "
            "Use the 'delete_record' tool instead, which requires "
            "user confirmation before irreversible deletion."
        )
    check_readonly_mode(method)
    args = args or []
    kwargs = kwargs or {}
    result = client.execute(model, method, *args, **kwargs)
    return json.dumps(result, indent=2, ensure_ascii=False, default=format_datetime)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
@handle_tool_errors
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


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
@handle_tool_errors
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


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
@handle_tool_errors
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


@mcp.tool(tags={"write"})
@handle_tool_errors
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
        In READONLY_MODE, this tool is disabled at registration time
        (hidden from LLM and direct calls are rejected).
    """
    result = client.create(model, values)
    if isinstance(values, list):
        ids = result if isinstance(result, list) else [result]
        return json.dumps(
            {
                "ids": ids,
                "count": len(ids),
                "success": True,
                "urls": [build_record_url(model, id) for id in ids],
            },
            indent=2,
        )
    # Single record creation - narrow type for type checker
    record_id = result if isinstance(result, int) else result[0]
    return json.dumps(
        {
            "id": record_id,
            "success": True,
            "url": build_record_url(model, record_id),
        },
        indent=2,
    )


@mcp.tool(tags={"write"}, annotations={"idempotentHint": True})
@handle_tool_errors
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
        In READONLY_MODE, this tool is disabled at registration time
        (hidden from LLM and direct calls are rejected).
    """
    result = client.write(model, ids, values)

    if result:
        return json.dumps(
            {
                "success": True,
                "updated_ids": ids,
                "urls": [build_record_url(model, id) for id in ids],
            },
            indent=2,
        )
    else:
        return json.dumps(
            {
                "success": False,
                "updated_ids": ids,
                "error": "Update operation failed",
            },
            indent=2,
        )


@mcp.tool(tags={"write", "destructive"}, annotations={"destructiveHint": True, "idempotentHint": True})
@handle_tool_errors
def delete_record(
    model: str,
    ids: list[int],
    confirm: bool = False,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Delete records from an Odoo model. IRREVERSIBLE operation.

    IMPORTANT: You MUST first call with confirm=False to show the user what will be deleted.
    Only set confirm=True AFTER the user explicitly approves the deletion.
    NEVER set confirm=True on the first call.

    Args:
        model: Model name (e.g., 'res.partner')
        ids: List of record IDs to delete
        confirm: Safety flag. Always call with False first, then True only after user approval.

    Returns:
        JSON string with success status or pending confirmation
    """
    if not confirm:
        return json.dumps(
            {
                "status": "error",
                "error": f"Deletion not confirmed. You must ask the user to confirm "
                f"before deleting {len(ids)} record(s) from {model}. "
                f"Set confirm=True only after user approval.",
                "details": {"model": model, "ids": ids},
                "warning": "This action is IRREVERSIBLE.",
            },
            ensure_ascii=False,
            indent=2,
        )
    result = client.unlink(model, ids)
    return json.dumps({"success": result, "deleted_ids": ids}, indent=2)


@mcp.tool(tags={"write"})
@handle_tool_errors
def prepare_upload() -> str:
    """
    Prepare an out-of-band upload of a CLIENT-side file to this server.

    Use this when the file to attach lives on the client machine and this MCP
    server is remote: pushing the bytes over plain HTTP keeps them out of the
    LLM token stream (much faster and cheaper than base64_data for large files).

    Workflow:
    1. Call this tool to get the endpoint info and a short-lived upload_token.
    2. POST the file (multipart field 'file') to <origin>/upload, where
       <origin> is the same scheme://host:port this MCP connection uses:
       curl -fsS -F "file=@/local/invoice.png" \\
            -H "Authorization: Bearer <upload_token>" <origin>/upload
       -> {"file_path": "/shared/uploads/<uuid>.png", "file_name": "invoice.png"}
    3. Call add_attachment(file_path=..., file_name=..., res_model=..., res_id=...).

    Returns:
        JSON string with endpoint, method, upload_token (null when the server
        has no MCP_AUTH_TOKEN configured — omit the Authorization header in
        that case), expires_in_seconds, and max_bytes.
    """
    token = _make_upload_token()
    return json.dumps(
        {
            "endpoint": "/upload",
            "base_url_hint": "Use the same scheme://host:port as this MCP connection.",
            "method": "POST multipart/form-data, file field name 'file'",
            "upload_token": token,
            "expires_in_seconds": UPLOAD_TOKEN_TTL_SECONDS if token else None,
            "max_bytes": UPLOAD_MAX_BYTES,
            "next_step": ("POST the file to obtain file_path, then call add_attachment(file_path=..., file_name=...)."),
        },
        indent=2,
    )


@mcp.tool(tags={"write"})
@handle_tool_errors
def add_attachment(
    file_path: str | None = None,
    base64_data: str | None = None,
    file_name: str | None = None,
    res_model: str | None = None,
    res_id: int | None = None,
    description: str | None = None,
    client: OdooJsonRpcClient = Depends(get_shared_client),
) -> str:
    """
    Upload a file attachment to Odoo via ir.attachment.

    Supports two input modes:
    - file_path: Read a local file from the MCP server's filesystem. The path
      must resolve to a location inside UPLOAD_DIR (default /shared/uploads);
      paths outside it are refused. If the file lives on the CLIENT machine
      and this server is remote, call prepare_upload first and POST the file
      to /upload to obtain a server-side file_path.
    - base64_data + file_name: Pass file content directly as base64 string.
      Use this when the file is not on disk (e.g., image uploaded via Discord).

    Args:
        file_path: Path to a local file the MCP server can read. Must resolve
                   to a location inside UPLOAD_DIR (default /shared/uploads);
                   paths outside it are refused. Mutually exclusive with base64_data.
        base64_data: File content as a base64-encoded string.
                     Mutually exclusive with file_path.
        file_name: Filename for the attachment (e.g., 'invoice.png').
                   Required with base64_data; optional with file_path (defaults to
                   the file's own name — handy to restore a real name when file_path
                   points at a server-generated /upload name).
        res_model: Model to attach to (e.g., 'account.move'). None for standalone attachment.
        res_id: Record ID to attach to. Required if res_model is provided.
        description: Optional description for the attachment.

    Returns:
        JSON string with attachment ID, URL, and linked record info.

    Note:
        In READONLY_MODE, this tool is disabled at registration time
        (hidden from LLM and direct calls are rejected).
    """
    if file_path and base64_data:
        raise ToolError("Provide either 'file_path' or 'base64_data', not both.")
    if not file_path and not base64_data:
        raise ToolError("Either 'file_path' or 'base64_data' must be provided.")

    if file_path:
        path = resolve_upload_path(file_path)
        if not path.is_file():
            raise ToolError(f"File not found: {file_path}")
        file_data = base64.b64encode(path.read_bytes()).decode("ascii")
        name = file_name or path.name
        file_size = path.stat().st_size
    else:
        if not file_name:
            raise ToolError("'file_name' is required when using 'base64_data'.")
        file_data = base64_data  # type: ignore[assignment]
        name = file_name
        file_size = len(base64.b64decode(base64_data))  # type: ignore[arg-type]

    values: dict[str, Any] = {
        "name": name,
        "datas": file_data,
    }
    if res_model:
        values["res_model"] = res_model
        if res_id:
            values["res_id"] = res_id
    if description:
        values["description"] = description

    attachment_id = client.create("ir.attachment", values)
    if isinstance(attachment_id, list):
        attachment_id = attachment_id[0]

    result: dict[str, Any] = {
        "id": attachment_id,
        "success": True,
        "file_name": name,
        "file_size": file_size,
        "url": build_record_url("ir.attachment", attachment_id),
    }
    if res_model and res_id:
        result["linked_to"] = {
            "model": res_model,
            "id": res_id,
            "url": build_record_url(res_model, res_id),
        }

    return json.dumps(result, indent=2)


# READONLY_MODE 的處理必須在模組層級（而非 __main__）：
# fastmcp run / fastmcp dev 以 import 方式載入本模組，不會執行 __main__。
# mcp.disable(tags={"write"}) 會同時「隱藏工具」並「拒絕直接呼叫」（Unknown tool）。
if READONLY_MODE:
    mcp.disable(tags={"write"})
    print("⚠️  READONLY_MODE is enabled. Write tools are disabled.", file=sys.stderr)


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

    if args.transport == "stdio":
        mcp.run()
    else:
        if not MCP_AUTH_TOKEN:
            print(
                "⚠️  MCP_AUTH_TOKEN is not set: the HTTP/SSE endpoint has NO authentication. "
                "Anyone who can reach this port gets full ODOO_API_KEY access. "
                "Set MCP_AUTH_TOKEN (with TLS) before exposing it beyond a trusted network.",
                file=sys.stderr,
            )
        mcp.run(transport=args.transport, host=args.host, port=args.port)
