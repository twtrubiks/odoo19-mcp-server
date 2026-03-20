"""Tests for odoo_mcp_server — 只測有實際邏輯、容易藏 bug 的地方."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from odoo_mcp_server import OdooJsonRpcClient, create_record, delete_record, execute_method

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
