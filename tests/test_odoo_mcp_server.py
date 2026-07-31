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

import odoo_mcp_server
from odoo_mcp_server import (
    AuthenticationError,
    OdooJsonRpcClient,
    OdooPassthroughVerifier,
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


# =============================================================================
# 2b. kwargs-only 呼叫 — positional args 會觸發 odoolib 的 /doc-bearer 內省，
#     該路由需要 Technical Documentation 群組，一般使用者會 403
# =============================================================================


class TestKwargsOnlyProxyCalls:
    """釘住不變式：wrapper 對 model proxy 的呼叫絕不帶 positional args."""

    def _client_with_proxy(self):
        mock_proxy = MagicMock()
        client = OdooJsonRpcClient(connection=MagicMock())
        return client, mock_proxy

    def test_wrappers_never_pass_positional_args(self):
        client, proxy = self._client_with_proxy()
        proxy.read.return_value = []
        with patch.object(client, "get_model", return_value=proxy):
            client.search("res.partner", [("id", ">", 0)], limit=5, offset=2)
            client.search_count("res.partner", [])
            client.read("res.partner", [1, 2], fields=["name"])
            client.read("res.partner", [1, 2])
            client.search_read("res.partner", [], fields=["name"], order="id")
            client.create("res.partner", {"name": "A"})
            client.write("res.partner", [1], {"name": "B"})
            client.unlink("res.partner", [1])
            client.fields_get("res.partner", attributes=["type"])

        for name in (
            "search",
            "search_count",
            "read",
            "search_read",
            "create",
            "write",
            "unlink",
            "fields_get",
        ):
            for call in getattr(proxy, name).call_args_list:
                assert call.args == (), f"{name} 傳了 positional args: {call.args}"

    def test_kwarg_names_match_odoo_orm_signatures(self):
        """kwargs 名稱打錯 Odoo 會回 422，這裡釘住 ORM 簽名的參數名."""
        client, proxy = self._client_with_proxy()
        with patch.object(client, "get_model", return_value=proxy):
            client.create("res.partner", {"name": "A"})
            client.write("res.partner", [1], {"name": "B"})
            client.unlink("res.partner", [1])

        assert proxy.create.call_args.kwargs == {"vals_list": {"name": "A"}}
        assert proxy.write.call_args.kwargs == {"ids": [1], "vals": {"name": "B"}}
        assert proxy.unlink.call_args.kwargs == {"ids": [1]}


class TestExecuteIntrospection403:
    """execute() 收到 /doc-bearer 內省 403 時要翻成可行動的錯誤訊息."""

    def _make_403(self, url):
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        return httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    def test_doc_bearer_403_becomes_permission_error(self):
        client = OdooJsonRpcClient(connection=MagicMock())
        proxy = MagicMock()
        proxy.action_confirm.side_effect = self._make_403("http://odoo:8069/doc-bearer/sale.order.json")
        with (
            patch.object(client, "get_model", return_value=proxy),
            pytest.raises(PermissionError, match="Technical Documentation"),
        ):
            client.execute("sale.order", "action_confirm", [1])

    def test_other_403_is_reraised_unchanged(self):
        client = OdooJsonRpcClient(connection=MagicMock())
        proxy = MagicMock()
        proxy.action_confirm.side_effect = self._make_403("http://odoo:8069/json/2/sale.order/action_confirm")
        with (
            patch.object(client, "get_model", return_value=proxy),
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.execute("sale.order", "action_confirm", [1])


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
    # 還原成空字串而非 del：del 之後 reload 會讓 load_dotenv 從本機 .env 重新注入
    os.environ["MCP_AUTH_TOKEN"] = ""
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
# 10. /upload 端點（跨機器 out-of-band 傳檔）
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


def _upload_request(mcp_server, data=b"IMAGE", filename="pic.png", token=None):
    """對 FastMCP 的 HTTP app POST /upload（multipart 檔案），回傳 response."""
    app = mcp_server.http_app()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def _run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post("/upload", files={"file": (filename, data)}, headers=headers)

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

    def test_upload_accepts_derived_token(self, tmp_path, monkeypatch):
        """帶 prepare_upload 簽發的短效衍生 token → 200（master token 不用出場）."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        resp = _upload_request(odoo_mcp_server.mcp, token=_make_upload_token())
        assert resp.status_code == 200

    def test_oversized_upload_rejected(self, tmp_path, monkeypatch):
        """超過 UPLOAD_MAX_BYTES → 拒絕且不寫檔."""
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_DIR", tmp_path)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_MAX_BYTES", 4)
        resp = _upload_request(odoo_mcp_server.mcp, data=b"way too big")
        assert resp.status_code in (400, 413)
        assert list(tmp_path.iterdir()) == []


# =============================================================================
# 11. 多 user 模式（MCP_MULTIUSER）— pass-through 認證與 per-user client
# =============================================================================


@pytest.fixture
def clear_user_caches():
    """清空多 user 驗證快取，避免測試間互相汙染."""
    odoo_mcp_server._user_cache.clear()
    odoo_mcp_server._neg_cache.clear()
    yield
    odoo_mcp_server._user_cache.clear()
    odoo_mcp_server._neg_cache.clear()


def _fake_odoo_client(uid=7, login="alice@example.com", name="Alice"):
    """模擬一個驗證成功的 OdooJsonRpcClient."""
    fake = MagicMock()
    fake.get_current_uid.return_value = uid
    fake.read.return_value = [{"login": login, "name": name}]
    return fake


class TestPassthroughVerifier:
    """OdooPassthroughVerifier：static fallback、問 Odoo 驗證、正/負快取."""

    def _verify(self, token):
        return asyncio.run(OdooPassthroughVerifier().verify_token(token))

    def test_static_token_maps_to_env_admin(self, monkeypatch, clear_user_caches):
        """MCP_AUTH_TOKEN 在多 user 模式仍可用，對應 env-admin 身分."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "master")
        access = self._verify("master")
        assert access is not None
        assert access.client_id == odoo_mcp_server.ENV_ADMIN_CLIENT_ID

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_valid_odoo_key_returns_identity(self, mock_connect, monkeypatch, clear_user_caches):
        """有效的 Odoo API key → AccessToken 帶該 user 的 login 與 uid."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        mock_connect.return_value = _fake_odoo_client()
        access = self._verify("alice-api-key")
        assert access is not None
        assert access.client_id == "alice@example.com"
        assert access.claims["uid"] == 7
        mock_connect.assert_called_once_with(odoo_mcp_server.ODOO_URL, odoo_mcp_server.ODOO_DATABASE, "alice-api-key")

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_valid_key_cached(self, mock_connect, monkeypatch, clear_user_caches):
        """TTL 內第二次驗證直接命中快取，不再打 Odoo."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        mock_connect.return_value = _fake_odoo_client()
        self._verify("alice-api-key")
        self._verify("alice-api-key")
        assert mock_connect.call_count == 1

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_invalid_key_negative_cached(self, mock_connect, monkeypatch, clear_user_caches):
        """401 → None 且進負快取：連續嘗試不會每次都打 Odoo（暴力破解跳板防護）."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        fake = MagicMock()
        fake.get_current_uid.side_effect = AuthenticationError("bad key")
        mock_connect.return_value = fake
        assert self._verify("bad-key") is None
        assert self._verify("bad-key") is None
        assert mock_connect.call_count == 1

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_transient_error_not_cached(self, mock_connect, monkeypatch, clear_user_caches):
        """網路錯誤 → None 但不進負快取，下一次會重試（別把合法 key 鎖在門外）."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        fake = MagicMock()
        fake.get_current_uid.side_effect = ConnectionError("odoo down")
        mock_connect.return_value = fake
        assert self._verify("alice-api-key") is None
        assert self._verify("alice-api-key") is None
        assert mock_connect.call_count == 2

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_identity_read_failure_falls_back_to_uid(self, mock_connect, monkeypatch, clear_user_caches):
        """res.users read 失敗不影響認證（uid 已驗過），client_id 退回 uid-N."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        fake = MagicMock()
        fake.get_current_uid.return_value = 7
        fake.read.side_effect = Exception("no permission")
        mock_connect.return_value = fake
        access = self._verify("alice-api-key")
        assert access is not None
        assert access.client_id == "uid-7"

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_revoked_key_evicts_user_cache_entry(self, mock_connect, monkeypatch, clear_user_caches):
        """快取過期後重驗、key 已撤銷 → 舊 entry 從 _user_cache 移除，不永久殘留."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        mock_connect.return_value = _fake_odoo_client()
        self._verify("alice-api-key")
        assert len(odoo_mcp_server._user_cache) == 1
        entry = next(iter(odoo_mcp_server._user_cache.values()))
        entry.checked_at -= odoo_mcp_server._VERIFY_TTL_SECONDS + 1
        revoked = MagicMock()
        revoked.get_current_uid.side_effect = AuthenticationError("revoked")
        mock_connect.return_value = revoked
        assert self._verify("alice-api-key") is None
        assert odoo_mcp_server._user_cache == {}

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_stale_user_entries_pruned_on_insert(self, mock_connect, monkeypatch, clear_user_caches):
        """過期 entry（換 key 後不會再被讀到）在下一次成功寫入時被清掉."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        mock_connect.return_value = _fake_odoo_client()
        self._verify("alice-old-key")
        entry = next(iter(odoo_mcp_server._user_cache.values()))
        entry.checked_at -= odoo_mcp_server._VERIFY_TTL_SECONDS + 1
        self._verify("alice-new-key")
        assert len(odoo_mcp_server._user_cache) == 1

    @patch("odoo_mcp_server.OdooJsonRpcClient.connect")
    def test_neg_cache_overflow_evicts_oldest_only(self, mock_connect, monkeypatch, clear_user_caches):
        """負快取滿了只逐筆丟最舊的，不整鍋清空——近期失敗 key 的鎖定不被亂 key 重置."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        monkeypatch.setattr("odoo_mcp_server._NEG_CACHE_MAX", 3)
        fake = MagicMock()
        fake.get_current_uid.side_effect = AuthenticationError("bad")
        mock_connect.return_value = fake
        for i in range(3):
            self._verify(f"bad-{i}")  # 填滿到上限
        self._verify("bad-3")  # 觸發淘汰：只丟最舊的 bad-0
        assert len(odoo_mcp_server._neg_cache) == 3
        calls = mock_connect.call_count
        self._verify("bad-2")  # 仍在負快取 → 不打 Odoo
        assert mock_connect.call_count == calls


def _fake_access(client_id="alice@example.com", token="alice-api-key", auth="odoo-api-key"):
    """模擬 verify_token 簽出的 AccessToken（路由依據是 claims["auth"]）."""
    return MagicMock(client_id=client_id, token=token, claims={"auth": auth})


class TestGetCallerClient:
    """get_caller_client：依呼叫者身分回專屬連線或 env 共享單例."""

    def test_no_auth_context_uses_env_singleton(self, monkeypatch, clear_user_caches):
        """stdio / 未認證 → env 憑證的共享單例（原行為不變）."""
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: None)
        monkeypatch.setattr("odoo_mcp_server._client", None)
        monkeypatch.setattr("odoo_mcp_server.ODOO_API_KEY", "env-key")
        with patch("odoo_mcp_server.OdooJsonRpcClient.connect") as mock_connect:
            mock_connect.return_value = MagicMock()
            c1 = odoo_mcp_server.get_caller_client()
            c2 = odoo_mcp_server.get_caller_client()
        assert c1 is c2
        mock_connect.assert_called_once_with(odoo_mcp_server.ODOO_URL, odoo_mcp_server.ODOO_DATABASE, "env-key")

    @pytest.mark.parametrize("bad_key", ["", "your_api_key_here"])
    def test_env_path_without_real_key_fails_fast(self, bad_key, monkeypatch, clear_user_caches):
        """ODOO_API_KEY 空值或 placeholder → 明確 ToolError，而非拿 placeholder 打 Odoo 吃 401."""
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: None)
        monkeypatch.setattr("odoo_mcp_server._client", None)
        monkeypatch.setattr("odoo_mcp_server.ODOO_API_KEY", bad_key)
        with (
            patch("odoo_mcp_server.OdooJsonRpcClient.connect") as mock_connect,
            pytest.raises(ToolError, match="ODOO_API_KEY is not configured"),
        ):
            odoo_mcp_server.get_caller_client()
        mock_connect.assert_not_called()

    def test_admin_fallback_warning(self, monkeypatch, capsys):
        """MCP_MULTIUSER + MCP_AUTH_TOKEN 但沒真的設 ODOO_API_KEY → 啟動警告."""
        monkeypatch.setattr("odoo_mcp_server.MCP_MULTIUSER", True)
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "master")
        monkeypatch.setattr("odoo_mcp_server.ODOO_API_KEY", "your_api_key_here")
        odoo_mcp_server._warn_if_admin_fallback_unusable()
        assert "admin fallback" in capsys.readouterr().err

    def test_env_admin_uses_env_singleton(self, monkeypatch, clear_user_caches):
        """MCP_AUTH_TOKEN 身分（env-admin，claims auth=static）→ 共享單例，不走 per-user 路徑."""
        admin = _fake_access(client_id=odoo_mcp_server.ENV_ADMIN_CLIENT_ID, token="master", auth="static")
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: admin)
        sentinel = MagicMock()
        monkeypatch.setattr("odoo_mcp_server._client", sentinel)
        assert odoo_mcp_server.get_caller_client() is sentinel

    def test_authenticated_user_gets_own_client(self, monkeypatch, clear_user_caches):
        """已認證的一般 user → 綁自己 API key 的專屬連線，env 單例不被建立."""
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: _fake_access())
        monkeypatch.setattr("odoo_mcp_server._client", None)
        fake = _fake_odoo_client()
        with patch("odoo_mcp_server.OdooJsonRpcClient.connect", return_value=fake) as mock_connect:
            client = odoo_mcp_server.get_caller_client()
        assert client is fake
        mock_connect.assert_called_once_with(odoo_mcp_server.ODOO_URL, odoo_mcp_server.ODOO_DATABASE, "alice-api-key")
        assert odoo_mcp_server._client is None

    def test_login_collision_cannot_reach_env_singleton(self, monkeypatch, clear_user_caches):
        """Odoo login 剛好叫 ENV_ADMIN_CLIENT_ID 的 user 不能被誤路由到 env 共享連線（權限提升）.

        路由依據必須是 verify_token 蓋的 claims["auth"]，不能是 client_id
        （client_id 取自 Odoo login，是使用者可影響的字串）。
        """
        access = _fake_access(client_id=odoo_mcp_server.ENV_ADMIN_CLIENT_ID, token="collider-key")
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: access)
        sentinel = MagicMock()
        monkeypatch.setattr("odoo_mcp_server._client", sentinel)
        fake = _fake_odoo_client(login=odoo_mcp_server.ENV_ADMIN_CLIENT_ID)
        with patch("odoo_mcp_server.OdooJsonRpcClient.connect", return_value=fake):
            client = odoo_mcp_server.get_caller_client()
        assert client is fake
        assert client is not sentinel

    def test_revoked_key_raises_tool_error(self, monkeypatch, clear_user_caches):
        """快取過期後 key 已被撤銷 → ToolError（明確告知，而非 500）."""
        access = _fake_access(token="revoked-key")
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: access)
        fake = MagicMock()
        fake.get_current_uid.side_effect = AuthenticationError("revoked")
        with (
            patch("odoo_mcp_server.OdooJsonRpcClient.connect", return_value=fake),
            pytest.raises(ToolError, match="rejected"),
        ):
            odoo_mcp_server.get_caller_client()


class TestUploadTokenMultiuser:
    """多 user 模式下的 upload token：身分入 token、無 master token 也能簽驗."""

    def test_token_embeds_caller_identity(self, monkeypatch):
        """簽發者身分嵌進 token（含 "." 的 email login 也驗得過）."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", "secret")
        access = MagicMock(client_id="alice@example.com")
        monkeypatch.setattr("odoo_mcp_server._safe_get_access_token", lambda: access)
        token = _make_upload_token()
        assert token is not None
        assert ".alice@example.com." in token
        assert _verify_upload_token(token) is True

    def test_multiuser_without_master_token(self, monkeypatch):
        """MCP_MULTIUSER 且未設 MCP_AUTH_TOKEN → 用啟動時生成的 secret 簽驗."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_TOKEN_SECRET", None)
        monkeypatch.setattr("odoo_mcp_server.MCP_MULTIUSER", True)
        token = _make_upload_token()
        assert token is not None
        assert _verify_upload_token(token) is True

    def test_multiuser_upload_requires_bearer(self, monkeypatch):
        """多 user 模式下 /upload 一樣要驗：沒帶 Bearer 拒絕、衍生 token 放行."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_TOKEN_SECRET", None)
        monkeypatch.setattr("odoo_mcp_server.MCP_MULTIUSER", True)
        req_no_auth = MagicMock()
        req_no_auth.headers = {}
        assert _check_upload_auth(req_no_auth) is False
        req_ok = MagicMock()
        req_ok.headers = {"authorization": f"Bearer {_make_upload_token()}"}
        assert _check_upload_auth(req_ok) is True

    def test_upload_token_secret_env_override(self, monkeypatch):
        """UPLOAD_TOKEN_SECRET 優先於其他 secret 來源（多 worker 部署用）."""
        monkeypatch.setattr("odoo_mcp_server.MCP_AUTH_TOKEN", None)
        monkeypatch.setattr("odoo_mcp_server.MCP_MULTIUSER", False)
        monkeypatch.setattr("odoo_mcp_server.UPLOAD_TOKEN_SECRET", "shared-secret")
        token = _make_upload_token()
        assert token is not None
        assert _verify_upload_token(token) is True
