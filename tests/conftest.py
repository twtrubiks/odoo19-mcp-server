"""確保測試不受開發者本機 .env / shell 環境影響.

odoo_mcp_server 在 import 時執行 load_dotenv()（預設不覆蓋既有環境變數），
且認證相關設定（_auth 掛載、MCP_MULTIUSER）在模組層級就定案。因此必須趕在
測試模組 import 它之前，把這些變數釘成測試假設的基線（無認證、單 user），
測試行為才與本機 .env 的內容無關。
"""

import os

os.environ["MCP_MULTIUSER"] = "false"
os.environ["MCP_AUTH_TOKEN"] = ""  # 空字串 falsy，等同未設定
os.environ["UPLOAD_TOKEN_SECRET"] = ""
