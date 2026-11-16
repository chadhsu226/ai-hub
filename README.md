# AI 投資決策中樞 · MCU Finance

動態、持續更新的金融 AI 儀表板。**所有數字皆來自公開資料源**，每個面板都標示來源與抓取時間，頁底有完整的資料來源狀態表。

- 前端：GitHub Pages 靜態頁（`index.html`），每 60 秒重新讀取 `data/dashboard.json`
- 即時：BTC / ETH / SOL 由瀏覽器直連 Binance WebSocket（`data-stream.binance.vision`），失敗時退回 Binance REST → CoinGecko
- 排程：GitHub Actions（台股盤中每 10 分鐘、其餘每小時）執行 `scripts/pipeline.py`，把結果 commit 回 `data/`

## 面板與資料來源

| 面板 | 資料 | 來源 |
|---|---|---|
| 上方指標列 | TAIEX、2330 | 證交所 MIS（`getStockInfo.jsp`，排程快照） |
| | USD/TWD、VIX、費半 | Yahoo Finance spark API |
| | BTC | Binance WebSocket（瀏覽器即時） |
| | AI P(up) | 本專案因子模型 |
| 2330 K 線 | 1 分 K（盤前/假日改 5 分 K 近 5 日） | Yahoo Finance chart API |
| 因子模型網路 | 6 個特徵的 z 分數、係數、貢獻 | 本專案邏輯斯迴歸 |
| 產業熱力圖 | 底色＝新聞情緒、數字＝類股指數漲跌 | 新聞 LLM 評分 + 證交所 OpenAPI `MI_INDEX` |
| 資產配置 Sankey | 規則式權重與流量 | 本專案（規則寫在 `allocate()`） |
| 風控 | 樣本外 VaR / MDD / Sharpe / 波動度，與買進持有對照 | 本專案回測 |
| 新聞情緒 | 鉅亨網 + 經濟日報，逐則評分，附原文連結 | `api.cnyes.com`、經濟日報 RSS |
| 五檔 | 2330 最佳五檔、開高低量 | 證交所 MIS |
| 管線紀錄 | 每次執行的實際事件 | `data/log.json` |
| 跑馬燈 | 權值股收盤、NVDA/AAPL/黃金、ETH/SOL | 證交所 `STOCK_DAY_ALL`、Yahoo、Binance |
| 美 10Y | DGS10 | FRED（失敗時退回 Yahoo `^TNX`） |

## 模型說明

**目標**：TAIEX 下一交易日收盤是否上漲。

**特徵**（皆為收盤時已知資訊，避免前視偏誤）：5 日動能、20 日動能、20 日年化波動度、RSI14、費半前一個美股交易日報酬、台幣 5 日變動。

**估計**：L2 正則化邏輯斯迴歸（numpy 實作）。前 70% 期間訓練、後 30% 樣本外測試；當日預測則以全樣本重估。

**新聞情緒疊加**：`P_ens = σ(logit(P_factor) + w × 情緒EMA)`，`w` 預設 0.5。這是**固定先驗、不是估計值**，因為新聞情緒還沒有足夠歷史可回測；累積數月後可把情緒納入特徵正式估計。

**誠實揭露**：頁面會顯示樣本外準確率與「永遠猜多數方向」的天真基準。若模型沒有勝過基準，頁面會直接標示警告。

## 部署步驟

1. 建立 GitHub repo，把本資料夾全部推上去（含 `data/`，第一次就有資料可看）
2. **Settings → Pages**：Source 選 *Deploy from a branch*，`main` / `/ (root)`
3. **Settings → Secrets and variables → Actions**
   - Secret `ANTHROPIC_API_KEY`：新聞情緒用 LLM 評分（不設也能跑，會退回詞典法並在每則新聞標示）
   - Secret `FRED_API_KEY`（選填）：用 FRED 官方 API，比 CSV 下載穩定
   - Variable `LLM_MODEL`（選填，預設 `claude-haiku-5-5`）、`SENT_WEIGHT`（選填，預設 `0.5`）
4. **Actions** 分頁 → `update-data` → *Run workflow* 手動跑一次，確認綠燈
5. **Settings → Actions → General → Workflow permissions** 需為 *Read and write*（workflow 內已宣告 `contents: write`）

## 本機執行

```bash
pip install -r requirements.txt
python scripts/pipeline.py          # 約 20 秒，產生 data/*.json
python -m http.server 8000          # 開 http://localhost:8000
```

## 已知限制

- 台股資料為排程快照（GitHub cron 實際間隔常為 10–20 分鐘），**不是逐筆即時**；頁面上的時間戳是真實抓取時間
- 證交所 MIS 對海外 IP 的可用性不保證；若 Actions 跑在美國機房被擋，五檔與 TAIEX/2330 會自動改用 Yahoo 報價，資料來源表會顯示 FAIL
- Yahoo 對完整的瀏覽器 User-Agent 字串會回 429，程式刻意使用簡短的 `Mozilla/5.0`，並以 spark API 一次抓多檔來降低請求數
- `MI_INDEX` 類股指數為前一個收盤日資料，熱力圖標題會標示指數日期
- 每次 commit `data/` 會讓 repo 歷史變大；長期可改成推到獨立的 `data` 分支或使用 Pages artifact 部署

## 授權與免責

教學展示用途（銘傳大學財務金融學系「AI 應用於管理」計畫）。不構成投資建議。新聞標題版權屬原媒體，本頁僅顯示標題與連結。
