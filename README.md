# STARWISH 下單網站（含後端）

Flask + SQLite。客人在網站選品項後送出，訂單會存進資料庫，並通知你的 Discord 頻道。

## 功能
- `/`：下單網站（服務選項、品項與價格都由 `catalog.json` 提供，改這個檔即可）
- `POST /api/orders`：收單。**總額由伺服器依 catalog.json 重新計算**，不信任前端數字；另有欄位檢查、
  同一 IP 10 分鐘最多 5 筆、隱藏欄位擋機器人
- `/admin`：訂單後台（帳密登入，可改狀態）、`/admin/export.csv`：匯出 Excel 可開的 CSV
- 每筆新訂單推送到 Discord webhook（沒設定就略過）

## 本機試跑
```bash
pip install -r requirements.txt
cp .env.example .env   # 填入設定；本機可用 export 載入
export ADMIN_PASSWORD=你的密碼 DISCORD_WEBHOOK_URL=你的webhook網址
python app.py          # http://localhost:5000
```

## 設定（環境變數）
| 名稱 | 說明 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord 頻道 → 編輯頻道 → 整合 → Webhook |
| `ADMIN_USER` / `ADMIN_PASSWORD` | 後台帳密。**沒設 ADMIN_PASSWORD 後台會停用** |
| `DB_PATH` | SQLite 檔案位置，需放在有持久儲存的磁碟 |

## 部署
啟動指令：`gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`
（頻率限制記在記憶體裡，所以用單一 worker）

- **自己的主機（VPS）**：用 gunicorn 加 Nginx 或 Caddy 反向代理，Caddy 可自動申請 HTTPS。
- **Render 等平台**：選 Web Service，Build 填 `pip install -r requirements.txt`，Start 填上面的指令，
  並**掛載持久磁碟**，把 `DB_PATH` 指到磁碟路徑。沒有持久磁碟時，重新部署後訂單會消失
  （Discord 通知仍會保留一份紀錄）。

## 綁定網域 starwish.tw
在主機平台新增自訂網域，再依指示到註冊商設定 DNS（www 用 CNAME，根網域用 A 或 ALIAS）。

## 上線前
- 到 `catalog.json` 換成正式價格與品項
- 後台使用 HTTPS 才安全（Basic 登入的密碼是明文傳輸）
- 訂單含客人聯絡資料，請定期備份 `orders.db` 或匯出 CSV
