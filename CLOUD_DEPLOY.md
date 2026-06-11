# QR POS Order V2.3 雲端部署說明

## 1. 建議部署方式

測試與展示可先用 Render Web Service。正式營運建議至少加：自訂網域、備份、持久化資料庫、圖片雲端儲存。

## 2. Render 設定

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Environment Variables 建議設定：

```text
SECRET_KEY=請自行產生一組長亂數
ADMIN_PASSWORD=你的後台密碼
KITCHEN_PASSWORD=你的後廚密碼
CHECKOUT_PASSWORD=你的結帳密碼
PRINT_AGENT_TOKEN=請自行產生一組列印代理程式Token
BASE_URL=https://你的render網址.onrender.com
```

## 3. 重要限制

V2.3 預設仍使用 SQLite，適合測試版。Render 免費服務如果沒有掛 Persistent Disk，重啟後資料可能遺失。
正式營業建議升級 V2.4：PostgreSQL + 圖片雲端儲存 + 自動備份。

## 4. 桌號 QR

正式 QR 使用：

```text
https://你的網域/t/A01
https://你的網域/t/A02
```

## 5. 店內列印代理程式

店內電腦保留 `run_print_agent.bat`，並在 `config.json` 設定：

```json
"base_url": "https://你的網域",
"print_agent_token": "與雲端相同的PRINT_AGENT_TOKEN"
```
