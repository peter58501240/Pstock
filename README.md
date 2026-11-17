# Pstock

個人台股投資儀表板。

## GitHub Pages 部署方式
請使用最簡單的分支部署，不使用 GitHub Actions：

1. 進入 **Settings → Pages**
2. **Build and deployment → Source** 選 **Deploy from a branch**
3. **Branch** 選 **main**
4. **Folder** 選 **/(root)**
5. 按 **Save**

部署網址：

**https://peter58501240.github.io/Pstock/**

網站首頁檔 `index.html` 已放在 main 分支根目錄。

## 每日智慧選股（實驗版）

- 網頁：`screener.html`，由首頁「智慧選股」進入；不影響既有持股／損益／圖表。
- GitHub Actions：每日盤後執行 `scripts/run_screener.py`，輸出 `data/screener.json` 至部署網站。
- 股票母體：上市、上櫃四碼普通股；排除 ETF／TDR／金融，且須符合基本量價門檻。
- A：強勢續強；B：止跌轉強；C：潛力觀察。評分僅供排序，不代表勝率或買賣建議。
- 最新日 OHLCV：TWSE STOCK_DAY_ALL（必要時改用 MI_INDEX 日期查詢）＋TPEx 上櫃每日收盤 OpenAPI；歷史日線：Yahoo Finance。
- 歷史資料保留在 GitHub Actions cache，不進版控。第一次執行最多補 650 檔；之後排程會繼續補齊，並在頁面顯示有效覆蓋率。快取失效時可能重新建立。
- 官方行情兩市場不同日期、部分下載失敗、歷史資料未滿 70 根等情況，都會在頁面顯示「資料未齊」，不得當作全市場完整排名。
- 手動更新：GitHub → Actions → Deploy Pstock to GitHub Pages → Run workflow。排程預計台灣時間 14:10 更新、15:35 再檢查資料是否補齊，GitHub 可能延遲。
- 本功能尚未執行嚴謹回測。不得把選股分數、止損參考價當成經過績效驗證的交易系統。
