FROM python:3.13-slim

LABEL maintainer="twtrubiks@gmail.com" \
      description="Odoo MCP Server using FastMCP and JSON-RPC" \
      version="1.0"

WORKDIR /app

# Python 環境最佳化
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# 安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式
COPY odoo_mcp_server.py .

# 建立非 root 用戶
RUN useradd --create-home appuser
USER appuser

# 環境變數（執行時透過 -e 覆蓋）
# 注意：ODOO_API_KEY 為敏感資料，不在此定義，必須透過 docker run -e 傳入
ENV ODOO_URL=""
ENV ODOO_DATABASE=""
ENV READONLY_MODE=false

# 預設使用 stdio 傳輸層（適合 Claude CLI/Desktop）
# HTTP 模式: docker run -p 8000:8000 odoo-mcp-server python odoo_mcp_server.py --transport http --host 0.0.0.0 --port 8000
CMD ["python", "odoo_mcp_server.py"]
