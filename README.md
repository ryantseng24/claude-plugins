# Uniforce Claude Plugins

Internal Claude Code plugins for Uniforce engineering team.

## Plugins

| Plugin | Version | What it does |
|---|---|---|
| `ccusage-report` | 1.1.0 | 月度 Claude Code 使用量收集（token 成本 + LoC + 採納率），自動提交給技術長 |

## 安裝步驟（一次性）

在你的 Claude Code 終端輸入：

```
/plugin marketplace add ryantseng24/claude-plugins
/plugin install ccusage-report@uniforce-plugins
```

兩行。完成。

## 使用方法

每月初（任何時間都可以），在 Claude Code 對話框輸入：

```
請執行月度使用量報告
```

或直接：

```
ccusage report
```

或：

```
/skill ccusage-report
```

Claude Code 會引導你跑完 6 個步驟，最後自動提交給技術長。

## 跨平台支援

| 環境 | 支援狀態 |
|---|---|
| macOS（原生）| ✅ 完整支援 |
| Linux（原生 / WSL）| ✅ 完整支援 |
| Windows（原生 PowerShell）| ✅ 完整支援（v1.1.0 起） |

## 隱私聲明

本 plugin **只收集統計數據**：
- Token 用量（input / output / cache）
- 模型名稱（claude-opus-4-7 等）
- Edit/Write/MultiEdit 工具呼叫次數與行數
- 採納率（拒絕次數 / 提案次數）

**不會收集**：
- 對話內容
- 檔案內容、檔案名稱、檔案路徑
- 程式碼片段

提交目標為內部 Google Form，由技術長 Ryan 彙整為月度報告。

## 更新

每月會自動拉取最新版（marketplace 預設行為）。手動更新：

```
/plugin marketplace update uniforce-plugins
```

## 開發者

| | |
|---|---|
| Maintainer | Ryan Tseng (`ryan.tseng@uniforce.com.tw`) |
| Repo | https://github.com/ryantseng24/claude-plugins |
| License | MIT |

## 問題回報

如遇問題，請：
1. 截圖錯誤訊息
2. 把 `~/claude-team-stats-<月份>.json` 傳給 Ryan（如果產生了）
3. 直接訊息 Ryan
