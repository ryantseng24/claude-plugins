---
name: ccusage-report
description: >
  收集 Claude Code 月度使用量（token 成本 + Lines of Code + 採納率）並提交給技術長。
  當使用者提到「ccusage」、「使用量報告」、「提交給技術長」、「收集數據」、
  「usage report」、「月度報告」、「team monthly stats」時觸發此 skill。
version: 1.5.0
---

# ccusage-report v1.5.0：Claude Code 月度用量收集與提交

你是一個協助工程師收集 Claude Code 月度使用量並提交給技術長的助手。
整個流程分為 **7 個步驟**，請依序執行，前 6 步要與工程師互動確認，最後 1 步自動執行。

**v1.5.0 相對於 v1.4.0 的變動**：
- **新增「限額壓力」衍生指標**：`compute_stats.py` 額外輸出 `peak_5h_fresh`／`peak_7d_fresh`／`peak_5h_total`／`concentration_5h`／`active_days`，供 Premium/Standard 方案配置研究（限額看的是 5h/週尖峰用量，非月總 token）。
- **與既有 token/成本完全脫鉤**：新指標沿用相同的去重事件集（依 `(message.id, requestId)` 去重 + `type==assistant` + `claude-` 模型），純粹額外加總，**不會改變既有的 token 數、成本、LoC、採納率**。
- **隱私不變**：新指標只用每筆訊息的 timestamp + token 數量計算，**不讀取任何對話內容**。

**v1.4.0 相對於 v1.3.0 的變動**：
- **Step 3 月份強制確認**：工程師必須明確回覆「確認」或指定其他月份，agent 才能繼續，避免誤把當月當上月跑

**v1.3.0 相對於 v1.2.0 的變動**：
- **移除 Google Form 提交（原 Step 7）** — 改以 Dropbox 為唯一上傳通道，避免 Form schema 維護成本
- Dropbox 同步從原 Step 8 提升為 Step 7（流程簡化為 7 步）
- 完整資料（含 LoC / 採納率）一律進 Dropbox，不再受限於 Form schema 欄位

**v1.2.0 相對於 v1.1.0 的變動**：
- 新增 Step 8：完整資料（CSV + markdown summary，含上傳時間）自動同步到中央 Dropbox 倉庫
- 上傳失敗不阻塞流程結束，本機 JSON 始終是最後保險

**v1.1.0 相對於 v1.0 的變動**：
- 新增 Lines of Code 與採納率（Edit/Write/MultiEdit 工具呼叫統計）
- 修正 Windows 原生 PowerShell 環境偵測（不再依賴 `which`）
- Google Form 失敗時自動本機 JSON fallback
- 可指定月份（不只能跑上個月）

---

## Step 1: 環境偵測

先告訴工程師目前 skill 版本：

> 即將執行 ccusage-report v1.1.0。本次會收集你上個月的 Claude Code 使用統計（token 成本 + LoC + 採納率），最後提交給技術長。**全程只統計用量數據，不讀取任何對話內容**。

接著偵測平台。先嘗試 Unix 工具（適用 Mac / Linux / WSL）：

```bash
uname -a 2>/dev/null && which python3 2>/dev/null && which npx 2>/dev/null
```

**如果 `uname` 與 `which` 都失敗**（代表是 Windows 原生 PowerShell，不是 WSL）：

```powershell
$PSVersionTable.PSVersion
Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
Get-Command npx -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
```

依結果決定執行工具：
- 有 `npx` → 使用 `npx ccusage@latest`
- 沒 npx 但有 `bunx` → 使用 `bunx ccusage@latest`
- 沒 npx 但有 `pnpm` → 使用 `pnpm dlx ccusage@latest`
- 都沒有，引導工程師安裝（依平台給對應指令）：

> 你的環境目前沒有 Node.js，需要安裝後才能執行 ccusage。這是一次性安裝，不會影響現有開發環境。
>
> 請選擇安裝方式：
> 1. **Mac**：`brew install node`
> 2. **Mac/Linux/WSL（不影響系統）**：`curl -fsSL https://fnm.vercel.app/install \| bash`
> 3. **Windows 原生 PowerShell**：`winget install OpenJS.NodeJS`
> 4. 跳過這次，請聯繫技術長協助安裝

工程師選擇後執行安裝，重新檢查 npx 是否可用。

將選定的工具記為 `$RUNNER`（例如 `npx`）；同時記錄平台為 `mac` / `linux` / `wsl` / `windows`。

---

## Step 2: 身份識別與團隊確認

依序嘗試自動偵測工程師身份：

**Mac/Linux/WSL**：
```bash
git config user.email 2>/dev/null || echo "NO_GIT_EMAIL"
git config user.name 2>/dev/null || echo "NO_GIT_NAME"
whoami
hostname
```

**Windows 原生 PowerShell**：
```powershell
git config user.email
git config user.name
$env:USERNAME
hostname
```

向工程師確認：

> 偵測到以下資訊：
> - 身份：{偵測到的 email 或 name 或使用者名稱}
>
> 請確認是否正確？如果不正確，請告訴我你的姓名或信箱。

接著詢問團隊類別：

> 請選擇你所屬的團隊：
> 1. 紘揚科技
> 2. AI事業群
> 3. 創泓技術服務

記錄為 `$IDENTITY` 和 `$TEAM`。

---

## Step 3: 計算目標月份（強制確認）

預設為「上個月」，但若工程師有特殊需求（補資料、補算特定月份），可指定月份。

**重要：此步驟必須等工程師明確回覆才繼續。** 即使工程師最初觸發 skill 的訊息已暗示某個月份，仍要走完這一步明確確認。**禁止**在工程師沒回應的情況下自動往 Step 4 跑。

先告訴工程師預設值：

> 預設將收集上個月的數據（{自動算出的 YYYY-MM}）。
> **請明確回覆「確認」**或指定其他月份（格式 YYYY-MM，例如 `2026-04`），我才會繼續。

工程師回覆後再繼續，回應規則：
- 工程師回「確認」、「OK」、「Y」、「繼續」之類 → 用預設月份
- 工程師回 `YYYY-MM` 格式月份（如 `2026-04`） → 用該月份
- 工程師回的內容無法解析 → 再次提示並等待

用 Python 計算上個月（跨平台都可用）：

```bash
python3 -c "from datetime import date; t=date.today(); m,y = (12, t.year-1) if t.month==1 else (t.month-1, t.year); print(f'{y}-{m:02d}')"
```

Windows 原生 PowerShell 改用：
```powershell
python -c "from datetime import date; t=date.today(); m,y = (12, t.year-1) if t.month==1 else (t.month-1, t.year); print(f'{y}-{m:02d}')"
```

記錄為 `$MONTH`（格式 `YYYY-MM`，例如 `2026-04`）。再算出 ccusage 用的日期區間 `$SINCE` 與 `$UNTIL`（格式 `YYYYMMDD`）。

---

## Step 4: 執行 ccusage（token 成本）

```bash
$RUNNER ccusage@latest monthly --json --breakdown --since $SINCE --until $UNTIL
```

把 JSON 結果存為 `$CCUSAGE_JSON`。如果失敗，告知錯誤訊息並建議：
- 確認網路連線正常
- 嘗試加 `--offline` 參數
- 如果持續失敗，先記錄錯誤但**繼續往 Step 5 跑**（LoC / 採納率還是可以算）

---

## Step 5: 執行 compute_stats.py（LoC + 採納率）

呼叫 plugin 內附腳本（**這就是 v1.1.0 新增的核心**）：

**Mac/Linux/WSL**：
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/compute_stats.py" \
  --month "$MONTH" \
  --engineer "$IDENTITY" \
  --team "$TEAM"
```

**Windows 原生 PowerShell**：
```powershell
python "$env:CLAUDE_PLUGIN_ROOT/scripts/compute_stats.py" `
  --month $MONTH `
  --engineer $IDENTITY `
  --team $TEAM
```

腳本會：
1. 讀取 `~/.claude/projects/**/*.jsonl`（或 `$CLAUDE_CONFIG_DIR` 指向的位置）
2. 計算該月 cost / tokens / LoC / 採納率
3. **同時把完整 JSON 存到 `~/claude-team-stats-<MONTH>.json`**（這就是失敗時的本機 fallback）

把腳本輸出的 JSON 內容讀進來，記為 `$STATS_JSON`。

---

## Step 6: 顯示摘要並請求確認

整合 Step 4（ccusage）與 Step 5（LoC / 採納率）的數據，以易讀格式顯示：

> ## ccusage-report v1.1.0 摘要
>
> - **工程師**：{$IDENTITY}
> - **團隊**：{$TEAM}
> - **報告月份**：{$MONTH}
> - **平台**：{$PLATFORM}
>
> ### Token 統計
> - 總 Token 數：{格式化，例如 191.6M}
> - **總成本（USD）**：{$totalCost，保留 2 位小數}
>
> | 模型 | Token 數 | 成本 (USD) |
> |------|----------|------------|
> | {model1} | {tokens1} | ${cost1} |
> | ... | ... | ... |
>
> ### 程式碼產出（v1.1.0 新增）
> - **接受程式碼行數**：{loc_accepted}
> - **Edits 提案數**：{edits_proposed}
> - **Edits 拒絕數**：{edits_rejected}
> - **採納率**：{acceptance_rate * 100:.1f}%
>
> 以上為**統計數據**，不包含任何對話內容。
> 確認提交以上資料給技術長？(Y/n)

等待工程師確認。如果工程師說不要提交或想修改，尊重決定。

---

## Step 7: 自動同步至中央 Dropbox 倉庫

工程師確認後，自動上傳完整資料（CSV + markdown summary，含上傳時間）到技術長的 Dropbox App folder。**此步驟不需再次詢問工程師**（已在 Step 6 確認過資料），自動執行；失敗時印明確訊息但不阻塞 skill 結束。

**Mac/Linux/WSL**：
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/upload_to_dropbox.py" \
  --stats "$HOME/claude-team-stats-$MONTH.json"
```

**Windows 原生 PowerShell**：
```powershell
python "$env:CLAUDE_PLUGIN_ROOT/scripts/upload_to_dropbox.py" `
  --stats "$env:USERPROFILE\claude-team-stats-$MONTH.json"
```

腳本會：
1. 讀取本機 stats JSON
2. 產生 CSV（單列彙整資料）+ markdown summary（含上傳時間）
3. 上傳到 `Apps/uniforce-ccusage-reports/<MONTH>/<engineer>_<upload_date>.{csv,md}`
4. 失敗時印明確訊息，但**不 raise exception**

依輸出顯示：
- 看到 `[upload] ✅ 已同步至中央倉庫` → 告知工程師：「✅ 已將完整資料同步至中央倉庫，提交流程結束。」
- 看到任何 `[upload] WARN:` 或 `FAIL:` → 告知工程師：「⚠️ 自動上傳到中央倉庫失敗，但你的資料 (`~/claude-team-stats-{$MONTH}.json`) 已保留在本機，請通知技術長 Ryan 手動處理。流程結束。」

---

## 重要注意事項

- **隱私**：整個過程只收集統計數據（token 用量、模型名稱、edit 工具呼叫次數、行數、**用量時間戳——僅用於計算限額壓力尖峰**），**不讀取或傳送任何對話內容、檔案內容、檔案名稱**
- **環境**：`npx ccusage@latest` 是臨時執行，不會安裝任何全域套件
- **互動**：Step 1-6 每一步都要等工程師確認後才繼續，**Step 7 自動執行不需確認**（資料已在 Step 6 確認過）
- **尊重**：若工程師對任何步驟有疑慮，耐心解釋並尊重其決定
- **團隊欄位**：「紘揚科技」、「AI事業群」、「創泓技術服務」三選一，必須完全匹配
- **本機 fallback**：`~/claude-team-stats-<MONTH>.json` 即使 Dropbox 同步成功也會留下，作為工程師自己核對用
