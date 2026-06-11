# QR POS Order V2.3

## 新增功能

- 客戶端 / 後廚端 / 結帳端 / 後台權限分離
- 登入頁 `/login`
- 後台密碼、後廚密碼、結帳密碼
- 列印代理程式 Token 驗證
- Render 雲端部署檔案：`render.yaml`、`.env.example`、`CLOUD_DEPLOY.md`

## 預設入口

- 客戶端：`/t/A01`
- 後廚端：`/kds`
- 結帳端：`/checkout`
- 後台：`/admin`

## 預設密碼

- 後台：admin123
- 後廚：kitchen123
- 結帳：checkout123

正式上線前請務必修改 `config.json` 或雲端環境變數。
