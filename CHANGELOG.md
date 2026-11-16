# CHANGELOG

> 開發端每次修改後在最上方新增一筆。GitHub agent 依「變更檔案」同步，`data/` 永遠不在同步範圍內（首次部署除外）。

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
