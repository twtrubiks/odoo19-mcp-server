"""Tests for odoo_mcp_server — 只測有實際邏輯、容易藏 bug 的地方."""

import asyncio
import hashlib
import hmac
import importlib
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

import odoo_mcp_server
from odoo_mcp_server import (
    OdooJsonRpcClient,
    _check_upload_auth,
    _make_upload_token,
    _safe_suffix,
    _sanitize_error_message,
    _verify_upload_token,
    add_attachment,
    create_record,
    delete_record,
    execute_method,
    handle_tool_errors,
    prepare_upload,
    resolve_upload_path,
    search_records,
)

# =============================================================================
# 1. connect() URL 解析 — 分支多，使用者輸入格式千變萬化
# =============================================================================


class TestConnect:
    """測試 OdooJsonRpcClient.connect() 的 URL 解析邏輯."""

    @patch("odoo_mcp_server.odoolib.get_connection")
    def test_http_default_port(self, mock_get_conn):
        """http 沒給 port → 預設 8069."""
        mock_get_conn.return_value = MagicMock()
        OdooJsonRpcClient.connect("http://myserver", "db", "key")
        mock_get_conn.assert_called_once_with(
            hostname="myserver",
            port=8069,
            database="db",
            login="api",
            password="key",
            protocol="json2",
        )

    @patch("odoo_mcp_server.odoolib.get_connection")
    def test_https_default_port(self, mock_get_conn):
        """https 沒給 port → 預設 443，protocol 為 json2s."""
        mock_get_conn.return_value = MagicMock()
        OdooJsonRpcClient.connect("https://myserver", "db", "key")
        mock_get_conn.assert_called_once_with(
            hostname="myserver",
            port=443,
            database="db",
            login="api",
            password="key",
            protocol="json2s",
        )

    @patch("odoo_mcp_server.odoolib.get_connection")
    def test_explicit_port(self, mock_get_conn):
        """有明確給 port → 使用指定的 port."""
        mock_get_conn.return_value = MagicMock()
        OdooJsonRpcClient.connect("http://myserver:8080", "db", "key")
        mock_get_conn.assert_called_once_with(
            hostname="myserver",
            port=8080,
            database="db",
            login="api",
            password="key",
            protocol="json2",
        )

    @patch("odoo_mcp_server.odoolib.get_connection")
    def test_trailing_slash_stripped(self, mock_get_conn):
        """URL 尾巴的 / 不應影響 hostname."""
        mock_get_conn.return_value = MagicMock()
        OdooJsonRpcClient.connect("http://myserver:8069/", "db", "key")
        mock_get_conn.assert_called_once_with(
            hostname="myserver",
            port=8069,
            database="db",
            login="api",
            password="key",
            protocol="json2",
        )


# =============================================================================
# 2. read() dict → list 正規化 — odoolib 回傳不一致的防禦邏輯
# =============================================================================


class TestReadNormalization:
    """測試 read() 一定回傳 list，不管 odoolib 回 dict 還是 list."""

    def _make_client(self, read_return_value):
        mock_proxy = MagicMock()
        mock_proxy.read.return_value = read_return_value
        client = OdooJsonRpcClient(connection=MagicMock())
        return client, mock_proxy

    def test_single_record_returns_dict_normalized_to_list(self):
        """odoolib 傳單筆回 dict → 應正規化為 list."""
        client, mock_proxy = self._make_client({"id": 1, "name": "Alice"})
        with patch.object(client, "get_model", return_value=mock_proxy):
            result = client.read("res.partner", [1], fields=["name"])
        assert isinstance(result, list)
        assert result == [{"id": 1, "name": "Alice"}]

    def test_multiple_records_returns_list_unchanged(self):
        """odoolib 傳多筆回 list → 原樣回傳."""
        data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        client, mock_proxy = self._make_client(data)
        with patch.object(client, "get_model", return_value=mock_proxy):
            result = client.read("res.partner", [1, 2], fields=["name"])
        assert result == data

    def test_read_without_fields(self):
        """不傳 fields → 呼叫 read(ids) 而非 read(ids, fields)."""
        client, mock_proxy = self._make_client([{"id": 1, "name": "Alice"}])
        with patch.object(client, "get_model", return_value=mock_proxy):
            client.read("res.partner", [1])
        mock_proxy.read.assert_called_once_with([1])


# =============================================================================
# 3. create_record() 單筆 vs 批次 — 兩層 isinstance 判斷，邊界情況多
# =============================================================================


class TestCreateRecord:
    """測試 create_record 的單筆/批次分支邏輯."""

    def _make_mock_client(self, create_return):
        mock_client = MagicMock()
        mock_client.create.return_value = create_return
        return mock_client

    def test_single_creation_returns_id(self):
        """單筆建立 → 回傳 {id, success, url}."""
        client = self._make_mock_client(42)
        result = json.loads(create_record(model="res.partner", values={"name": "Test"}, client=client))
        assert result["id"] == 42
        assert result["success"] is True
        assert "url" in result

    def test_batch_creation_returns_ids(self):
        """批次建立 → 回傳 {ids, count, success, urls}."""
        client = self._make_mock_client([10, 11, 12])
        values = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        result = json.loads(create_record(model="res.partner", values=values, client=client))
        assert result["ids"] == [10, 11, 12]
        assert result["count"] == 3
        assert result["success"] is True
        assert len(result["urls"]) == 3

    def test_batch_creation_single_id_returned(self):
        """批次建立但 odoo 回傳單一 int（而非 list）→ 應包成 list."""
        client = self._make_mock_client(99)
        values = [{"name": "Only"}]
        result = json.loads(create_record(model="res.partner", values=values, client=client))
        assert result["ids"] == [99]
        assert result["count"] == 1


# =============================================================================
# 4. delete_record() confirm 閘門 — 安全機制，壞了就直接刪使用者資料
# =============================================================================


class TestDeleteRecord:
    """測試 delete_record 的 confirm 安全機制."""

    def test_confirm_false_blocks_deletion(self):
        """confirm=False → 不應呼叫 client，回傳錯誤."""
        mock_client = MagicMock()
        result = json.loads(delete_record(model="res.partner", ids=[1, 2], confirm=False, client=mock_client))
        assert result["status"] == "error"
        assert "confirm" in result["error"].lower()
        mock_client.unlink.assert_not_called()

    def test_confirm_true_executes_deletion(self):
        """confirm=True → 呼叫 client.unlink."""
        mock_client = MagicMock()
        mock_client.unlink.return_value = True
        result = json.loads(delete_record(model="res.partner", ids=[1, 2], confirm=True, client=mock_client))
        assert result["success"] is True
        assert result["deleted_ids"] == [1, 2]
        mock_client.unlink.assert_called_once_with("res.partner", [1, 2])

    def test_default_confirm_is_false(self):
        """不傳 confirm → 預設 False，不執行刪除."""
        mock_client = MagicMock()
        result = json.loads(delete_record(model="res.partner", ids=[1], client=mock_client))
        assert result["status"] == "error"
        mock_client.unlink.assert_not_called()


# =============================================================================
# 5. execute_method + READONLY_MODE — 唯一擋住萬用入口的安全網
# =============================================================================


class TestExecuteMethodReadonly:
    """測試 execute_method 在 READONLY_MODE 下阻擋寫入操作."""

    @pytest.mark.parametrize("method", ["create", "write", "unlink", "copy"])
    def test_readonly_blocks_write_methods(self, monkeypatch, method):
        """READONLY_MODE=True 時，execute_method 應擋住所有寫入方法."""
        monkeypatch.setattr("odoo_mcp_server.READONLY_MODE", True)
        mock_client = MagicMock()

        with pytest.raises(ToolError, match="not allowed"):
            execute_method(model="res.partner", method=method, client=mock_client)
        mock_client.execute.assert_not_called()

    def test_readonly_allows_read_methods(self, monkeypatch):
        """READONLY_MODE=True 時，讀取方法應正常放行."""
        monkeypatch.setattr("odoo_mcp_server.READONLY_MODE", True)
        mock_client = MagicMock()
        mock_client.execute.return_value = [1, 2, 3]

        result = json.loads(execute_method(model="res.partner", method="search", args=[[]], client=mock_client))
        assert result == [1, 2, 3]
        mock_client.execute.assert_called_once()


class TestExecuteMethodUnlinkBlock:
    """測試 execute_method 攔下 unlink，不能繞過 delete_record 的 confirm 閘門."""

    def test_unlink_blocked_and_redirected(self):
        """method='unlink' → ToolError 指路 delete_record，不碰 client."""
        mock_client = MagicMock()
        with pytest.raises(ToolError, match="delete_record"):
            execute_method(model="res.partner", method="unlink", args=[[1, 2]], client=mock_client)
        mock_client.execute.assert_not_called()

    def test_unlink_blocked_even_in_readonly(self, monkeypatch):
        """unlink 的攔截先於 readonly 檢查，兩種模式下都擋."""
        monkeypatch.setattr("odoo_mcp_server.READONLY_MODE", True)
        mock_client = MagicMock()
        with pytest.raises(ToolError, match="delete_record"):
            execute_method(model="res.partner", method="unlink", args=[[1]], client=mock_client)
        mock_client.execute.assert_not_called()

    def test_other_write_methods_still_allowed(self, monkeypatch):
        """非 readonly 時，unlink 以外的寫入方法（如 create）仍放行."""
        monkeypatch.setattr("odoo_mcp_server.READONLY_MODE", False)
        mock_client = MagicMock()
        mock_client.execute.return_value = 42
        result = json.loads(
            execute_method(model="res.partner", method="create", args=[{"name": "x"}], client=mock_client)
        )
        assert result == 42
        mock_client.execute.assert_called_once()


# =============================================================================
# 5.5 add_attachment 的 file_path 限制在 UPLOAD_DIR（防任意檔案讀取外洩）
# =============================================================================


class TestUploadDirConfinement:
    """file_path 只能讀 UPLOAD_DIR 底下的檔案，越界（含 symlink）一律拒絕."""

    def test_path_inside_upload_dir_allowed(self, tmp_path, monkeypatch):
        """UPLOAD_DIR 內的真實檔案 → resolve_upload_path 回傳解析後路徑."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        f = tmp_path / "invoice.png"
        f.write_bytes(b"data")
        assert resolve_upload_path(str(f)) == f.resolve()

    def test_path_outside_upload_dir_rejected(self, tmp_path, monkeypatch):
        """UPLOAD_DIR 外的路徑（如 /etc/passwd）→ ToolError，且不洩漏檔案是否存在."""
        upload = tmp_path / "uploads"
        upload.mkdir()
        outside = tmp_path / "secret.env"
        outside.write_bytes(b"ODOO_API_KEY=leak")
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", upload)
        with pytest.raises(ToolError, match="UPLOAD_DIR"):
            resolve_upload_path(str(outside))

    def test_traversal_escape_rejected(self, tmp_path, monkeypatch):
        """'..' 逃逸到 UPLOAD_DIR 外 → resolve() 正規化後被擋."""
        upload = tmp_path / "uploads"
        upload.mkdir()
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", upload)
        with pytest.raises(ToolError, match="UPLOAD_DIR"):
            resolve_upload_path(str(upload / ".." / "secret.env"))

    def test_symlink_pointing_outside_rejected(self, tmp_path, monkeypatch):
        """UPLOAD_DIR 內指向外部的 symlink → resolve() 解掉後仍在外，被擋."""
        upload = tmp_path / "uploads"
        upload.mkdir()
        secret = tmp_path / "secret.env"
        secret.write_bytes(b"ODOO_API_KEY=leak")
        link = upload / "innocent.png"
        link.symlink_to(secret)
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", upload)
        with pytest.raises(ToolError, match="UPLOAD_DIR"):
            resolve_upload_path(str(link))

    def test_add_attachment_rejects_outside_path_before_touching_client(self, tmp_path, monkeypatch):
        """add_attachment 對越界 file_path 先擋下，不讀檔也不碰 client."""
        upload = tmp_path / "uploads"
        upload.mkdir()
        outside = tmp_path / "secret.env"
        outside.write_bytes(b"ODOO_API_KEY=leak")
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", upload)
        mock_client = MagicMock()
        with pytest.raises(ToolError, match="UPLOAD_DIR"):
            add_attachment(file_path=str(outside), client=mock_client)
        mock_client.create.assert_not_called()


# =============================================================================
# 6. handle_tool_errors — 統一錯誤處理 decorator
# =============================================================================


class TestHandleToolErrors:
    """測試 handle_tool_errors decorator 的錯誤轉換邏輯."""

    def test_normal_return_passes_through(self):
        """正常回傳值不受影響."""

        @handle_tool_errors
        def good_func():
            return "ok"

        assert good_func() == "ok"

    def test_generic_exception_converted_to_tool_error(self):
        """一般 Exception → 轉為 ToolError，保留原始訊息."""

        @handle_tool_errors
        def bad_func():
            raise RuntimeError("model not found")

        with pytest.raises(ToolError, match="bad_func failed: model not found"):
            bad_func()

    def test_tool_error_not_wrapped(self):
        """已經是 ToolError → 直接 re-raise，不會被包兩層."""

        @handle_tool_errors
        def readonly_func():
            raise ToolError("not allowed in READONLY_MODE")

        with pytest.raises(ToolError, match="not allowed in READONLY_MODE"):
            readonly_func()

    def test_original_exception_chained(self):
        """原始 Exception 應保留在 __cause__ 中."""

        @handle_tool_errors
        def failing_func():
            raise ValueError("bad value")

        with pytest.raises(ToolError) as exc_info:
            failing_func()
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_search_records_rpc_error(self):
        """實際 tool：search_records RPC 錯誤 → ToolError."""
        mock_client = MagicMock()
        mock_client.search_read.side_effect = Exception("Object res.partnerr doesn't exist")

        with pytest.raises(ToolError, match="search_records failed.*doesn't exist"):
            search_records(model="res.partnerr", client=mock_client)

    def test_execute_method_rpc_error(self):
        """實際 tool：execute_method RPC 錯誤 → ToolError."""
        mock_client = MagicMock()
        mock_client.execute.side_effect = ConnectionError("Connection refused")

        with pytest.raises(ToolError, match="execute_method failed.*Connection refused"):
            execute_method(model="res.partner", method="search", args=[[]], client=mock_client)


# =============================================================================
# 7. _sanitize_error_message — 去除 Odoo debug traceback
# =============================================================================


class TestSanitizeErrorMessage:
    """測試 _sanitize_error_message 的 debug 欄位移除邏輯."""

    def test_strips_debug_from_odoo_rpc_error(self):
        """Odoo RPC 錯誤 → 移除 debug 欄位，保留其他資訊."""
        body = {
            "name": "werkzeug.exceptions.NotFound",
            "message": "the model 'res.partnersa' does not exist",
            "arguments": ["the model 'res.partnersa' does not exist", 404],
            "context": {},
            "debug": "Traceback (most recent call last):\n  File ...\n  ...",
        }
        error = Exception(f"Unexpected status code 404: {json.dumps(body)}")
        result = _sanitize_error_message(error)

        assert "debug" not in result
        assert "Traceback" not in result
        assert "the model 'res.partnersa' does not exist" in result
        assert "Unexpected status code 404:" in result
        assert "werkzeug.exceptions.NotFound" in result

    def test_no_debug_field_unchanged(self):
        """JSON body 沒有 debug 欄位 → 原樣回傳."""
        body = {"name": "SomeError", "message": "something went wrong"}
        error = Exception(f"Status 500: {json.dumps(body)}")
        result = _sanitize_error_message(error)
        assert result == str(error)

    def test_plain_exception_unchanged(self):
        """非 JSON 的一般 Exception → 原樣回傳."""
        error = ConnectionError("Connection refused")
        result = _sanitize_error_message(error)
        assert result == "Connection refused"

    def test_non_dict_json_unchanged(self):
        """JSON 是 array 而非 dict → 原樣回傳."""
        error = Exception("Some prefix: [1, 2, 3]")
        result = _sanitize_error_message(error)
        assert result == str(error)

    def test_integration_with_decorator(self):
        """decorator 整合測試：Odoo 風格錯誤經過 decorator 後 debug 被移除."""
        body = {
            "message": "field 'namee' does not exist",
            "debug": "Traceback ...\n  very long traceback ...",
        }

        @handle_tool_errors
        def failing_tool():
            raise Exception(f"Unexpected status code 400: {json.dumps(body)}")

        with pytest.raises(ToolError) as exc_info:
            failing_tool()
        error_msg = str(exc_info.value)
        assert "field 'namee' does not exist" in error_msg
        assert "Traceback" not in error_msg


# =============================================================================
# 8. READONLY_MODE 停用寫入工具 — 模組層級 mcp.disable(tags={"write"})
#    （守住停用機制的回歸：模組層級停用，各啟動方式皆生效）
# =============================================================================

WRITE_TOOLS = {"create_record", "update_record", "delete_record", "execute_method", "add_attachment", "prepare_upload"}


@pytest.fixture
def readonly_module():
    """設定 READONLY_MODE=true 後 reload 模組（模擬 fastmcp run 的 import 式載入），
    結束後還原."""
    os.environ["READONLY_MODE"] = "true"
    importlib.reload(odoo_mcp_server)
    yield odoo_mcp_server
    del os.environ["READONLY_MODE"]
    importlib.reload(odoo_mcp_server)


class TestReadonlyDisablesWriteTools:
    """測試 READONLY_MODE 下，寫入工具在 MCP 層被隱藏且拒絕呼叫."""

    def test_write_tools_hidden_from_list(self, readonly_module):
        """tools/list 不應出現任何 write tag 的工具，唯讀工具照常存在."""

        async def _list():
            async with Client(readonly_module.mcp) as client:
                return {tool.name for tool in await client.list_tools()}

        names = asyncio.run(_list())
        assert not (names & WRITE_TOOLS)
        assert "search_records" in names

    def test_write_tool_call_rejected(self, readonly_module):
        """直接呼叫被停用的工具 → 拒絕（不只是隱藏）."""

        async def _call():
            async with Client(readonly_module.mcp) as client:
                await client.call_tool("create_record", {"model": "res.partner", "values": {}})

        with pytest.raises(ToolError, match="Unknown tool"):
            asyncio.run(_call())

    def test_normal_mode_shows_write_tools(self):
        """未設定 READONLY_MODE → 寫入工具正常可見."""

        async def _list():
            async with Client(odoo_mcp_server.mcp) as client:
                return {tool.name for tool in await client.list_tools()}

        names = asyncio.run(_list())
        assert names >= WRITE_TOOLS


# =============================================================================
# 9. MCP_AUTH_TOKEN — HTTP transport 的 Bearer 認證（opt-in 安全機制）
# =============================================================================

TEST_TOKEN = "test-secret-token"

# MCP initialize 握手請求（不需連 Odoo，適合驗證 auth 層）
MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def auth_enabled_module():
    """設定 MCP_AUTH_TOKEN 後 reload 模組（模擬 fastmcp run 的 import 式載入），
    結束後還原為無認證狀態."""
    os.environ["MCP_AUTH_TOKEN"] = TEST_TOKEN
    importlib.reload(odoo_mcp_server)
    yield odoo_mcp_server
    del os.environ["MCP_AUTH_TOKEN"]
    importlib.reload(odoo_mcp_server)


def _http_request(mcp_server, path, method="POST", token=None):
    """對 FastMCP 的 HTTP app 發一個請求，回傳 response."""
    app = mcp_server.http_app()
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def _run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                if method == "GET":
                    return await client.get(path, headers=headers)
                return await client.post(path, json=MCP_INIT_BODY, headers=headers)

    return asyncio.run(_run())


class TestMcpAuthToken:
    """測試 MCP_AUTH_TOKEN 的 opt-in 認證：設了才驗、沒設不擋、/health 永遠豁免."""

    def test_no_token_env_means_no_auth(self):
        """未設定 MCP_AUTH_TOKEN → auth 為 None（向下相容，無認證）."""
        assert odoo_mcp_server.mcp.auth is None

    def test_token_env_enables_static_verifier(self, auth_enabled_module):
        """設定 MCP_AUTH_TOKEN → 模組層級掛上 StaticTokenVerifier
        （import 式載入即生效，不依賴 __main__）."""
        assert isinstance(auth_enabled_module.mcp.auth, StaticTokenVerifier)

    def test_mcp_endpoint_rejects_missing_token(self, auth_enabled_module):
        """啟用認證後，/mcp 未帶 token → 401."""
        response = _http_request(auth_enabled_module.mcp, "/mcp")
        assert response.status_code == 401

    def test_mcp_endpoint_accepts_valid_token(self, auth_enabled_module):
        """啟用認證後，/mcp 帶正確 token → initialize 握手成功."""
        response = _http_request(auth_enabled_module.mcp, "/mcp", token=TEST_TOKEN)
        assert response.status_code == 200

    def test_health_endpoint_bypasses_auth(self, auth_enabled_module):
        """/health 是 custom route，不受 auth 保護（LB probe 需免認證）."""
        response = _http_request(auth_enabled_module.mcp, "/health", method="GET")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


# =============================================================================
# 10. /upload 端點 + add_attachment file_name 覆寫（跨機器 out-of-band 傳檔）
# =============================================================================


class TestSafeSuffix:
    """_safe_suffix 只接受短的 ASCII 英數副檔名，其餘一律回空字串."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("invoice.png", ".png"),
            ("IMG.JPEG", ".jpeg"),  # 轉小寫
            ("noext", ""),
            ("archive.tar.gz", ".gz"),
            ("weird.評", ""),  # 非 ASCII
            ("x." + "a" * 20, ""),  # 過長
            ("bad.sh script", ""),  # 含空白非英數
        ],
    )
    def test_cases(self, filename, expected):
        assert _safe_suffix(filename) == expected


class TestUploadAuth:
    """_check_upload_auth：設了 MCP_AUTH_TOKEN 才驗 Bearer，沒設一律放行."""

    def _req(self, authorization=None):
        req = MagicMock()
        req.headers = {"authorization": authorization} if authorization is not None else {}
        return req

    def test_no_token_configured_allows_all(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        assert _check_upload_auth(self._req()) is True

    def test_correct_bearer_passes(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        assert _check_upload_auth(self._req("Bearer secret")) is True

    def test_wrong_token_rejected(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        assert _check_upload_auth(self._req("Bearer nope")) is False

    def test_missing_header_rejected(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        assert _check_upload_auth(self._req()) is False

    def test_derived_upload_token_passes(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        assert _check_upload_auth(self._req(f"Bearer {_make_upload_token()}")) is True


class TestUploadTokenHelpers:
    """prepare_upload 簽發的短效 token：HMAC 衍生、無狀態驗證、過期/竄改一律拒絕."""

    def _sign(self, key: bytes, expiry: str) -> str:
        return hmac.new(key, expiry.encode(), hashlib.sha256).hexdigest()

    def test_make_and_verify_roundtrip(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        token = _make_upload_token()
        assert token is not None
        assert _verify_upload_token(token) is True

    def test_expired_token_rejected(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        expiry = str(int(time.time()) - 1)
        assert _verify_upload_token(f"{expiry}.{self._sign(b'secret', expiry)}") is False

    def test_tampered_expiry_rejected(self, monkeypatch):
        """竄改 expiry 延長效期 → HMAC 對不上，拒絕."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        token = _make_upload_token()
        assert token is not None
        expiry, _, sig = token.partition(".")
        assert _verify_upload_token(f"{int(expiry) + 9999}.{sig}") is False

    def test_wrong_key_rejected(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        expiry = str(int(time.time()) + 600)
        assert _verify_upload_token(f"{expiry}.{self._sign(b'other-key', expiry)}") is False

    def test_garbage_rejected(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        assert _verify_upload_token("not-a-token") is False

    def test_no_auth_token_configured(self, monkeypatch):
        """未設 MCP_AUTH_TOKEN → 不簽發也不驗證（/upload 本來就不擋）."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        assert _make_upload_token() is None
        assert _verify_upload_token("123.abc") is False


class TestPrepareUpload:
    """prepare_upload 工具：回傳端點用法；有設 MCP_AUTH_TOKEN 才附短效 token，且絕不含 master token."""

    def test_with_auth_token(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        body = json.loads(prepare_upload())
        assert body["endpoint"] == "/upload"
        assert _verify_upload_token(body["upload_token"]) is True
        assert body["expires_in_seconds"] == odoo_mcp_server.UPLOAD_TOKEN_TTL_SECONDS
        assert "secret" not in json.dumps(body)

    def test_without_auth_token(self, monkeypatch):
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        body = json.loads(prepare_upload())
        assert body["upload_token"] is None
        assert body["expires_in_seconds"] is None


def _upload_request(mcp_server, data=b"IMAGE", filename="pic.png", token=None, field="file", as_form=False):
    """對 FastMCP 的 HTTP app POST /upload，預設送 multipart 檔案；as_form=True 送一般表單欄位."""
    app = mcp_server.http_app()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def _run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                if as_form:
                    return await client.post("/upload", data={field: "x"}, headers=headers)
                return await client.post("/upload", files={field: (filename, data)}, headers=headers)

    return asyncio.run(_run())


class TestUploadEndpoint:
    """/upload：認證、寫入 UPLOAD_DIR、檔名淨化、大小上限的端到端行為."""

    def test_upload_without_auth_writes_file(self, tmp_path, monkeypatch):
        """未設 token → 上傳成功，位元組寫進 UPLOAD_DIR，file_path 落在其中且保留副檔名."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        resp = _upload_request(odoo_mcp_server.mcp, data=b"IMAGE", filename="invoice.png")
        assert resp.status_code == 200
        body = resp.json()
        stored = Path(body["file_path"])
        assert stored.parent == tmp_path
        assert stored.suffix == ".png"
        assert stored.read_bytes() == b"IMAGE"
        assert body["file_name"] == "invoice.png"

    def test_upload_requires_token_when_configured(self, tmp_path, monkeypatch):
        """設了 token 但沒帶 → 401，且不寫檔（custom route 不受 MCP 認證保護，靠自檢）."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        resp = _upload_request(odoo_mcp_server.mcp, token=None)
        assert resp.status_code == 401
        assert list(tmp_path.iterdir()) == []

    def test_upload_accepts_correct_token(self, tmp_path, monkeypatch):
        """設了 token 且帶對 → 200."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        resp = _upload_request(odoo_mcp_server.mcp, token="secret")
        assert resp.status_code == 200

    def test_upload_accepts_derived_token(self, tmp_path, monkeypatch):
        """帶 prepare_upload 簽發的短效衍生 token → 200（master token 不用出場）."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        resp = _upload_request(odoo_mcp_server.mcp, token=_make_upload_token())
        assert resp.status_code == 200

    def test_missing_file_field_rejected(self, tmp_path, monkeypatch):
        """沒有 multipart 'file' 欄位 → 400."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        resp = _upload_request(odoo_mcp_server.mcp, field="notfile", as_form=True)
        assert resp.status_code == 400

    def test_oversized_upload_rejected(self, tmp_path, monkeypatch):
        """超過 UPLOAD_MAX_BYTES → 拒絕且不寫檔."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_MAX_BYTES", 4)
        resp = _upload_request(odoo_mcp_server.mcp, data=b"way too big")
        assert resp.status_code in (400, 413)
        assert list(tmp_path.iterdir()) == []


class TestAddAttachmentFileNameOverride:
    """add_attachment 的 file_path 模式支援 file_name 覆寫（把 /upload 的 uuid 名還原成原名）."""

    def test_file_name_override_used(self, tmp_path, monkeypatch):
        """帶 file_name → Odoo 附件名用 file_name."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        f = tmp_path / "abcdef123456.png"
        f.write_bytes(b"IMAGE")
        mock_client = MagicMock()
        mock_client.create.return_value = 7
        add_attachment(file_path=str(f), file_name="invoice.png", client=mock_client)
        assert mock_client.create.call_args.args[1]["name"] == "invoice.png"

    def test_defaults_to_disk_name(self, tmp_path, monkeypatch):
        """未帶 file_name → 用磁碟檔名（既有行為不變）."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        f = tmp_path / "invoice.png"
        f.write_bytes(b"IMAGE")
        mock_client = MagicMock()
        mock_client.create.return_value = 7
        add_attachment(file_path=str(f), client=mock_client)
        assert mock_client.create.call_args.args[1]["name"] == "invoice.png"
