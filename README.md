# Odoo 19 MCP Server (JSON-2 API)

[![odoo19-mcp-server MCP server](https://glama.ai/mcp/servers/twtrubiks/odoo19-mcp-server/badges/card.svg)](https://glama.ai/mcp/servers/twtrubiks/odoo19-mcp-server)

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white)](https://www.python.org/)
[![GitHub stars](https://img.shields.io/github/stars/twtrubiks/odoo19-mcp-server?style=flat)](https://github.com/twtrubiks/odoo19-mcp-server/stargazers)
[![GitHub last commit](https://img.shields.io/github/last-commit/twtrubiks/odoo19-mcp-server)](https://github.com/twtrubiks/odoo19-mcp-server/commits/main)
[![Awesome MCP Servers](https://img.shields.io/badge/Awesome-MCP_Servers-fc60a8?logo=awesomelists&logoColor=white)](https://github.com/punkpeye/awesome-mcp-servers)

**支援的 MCP Client**

[![Claude Code](https://img.shields.io/badge/Claude_Code-supported-D97757?logo=anthropic&logoColor=white)](#claude-code)
[![Gemini CLI](https://img.shields.io/badge/Gemini_CLI-supported-4285F4?logo=googlegemini&logoColor=white)](#gemini-cli)
[![Antigravity CLI](https://img.shields.io/badge/Antigravity_CLI-supported-1A73E8?logo=google&logoColor=white)](#antigravity-cli)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-supported-7C3AED)](#openclaw)
[![Codex CLI](https://img.shields.io/badge/Codex_CLI-supported-000000?logo=openai&logoColor=white)](#codex-cli)

* [Youtube Tutorial - MCP Server 自己做！Odoo 19 + FastMCP 完整開發教學](https://youtu.be/JhAudIIII3M)

Odoo 19 MCP Server，使用 JSON-2 API 連線。

本專案基於 [Odoo 19 JSON-2 API 完整使用指南](https://github.com/twtrubiks/odoo-demo-addons-tutorial/blob/19.0/odoo-json2-client/README.md) 開發。

![執行畫面](https://cdn.imgpile.com/f/re0866c_xl.png)

## 技術棧

- **Python**: 3.13
- **FastMCP**: >=3.0.0,<4.0.0
- **odoo-client-lib**: 2.0.1 (JSON-2 API)

## 架構

```mermaid
flowchart TB
    subgraph Client["MCP Client"]
        CC[Claude Code]
        GC[Gemini CLI]
        MI[MCP Inspector]
    end

    subgraph Server["MCP Server (FastMCP)"]
        R[Resources<br/>odoo://models<br/>odoo://user<br/>odoo://company]
        T[Tools<br/>search_records<br/>create_record<br/>update_record]
        DI[Dependency Injection<br/>get_shared_client]
    end

    subgraph RPC["OdooJsonRpcClient"]
        OL[odoolib<br/>json2/json2s protocol]
    end

    subgraph Odoo["Odoo Server"]
        EP["/jsonrpc endpoint"]
    end

    Client -->|MCP Protocol<br/>stdio/http/sse| Server
    R --> DI
    T --> DI
    DI --> RPC
    RPC -->|HTTP/HTTPS| Odoo
```

## MCP 核心概念

### Resources vs Tools

| 特性 | Resources | Tools |
|------|-----------|-------|
| **用途** | 提供上下文資訊 | 執行操作/動作 |
| **觸發** | 客戶端控制（如 Claude Code） | LLM 自動決定呼叫 |
| **參數** | 無（或 URI 參數） | 有（需 LLM 生成） |
| **類比** | 員工手冊（背景知識） | 工具箱（按需使用） |
| **HTTP 類比** | GET（讀取） | POST/PUT/DELETE（操作） |

**Resources** - 動態上下文，LLM 一開始就知道的背景資訊：

```
odoo://user     → "我是誰"
odoo://company  → "我在哪間公司"
odoo://models   → "有哪些模型可用"
```

**Tools** - 需要時才呼叫的操作：

```
search_records(model="res.partner", domain=[...])  → 搜尋
create_record(model="sale.order", values={...})    → 建立
```

### 為什麼不用 Default Prompt？

| 方式 | Default Prompt | Resource |
|------|----------------|----------|
| 資料來源 | 寫死在程式碼 | 即時從 Odoo 查詢 |
| 更新時機 | 部署時 | 每次連線時 |
| 換用戶登入 | 資訊錯誤 | 自動正確 |

```python
# ❌ Default Prompt（寫死）
SYSTEM_PROMPT = "當前用戶: Admin"  # 換人登入就錯了

# ✅ Resource（動態）
@mcp.resource("odoo://user")
def get_current_user():
    return client.read("res.users", [uid])  # 即時查詢
```

**結論**：Resource 是「動態的上下文」，不是靜態文字。

> 參考：[MCP Resources](https://modelcontextprotocol.io/docs/concepts/resources) | [MCP Tools](https://modelcontextprotocol.io/docs/concepts/tools)

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `ODOO_URL` | Odoo 伺服器 URL | `http://localhost:8069` |
| `ODOO_DATABASE` | 資料庫名稱 | - |
| `ODOO_API_KEY` | API Key 認證 | - |
| `READONLY_MODE` | 唯讀模式（禁止寫入操作） | `false` |
| `MCP_AUTH_TOKEN` | HTTP/SSE 模式的 Bearer Token 認證（未設定＝無認證；stdio 不適用），見[安全機制](#安全機制) | -（停用） |

建立 `.env` 檔案：

```bash
cp .env.example .env
```

## 安裝

```bash
pip install -r requirements.txt
```

## 啟動方式

### 開發模式（MCP Inspector）

```bash
fastmcp dev inspector odoo_mcp_server.py
```

## 傳輸模式（Transport）

本專案支援三種 MCP 傳輸模式：

| 模式 | 說明 | 適用情境 |
|------|------|----------|
| `stdio` | 標準輸入輸出（預設） | Claude Desktop、Cursor IDE、本機開發 |
| `http` | HTTP 協定 | 遠端服務、n8n、Web 應用整合 |
| `sse` | Server-Sent Events（已棄用） | 向下相容舊版 Client |

### stdio vs HTTP/SSE：算力位置

兩種模式的關鍵差異在於「誰來啟動 MCP Server」以及「算力在哪裡執行」：

**stdio 模式（本機算力）**

```
┌─────────────────────────────────────┐
│            你的電腦 💻               │
│                                     │
│  Claude Desktop ──> MCP Server      │
│                     (使用本機算力)   │
└─────────────────────────────────────┘
```

- Client（如 Claude Desktop）啟動 MCP Server 作為子進程
- MCP Server 使用你電腦的 CPU/RAM
- Server 隨 Client 啟動/關閉

**HTTP/SSE 模式（遠端算力）**

```
┌──────────────┐         ┌──────────────────┐
│   你的電腦    │         │     雲端 ☁️       │
│              │         │                  │
│Claude Desktop│ ──網路──>│   MCP Server     │
│  (輕量)      │         │  (使用雲端算力)   │
└──────────────┘         └──────────────────┘
```

- MCP Server 獨立運行在雲端/遠端主機
- 多個 Client 可同時連線同一個 Server
- 適合團隊共用、n8n 整合、正式環境

### 啟動不同模式

```bash
# stdio 模式（預設）
python odoo_mcp_server.py

# HTTP 模式
python odoo_mcp_server.py --transport http --host 0.0.0.0 --port 8000

# SSE 模式（已棄用，建議使用 HTTP）
python odoo_mcp_server.py --transport sse --host 0.0.0.0 --port 8000
```

### 雲端部署（HTTP 模式）

> ⚠️ **安全提醒**：HTTP 模式預設**沒有認證**——任何連得到該 port 的人都直接繼承
> `ODOO_API_KEY` 的完整權限。除非 server 只在受信任的內網使用，
> 否則請務必設定 `MCP_AUTH_TOKEN` 並搭配 TLS，詳見[安全機制](#安全機制)

專案提供 `docker-compose.example.yml` 範本，複製後修改即可使用：

```bash
cp .env.example .env                                  # 填入 ODOO_URL / ODOO_DATABASE / ODOO_API_KEY
cp docker-compose.example.yml docker-compose.yml      # 依需求調整
docker compose up -d
```

範本內容

```yaml
volumes:
  shared-uploads:

services:
  odoo-mcp:
    build: .
    command: ["python", "odoo_mcp_server.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
    # 對外暴露 port 8000（host 端 client 可直接連 http://localhost:8000/mcp）。
    # ⚠️ "8000:8000" 會綁定 0.0.0.0：同網段的所有機器都連得到
    # （主機若有公網 IP，就是整個網際網路），且 Docker 發佈的 port 會繞過 ufw 防火牆規則。
    # 建議設定 MCP_AUTH_TOKEN（見 environment）；只給本機 client 用可改 "127.0.0.1:8000:8000"。
    # 若只需 Docker 內網存取（例如 client 也在同一個 compose 裡），可整段移除 ports。
    ports:
      - "8000:8000"
    environment:
      - ODOO_URL=${ODOO_URL}
      - ODOO_DATABASE=${ODOO_DATABASE}
      - ODOO_API_KEY=${ODOO_API_KEY}
      - READONLY_MODE=${READONLY_MODE:-false}
      # HTTP 模式的 Bearer Token 認證（未設定＝無認證，見 README「安全機制」）
      - MCP_AUTH_TOKEN=${MCP_AUTH_TOKEN:-}
      # HTTP 模式的 Host 標頭防護（DNS rebinding protection，來自底層 MCP SDK）：
      # 用非 localhost 的 IP／網域連進來時，預設會被擋下並回 "Invalid host header"。
      # ⚠️ 快速測試可先全開（勿用於正式環境）：
      # - FASTMCP_HTTP_ALLOWED_HOSTS=["*"]
    volumes:
      - shared-uploads:/shared   # 圖片傳遞通道；對應 Dockerfile 預建的 /shared/uploads
    restart: unless-stopped
```

> **圖片 / 附件傳遞**：`add_attachment` 的 `file_path` 模式會從 `/shared/uploads/` 讀檔上傳到 Odoo，避免大量 base64 佔用 LLM output token。
>
> ⚠️ `shared-uploads` 是 Docker named volume，只能在「同一台 Docker host」共享。client 與 server **跨機器**時無法共用此 volume，只能改用 `base64_data` 模式傳檔。詳見 `docker-compose.example.yml` 註解。
>
> 🔒 `file_path` 僅允許讀取 `UPLOAD_DIR`（預設 `/shared/uploads`）底下的檔案，`resolve()` 後越界一律拒絕（含指向外部的 symlink）。這是為了防止被 prompt injection 的 LLM 用 `file_path="/app/.env"` 把 server 機密讀出、上傳成附件外洩。純本機 stdio 若要放行任意路徑，設 `UPLOAD_DIR=/`（等於解除限制，自負風險）。

> **連不上、回 `Invalid host header`？** 這是底層 MCP SDK 的 **DNS rebinding 防護**——用非 localhost 的 IP／網域連進來時，Host 標頭不在允許清單內就會被擋。用 `FASTMCP_HTTP_ALLOWED_HOSTS` 放行：
>
> ```bash
> # 快速測試（⚠️ 對任何 Host 開放，勿用於正式環境）
> FASTMCP_HTTP_ALLOWED_HOSTS=["*"]
>
> # ✅ 正規做法：只列出 client 實際連線的 host（含 port）
> FASTMCP_HTTP_ALLOWED_HOSTS=["your-server-ip:8000"]     # 純 IP 部署
> FASTMCP_HTTP_ALLOWED_HOSTS=["mcp.example.com"]         # 反向代理／網域（建議搭配 TLS）
> ```
>
> 官方明確警告：使用萬用字元 `*` 會讓 server 對任何來源開放，正式環境請務必列出明確 host。必要時另有 `FASTMCP_HTTP_ALLOWED_ORIGINS`（瀏覽器型 client 的 Origin 白名單）。

```sh
# server 有設 MCP_AUTH_TOKEN 時，需帶 Authorization header
claude mcp add --transport http odoo-mcp https://your-cloud-server.com:8000/mcp --header "Authorization: Bearer your_random_token_here"

# server 未啟用認證（僅限受信任內網）
claude mcp add --transport http odoo-mcp https://your-cloud-server.com:8000/mcp
```

<details>
<summary><b>手動設定 JSON（加到 `~/.claude.json`）</b></summary>

```json
{
  "mcpServers": {
    "odoo-mcp": {
      "type": "http",
      "url": "https://your-cloud-server.com:8000/mcp",
      "headers": {
        "Authorization": "Bearer your_random_token_here"
      }
    }
  }
}
```

> ⚠️ 純 HTTP 下 Bearer token 是明文傳輸，僅適合受信任內網／臨時測試；對外請改用 `https://`（TLS）。

server 未啟用 `MCP_AUTH_TOKEN` 時，`headers` 整段可省略。

</details>

## MCP Resources

| URI | 說明 |
|-----|------|
| `odoo://models` | 列出所有模型 |
| `odoo://model/{model_name}` | 取得模型欄位定義 |
| `odoo://record/{model_name}/{record_id}` | 取得單筆記錄 |
| `odoo://user` | 當前登入用戶資訊 |
| `odoo://company` | 當前用戶所屬公司資訊 |

## MCP Tools

| Tool | 說明 | 唯讀 |
|------|------|------|
| `list_models` | 列出/搜尋可用模型 | Yes |
| `get_fields` | 取得模型欄位定義 | Yes |
| `search_records` | 搜尋記錄 | Yes |
| `count_records` | 計數記錄 | Yes |
| `read_records` | 讀取指定 ID 記錄 | Yes |
| `create_record` | 建立記錄 | No |
| `update_record` | 更新記錄 | No |
| `delete_record` | 刪除記錄（需二次確認） | No |
| `execute_method` | 執行任意模型方法（萬用入口，`unlink` 已封鎖，見[安全機制](#安全機制)） | No |

## Docker 建置

部分 client 的 Docker 設定（Claude Code / Gemini 的 Docker 版本）需要先建置本機映像檔：

```bash
docker build -t odoo-mcp-server .
```

## MCP Client 設定

本專案支援以下 MCP Client，各自的完整設定步驟見對應章節：

| Client | 加入方式 | 設定檔 |
|--------|----------|--------|
| [Claude Code](#claude-code) | `claude mcp add` | `~/.claude.json` |
| [Gemini CLI](#gemini-cli) | `gemini mcp add` | `~/.gemini/settings.json` |
| [Antigravity CLI](#antigravity-cli) | 手動編輯 | `~/.gemini/config/mcp_config.json` |
| [OpenClaw](#openclaw) | `openclaw mcp set` | OpenClaw config |
| [Codex CLI](#codex-cli) | `codex mcp add` + 手動編輯 | `~/.codex/config.toml` |

### Claude Code

設定檔位於 `~/.claude.json`：

#### 本機執行

```sh
claude mcp add odoo-mcp-server -- python odoo_mcp_server.py
```

<details>
<summary><b>手動設定 JSON</b></summary>

```json
{
  "mcpServers": {
    "odoo-mcp-server": {
      "command": "/bin/python",
      "args": [
        "odoo_mcp_server.py"
      ]
    }
  }
}
```

</details>

#### Docker（host.docker.internal）

適用於 Odoo 執行在本機的情況：

```sh
claude mcp add odoo-mcp-server -- docker run -i --rm --add-host=host.docker.internal:host-gateway -e ODOO_URL=http://host.docker.internal:8069 -e ODOO_DATABASE=odoo19 -e ODOO_API_KEY=your_api_key_here odoo-mcp-server
```

<details>
<summary><b>手動設定 JSON</b></summary>

```json
{
  "mcpServers": {
    "odoo-mcp-server": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "-e",
        "ODOO_URL=http://host.docker.internal:8069",
        "-e",
        "ODOO_DATABASE=odoo19",
        "-e",
        "ODOO_API_KEY=your_api_key_here",
        "odoo-mcp-server"
      ]
    }
  }
}
```

</details>

#### Docker（host network）

使用主機網路模式：

```sh
claude mcp add odoo-mcp-server -- docker run -i --rm --network host -e ODOO_URL=http://localhost:8069 -e ODOO_DATABASE=odoo19 -e ODOO_API_KEY=your_api_key_here odoo-mcp-server
```

<details>
<summary><b>手動設定 JSON</b></summary>

```json
{
  "mcpServers": {
    "odoo-mcp-server": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--network",
        "host",
        "-e",
        "ODOO_URL=http://localhost:8069",
        "-e",
        "ODOO_DATABASE=odoo19",
        "-e",
        "ODOO_API_KEY=your_api_key_here",
        "odoo-mcp-server"
      ]
    }
  }
}
```

</details>

#### Docker（遠端 Odoo）

```sh
claude mcp add odoo-mcp-server -- docker run -i --rm -e ODOO_URL=https://example.com/ -e ODOO_DATABASE=odoo19 -e ODOO_API_KEY=your_api_key_here odoo-mcp-server
```

<details>
<summary><b>手動設定 JSON</b></summary>

```json
{
  "mcpServers": {
    "odoo-mcp-server": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e",
        "ODOO_URL=https://example.com/",
        "-e",
        "ODOO_DATABASE=odoo19",
        "-e",
        "ODOO_API_KEY=your_api_key_here",
        "odoo-mcp-server"
      ]
    }
  }
}
```

</details>

### Gemini CLI

```sh
gemini mcp add --scope user odoo-mcp docker -- run -i --rm --add-host=host.docker.internal:host-gateway -e ODOO_URL=http://host.docker.internal:8069 -e ODOO_DATABASE=odoo19 -e ODOO_API_KEY=your_api_key_here odoo-mcp-server
```

<details>
<summary><b>手動設定 JSON（加到 `~/.gemini/settings.json`）</b></summary>

```json
{
  "mcpServers": {
    "odoo-mcp": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "-e",
        "ODOO_URL=http://host.docker.internal:8069",
        "-e",
        "ODOO_DATABASE=odoo19",
        "-e",
        "ODOO_API_KEY=your_api_key_here",
        "odoo-mcp-server"
      ]
    }
  }
}
```

</details>

### Antigravity CLI

> 自 2026/6/18 起個人版 Gemini CLI 停止服務，改用 [Antigravity CLI](https://antigravity.google/)。目前 **沒有** `mcp add` 子指令，需手動編輯設定檔。

設定檔路徑為 `~/.gemini/config/mcp_config.json`（Antigravity CLI / IDE / SDK 共用，等同 Gemini CLI 的 `--scope user`）。

JSON 格式與上方 Gemini CLI 設定相同。

設定後進入 Antigravity CLI 以 `/mcp` 指令重新載入，並確認連線狀態。

### OpenClaw

OpenClaw 透過 CLI 管理 MCP server，設定會寫入 `mcp.servers.<name>`。

> `/mcp` 指令為 **owner-only 且預設關閉**，需以 `commands.mcp: true` 開啟才能在 chat session 中使用。

#### 步驟 1：註冊 MCP server

```sh
# 請將 your-server-ip 換成你的 MCP server 位址
openclaw mcp set odoo-mcp '{"type":"http","url":"http://your-server-ip:8000/mcp"}'
```

<details>
<summary><b>手動設定 JSON（寫入 OpenClaw 設定的 `mcp.servers`）</b></summary>

OpenClaw 會自動正規化設定，把 `type:"http"` 轉成 `transport:"streamable-http"` 後存入：

```json
{
  "mcp": {
    "servers": {
      "odoo-mcp": {
        "url": "http://your-server-ip:8000/mcp",
        "transport": "streamable-http"
      }
    }
  }
}
```

</details>

#### 步驟 2：開啟 `/mcp` 指令

```sh
openclaw config set commands.mcp true
```

#### 步驟 3：重啟 Gateway 套用設定

```sh
openclaw gateway restart
```

> 若想等進行中的工作排空再重啟，可改用 `openclaw gateway restart --safe`。

#### 驗證

```sh
# server 是否註冊成功
openclaw mcp list
openclaw mcp show odoo-mcp

# /mcp 開關狀態（應回傳 true）
openclaw config get commands.mcp
```

完成後請**開一個新的 chat session（或硬重整 dashboard），再輸入 `/mcp`** 確認 `odoo-mcp` 連線狀態。

### Codex CLI

> Codex 的 `codex mcp add` **只支援 stdio（`command` / `args`）**，並不支援 url（streamable HTTP）形式的遠端 server。因此要連雲端 HTTP 模式的 MCP server，需先用佔位指令建立設定，再手動編輯 `~/.codex/config.toml`。

```sh
codex mcp add odoo-mcp -- echo placeholder
```

<details>
<summary><b>手動設定 TOML（修改 `~/.codex/config.toml`）</b></summary>

`codex mcp add` 產生的佔位設定：

```toml
[mcp_servers.odoo-mcp]
command = "echo"
args = ["placeholder"]
```

手動改為 url（streamable HTTP）：

```toml
[mcp_servers.odoo-mcp]
url = "https://your-cloud-server.com:8000/mcp"
```

> 若 server 端設定了 `MCP_AUTH_TOKEN`，需加上 `bearer_token_env_var = "ODOO_MCP_TOKEN"`，
> 並在執行 Codex 的環境中 `export ODOO_MCP_TOKEN=<與 server MCP_AUTH_TOKEN 相同的值>`；
> 或改用自訂 `http_headers` 直接填 `Authorization` header。

</details>

## 安全機制

### 部署定位與 HTTP 認證（`MCP_AUTH_TOKEN`）

設定 `MCP_AUTH_TOKEN` 環境變數即可啟用 Bearer Token 認證（opt-in）：

```bash
# 產生隨機 token
openssl rand -hex 32
```

- 啟用後 `/mcp` 端點要求 `Authorization: Bearer <token>`，未帶或錯誤一律回 401
- 未設定時行為與過去版本相同（無認證），但 HTTP/SSE 模式啟動時會在 stderr 印出警告

### 附件檔案讀取範圍（`UPLOAD_DIR`）

`add_attachment` 的 `file_path` 模式是本 server 唯一會讀取 MCP 主機本地檔案的入口。
若不設限，被 prompt injection 的 LLM 可用 `file_path="/app/.env"` 把 server 機密
（含 `ODOO_API_KEY` 本身）讀出、上傳成 Odoo 附件外洩——這是 confused deputy，
`MCP_AUTH_TOKEN` 擋不住（LLM 本來就是合法持 token 的 client）。

因此 `file_path` 被限制在 `UPLOAD_DIR`（預設 `/shared/uploads`）底下：

- 路徑經 `Path.resolve()` 正規化後，必須落在 `UPLOAD_DIR` 內，否則回 `ToolError`
- `resolve()` 會一併解掉 symlink，所以「白名單目錄裡放一個指向外部的 symlink」也擋得掉
- 預設值對齊 compose 的 `/shared/uploads` 圖片傳遞通道，Docker 部署無需額外設定
- 純本機 stdio 若要放行任意路徑，設 `UPLOAD_DIR=/`（等於解除限制，自負風險）
- 檔案不在磁碟上（如 Discord 上傳的圖片）時，改用 `base64_data` 模式，不受此限制

### 唯讀模式

設定 `READONLY_MODE=true` 啟用唯讀模式，適用於生產環境查詢：

- 寫入工具（`create_record`、`update_record`、`delete_record`、`execute_method`、`add_attachment`）在註冊時即被停用——LLM 看不到這些工具，直接呼叫也會被拒絕
- 停用發生在模組層級，任何啟動方式（`python odoo_mcp_server.py`、`fastmcp run`、`fastmcp dev`）都同樣生效

### 刪除二次確認

`delete_record` 內建 confirm 機制，LLM 必須先以 `confirm=False` 呼叫取得確認提示，經使用者同意後才能以 `confirm=True` 執行刪除。
`execute_method` 已攔下 `unlink`，無法用它繞過此確認流程。

> 注意：confirm 參數由 LLM 自行填入，屬於「引導 LLM」層級的防護，
> 並非強制性的安全邊界（LLM 理論上可直接傳 `confirm=True`）。

### `execute_method` 的安全邊界

`execute_method` 是萬用入口（escape hatch），用來呼叫沒有專用工具的模型方法，請理解其風險：

- ORM 原語 `unlink` 已被攔下，會導向 `delete_record` 的二次確認流程
- 但 `action_confirm`、`action_post`、`button_validate` 這類會改資料的**業務方法不設防**——
  Odoo 有上千個模型方法，server 無法枚舉哪些會寫入資料庫
- 真正的安全邊界在應該是給 MCP 使用**低權限的 Odoo 使用者** API key（最小權限原則）

## 健康檢查

HTTP/SSE transport 模式下提供 `/health` 端點：

```bash
curl http://localhost:8000/health
# {"status": "healthy", "service": "odoo-mcp-server", "version": "1.0.0"}
```

適用於 Docker healthcheck、Kubernetes probe、load balancer 探活。stdio 模式下不影響。
此端點**不受** `MCP_AUTH_TOKEN` 保護（不需帶 token）。

## License

Apache 2.0
