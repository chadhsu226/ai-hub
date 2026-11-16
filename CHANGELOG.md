# CHANGELOG

> 開發端每次修改後在最上方新增一筆。GitHub agent 依「變更檔案」同步，`data/` 永遠不在同步範圍內（首次部署除外）。

---

## 2026-11-16 · v1.1.0 beta 密碼畫面

**狀態**：待同步（請依 `DEPLOY.md` 第 3 節）

**變更檔案**
- `index.html`
- `CHANGELOG.md`

**內容**
- 開啟頁面時先顯示密碼畫面，沿用儀表板的深色風格；密碼錯誤時顯示提示並抖動輸入框
- 原始碼只存 SHA-256 雜湊，沒有明文密碼；瀏覽器沒有 `crypto.subtle` 時改用內建的 JS 版 SHA-256（已驗證兩者結果一致）
- 驗證通過後存進 sessionStorage（有 try/catch）：同一分頁重新整理不用再輸入，關掉分頁就要重輸
- 通過驗證前不載入 `data/`，也不連 Binance / CoinGecko（已實測：解鎖前沒有任何資料請求）
- 已測試：https 和本機 file:// 兩種開法，錯誤密碼、正確密碼、重新整理都正常

**已知限制**（Sean 已同意）：只擋一般訪客；repo 原始碼和 `data/*.json` 仍然公開。

**換密碼的方法**：不用改程式邏輯，只要把 `index.html` 裡 `PW_HASH` 換成新密碼的 SHA-256 雜湊，由開發端處理。

---

## 2026-11-16 · v1.0.0 首次上線

**狀態**：待首次部署（請依 `DEPLOY.md` 第 2 節）

**變更檔案**：全部（首次部署，含 `data/`）

**內容**
- 由模擬資料原型改為真實資料版本
- 資料管線 `scripts/pipeline.py`：證交所 OpenAPI / MIS、Yahoo Finance、鉅亨網、經濟日報 RSS、FRED（備援 Yahoo ^TNX）、Binance
- 新聞情緒：LLM 評分（無金鑰時退回詞典法）
- 因子模型：邏輯斯迴歸預測 TAIEX 次日漲跌，樣本外回測
- 規則式資產配置 Sankey、資料來源狀態表
- GitHub Actions 排程更新
- 支援本機 `file://` 直接開啟（讀 `data/*.js`）

**部署注意**
- 上傳前刪除 `deploy/` 子資料夾
- 推送 token 需 `workflow` 權限
