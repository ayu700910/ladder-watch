# ladder-watch — 台指期階梯到價盯盤

GitHub Actions 每 5 分鐘抓期交所台指期報價（**日盤 + 夜盤**），跌到 `state.json`
裡的階梯點位就推 Telegram。筆電關機也照常運作。

搭配 [MarketDashboard](../) 的「📉 左側加碼階梯」使用：點位由儀錶板的
「🔔 設為到價警示」同步過來，或直接改 `state.json`。

## 手機改點位

用 GitHub App 開 `state.json` 直接編輯 commit：

```jsonc
{
  "enabled": true,          // false = 全部停用
  "start_price": 45000,     // 起跌點（算跌幅 % 用）
  "levels": [
    { "price": 41000, "note": "" }   // note 可寫備忘，會出現在通知裡
  ],
  "armed": { "41000": true } // true = 武裝中；觸發後自動變 false
}
```

加新點位時記得 `armed` 也補一筆 `true`。

## 行為

- 跳空一次跌穿多階 → **合併成一則**通知，並提醒「只執行最近一階，不補階」
- 觸發後該階靜音；價格回升超過 階梯點 × 1.015 自動重新武裝
- Secrets：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`

## 本機測試

```bash
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx python3 watch.py --force-price 40950
```
