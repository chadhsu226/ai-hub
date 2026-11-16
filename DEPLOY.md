# DEPLOY.md — AI 投資決策中樞 部署與維運規則

> 給負責 GitHub 的 agent。本資料夾的程式由另一個 agent（開發端）撰寫與優化；你負責上傳 GitHub、設定 Pages、維護頁面上線狀態。**你不修改程式邏輯**，發現問題請回報給 Sean，由開發端修正。

## 0. 角色分工

| 誰 | 負責 | 不做 |
|---|---|---|
| 開發端 agent | 改 `index.html`、`scripts/`、workflow、README；每次改完寫 `CHANGELOG.md` | 不碰 git、不推送 |
| 你（GitHub agent） | 建 repo、推送程式檔、Pages 設定、主站連結、檢查 Actions 是否正常 | 不改程式、不從本機推 `data/`（首次除外） |
| Sean | 在 GitHub 網頁貼上 API 金鑰（Secrets） | — |
| GitHub Actions | 自動抓資料、commit `data/` | — |

## 1. Repo 規劃

- **獨立 repo：`chadhsu226/ai-hub`**（public），**不要**放進 `chadhsu226.github.io` 主站 repo
  - 原因：Actions 盤中約每 10 分鐘 commit 一次 `data/`，放主站會讓主站不斷重建、歷史被洗版、與你的推送互相衝突
- 上線網址：`https://chadhsu226.github.io/ai-hub/`
- 主站 `chadhsu226.github.io` 只需加一個連結指向上述網址

## 2. 首次部署（只做一次）

1. **上傳前清理**：刪除資料夾內的 `deploy/` 子資料夾（建置過程的暫存，不屬於專案）
2. 建立 repo `chadhsu226/ai-hub`，推送**整個資料夾**（這一次要包含 `data/`，讓頁面一上線就有資料）
   - 必須包含隱藏檔：`.github/workflows/update-data.yml`、`.nojekyll`、`.gitignore`
   - 推送用的 token 需有 **`workflow` 權限**，否則 GitHub 會拒收 `.github/workflows/` 下的檔案
3. **Settings → Pages**：Source = *Deploy from a branch*，Branch = `main`，資料夾 = `/ (root)`
4. **Settings → Actions → General → Workflow permissions**：選 *Read and write permissions*
5. **請 Sean 本人**到 **Settings → Secrets and variables → Actions** 新增（金鑰不要經過任何 agent 轉手）：
   - Secret `ANTHROPIC_API_KEY`：新聞情緒 LLM 評分（不設也能跑，會退回詞典法）
   - Secret `FRED_API_KEY`：選填
6. **Actions** 分頁 → `update-data` → *Run workflow* 手動跑一次
7. 驗收（見第 5 節）
8. 在主站加入口連結

## 3. 日常更新流程（開發端每次改完之後）

1. 讀 `CHANGELOG.md` 最上面一筆，確認這次改了哪些檔案
2. **先 `git pull --rebase`**（Actions 一直在 commit `data/`，不先拉一定衝突）
3. 只同步 CHANGELOG 列出的程式檔。可同步的範圍：
   - `index.html`
   - `scripts/`
   - `.github/workflows/`
   - `README.md`、`DEPLOY.md`、`CHANGELOG.md`
   - `requirements.txt`、`.gitignore`、`.nojekyll`
4. commit 訊息格式：`release: <CHANGELOG 該筆的標題>`
5. 推送後到 Actions 分頁手動 *Run workflow* 一次，確認新程式能跑
6. 驗收（第 5 節）

### ⛔ 絕對不要

- **不要推送本機的 `data/`**（首次部署除外）。線上的 `data/` 永遠比本機新，推上去會覆蓋新資料或造成衝突
  - 若 git 顯示 `data/` 有本機變更：`git checkout -- data/` 或 `git stash` 丟掉本機版本，以遠端為準
- 不要 force push（會洗掉 Actions 的資料 commit）
- 不要修改程式內容；有錯回報 Sean
- 不要把任何 API 金鑰寫進檔案或 commit

## 4. 檔案說明

| 路徑 | 說明 | 誰改 |
|---|---|---|
| `index.html` | 前端頁面（單檔） | 開發端 |
| `scripts/pipeline.py` | 資料管線 | 開發端 |
| `.github/workflows/update-data.yml` | 排程：台北時間週一至五 08:00–14:59 每 10 分鐘、其餘每小時 | 開發端 |
| `data/dashboard.json`、`data/dashboard.js` | 前端讀的資料（.js 版供本機 file:// 開啟） | **Actions** |
| `data/log.json`、`data/log.js` | 管線執行紀錄 | **Actions** |
| `data/news_cache.json` | 已評分新聞快取（避免重複呼叫 LLM） | **Actions** |
| `CHANGELOG.md` | 開發端的變更紀錄，你的同步依據 | 開發端 |

## 5. 驗收檢查（每次部署後）

打開 `https://chadhsu226.github.io/ai-hub/`：

- [ ] 右上角「資料更新」時間在 30 分鐘內（盤中）；徽章為綠色 `REAL DATA`，不是橘色 `STALE`
- [ ] 頁面下方「資料來源與狀態」表：大部分為綠色 OK
  - `FRED DGS10` 為 FAIL 是已知狀況（有 Yahoo 備援），可忽略
  - **若「證交所 MIS」為 FAIL**：代表 GitHub 美國機房被證交所擋，回報 Sean（TAIEX/2330 會自動改用 Yahoo，但五檔面板會空白）
- [ ] 左上 BTC 數字每隔幾秒會跳動（Binance 瀏覽器直連）
- [ ] Actions 分頁最近的 `update-data` 為綠色勾勾

## 6. 例行監看與回報

- Actions 連續失敗（紅叉）→ 打開 log，把錯誤訊息整段回報 Sean
- 頁面顯示 `STALE` 超過 2 小時 → 檢查 Actions 是否被停用（GitHub 對 60 天無人推送的 repo 會自動停用排程，需到 Actions 分頁重新啟用）
- repo 太大（> 500 MB）→ 回報 Sean，由開發端改成把資料推到獨立分支
