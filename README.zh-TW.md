# 旅行規劃管理助手 — LINE旅行規劃Bot

[English](./README.md)

一個LINE Bot，會在旅程進行中被動讀取群組對話，用Claude維護一份即時更新的markdown文件，記錄已經決定跟還沒決定的事情，並透過LIFF網頁呈現，內建記帳分攤跟投票功能。

設計對象是2-10人的朋友小團體一起規劃旅行——不是多租戶SaaS產品。完整的設計脈絡、參考的「LLM-Wiki」架構、開發過程中踩過的坑跟修法，可以參考[`docs/user_stories.md`](docs/user_stories.md)跟[`docs/plan.md`](docs/plan.md)。

## 功能

- **被動記錄**：旅程進行中，群組裡的每則訊息（含照片）都會被記錄下來。其他時間Bot完全不插話，也不會主動說話，除非有指令觸發。
- **LLM整理文件**：定時（也可以用`/整理`手動觸發）讓Claude把新訊息整理進一份markdown文件——時間軸、未定事項、其他資訊。每個結論旁邊都附來源引用（誰、什麼時候說的）。
- **有根據的問答**：`/問 <問題>`只根據文件內容回答，不會用模型自己的知識瞎猜，文件裡沒有答案就明講「還沒決定」。
- **LIFF頁面**：`/懶人包`取得一個可分享的網頁連結，顯示目前文件、記帳、進行中的投票。
- **記帳分攤**：`/記帳`記錄誰代墊了多少，用貪心「settle up」演算法算出結算建議，編輯要到LIFF頁面操作。
- **投票**：`/投票`開複選投票，實際投票動作在LIFF頁面進行，避免洗版聊天室。
- **查核機制**：便宜的第二輪LLM檢查（只在cron跑），找出整理時有沒有加進沒根據的內容，用`/檢查`查詢。
- **收回訊息處理**：如果有人收回訊息，下次整理時Bot會把對應加進文件的內容修正或移除（照片也會一併從備份刪除）。

在群組裡下`/說明`可以看完整指令列表。

## 使用方式

所有指令都是直接在LINE群組裡打繁體中文的斜線指令。`/說明`隨時能看到最新版的指令列表。

**🧳 旅程管理**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/旅程 開始 <名稱>` | 開始一趟旅程，從此刻起群組裡的每則訊息都會被記錄。 | `/旅程 開始 東京五日遊` → `🧳 旅程「東京五日遊」開始了！我會開始記錄接下來的討論。` |
| `/旅程 結束` | 結束目前旅程：先把還沒整理的訊息整理進文件，再附上最終記帳結算。只有開始的人能結束（或用`ADMIN_USER_ID`覆寫）。 | `/旅程 結束` → `🏁 旅程「東京五日遊」結束了！` ＋一則結算訊息 |

**🔍 查詢（唯讀，隨時可用）**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/問 <問題>` | 只根據旅程文件回答，不會瞎猜，文件沒提到就說「還沒決定」。 | `/問 我們住哪間飯店` → 根據實際討論內容回答，或`⚠️ 還沒決定` |
| `/未定事項` | 列出目前所有還沒決定的事項。 | `/未定事項` → 目前「未定事項」章節的純文字版本 |
| `/懶人包` | 傳送LIFF頁面連結（時間軸、記帳、投票都在同一頁）。 | `/懶人包` → 一則連到LIFF頁面的LINE按鈕訊息 |
| `/花費` | 顯示這趟旅程累積的LLM API花費。 | `/花費` → `🤖 這趟旅程：12次AI呼叫，約 US$0.0187（輸入9,204／輸出2,150 tokens）` |
| `/結算` | 顯示目前記帳結算狀況，不會結束旅程。 | `/結算` → 誰該付誰多少錢（貪心settle-up演算法算出來的） |

**💰 記帳**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/記帳 <金額> <說明> [@人...]` | 記錄誰代墊了多少。付款人一定算進分攤名單（LINE本來就不能@自己）。沒@人的話，分攤對象是這趟旅程期間發言過的所有人。 | `/記帳 1500 晚餐燒肉 @小華 @小明` → `💰 已記錄：Neo 代墊 1500元（晚餐燒肉），由3人分攤（Neo、小華、小明），每人約500.00元` |

編輯或刪除已記錄的帳目只能在LIFF頁面操作（`/懶人包`）——這種需要重複挑選特定項目的動作用聊天指令會洗版。

**🗳️ 投票**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/投票 開始 <題目>` | 開一個新投票（同一趟旅程同時只能有一個進行中）。 | `/投票 開始 晚餐吃什麼` → `🗳️ 投票「晚餐吃什麼」開始了！...` |
| `/投票 新增 <選項>` | 加入候選項目。 | `/投票 新增 燒肉` → `➕ 已新增選項「燒肉」。目前選項：\n1. 燒肉` |
| `/投票 結果` | 查看目前得票狀況跟每個人投了什麼。 | `/投票 結果` → 各選項票數＋投票者名單 |
| `/投票 結束` | 鎖定結果，任何人都能結束。 | `/投票 結束` → `🏁 投票結束！` ＋最終結果 |

實際投票／取消投票只能在LIFF頁面操作（`/懶人包`），可複選、點一下切換——同樣是為了不讓每一票都變成一則聊天訊息。

**⚙️ 其他**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/整理` | 手動觸發整理（平常每小時會自動跑一次）。 | `/整理` → `✅ 整理完成，處理了 8 則訊息。`或`⚠️ 目前沒有新訊息可整理` |
| `/檢查` | 查看查核機制有沒有抓到跟原始訊息對不上的可疑內容。 | `/檢查` → 可疑內容清單＋原因，或`✅ 目前沒有發現可疑內容` |
| `/模型` | 查看整理／問答／查核三個階段各用哪個模型；帶參數可修改（僅限管理員）。 | `/模型` → 目前設定清單；`/模型 整理 sonnet` → `✅ 已把「整理」的模型設定為 claude-sonnet-5` |
| `/說明` | 顯示完整指令列表。 | — |

## 部署前該知道的設計決策

- **所有回覆都靠指令觸發。** Bot唯一會主動做的事只有旅程進行中被動記錄訊息，不會有任何未經要求的插話。
- **LIFF頁面沒有存取控制。** 有連結的人都能看、都能編輯（記帳、投票等）。這符合小型朋友團體的信任模型，不適合有不受信任成員的場合。
- **同一個群組同時只能有一趟進行中的旅程。** 用`/旅程 開始`開始新的一趟，用`/旅程 結束`結束目前這趟（只有開始的人能結束，除非設定了`ADMIN_USER_ID`覆寫權限，見下方）。
- **目前只有繁體中文介面**，所有指令、回覆、LIFF介面文字都是寫死的繁體中文。

## 架構

- **Cloudflare Python Workers**（Pyodide執行環境）撐起整個後端——一個`fetch()` handler處理LINE webhook跟LIFF的API，一個`scheduled()` handler每小時掃描並整理/查核。
- **D1**（SQLite）存所有關聯式資料：訊息、旅程文件跟版本歷史、記帳、投票、LLM花費追蹤。
- **R2**存照片備份。
- **Anthropic API**（預設整理用`claude-sonnet-5`、問答跟查核用`claude-haiku-4-5`）透過官方Python SDK呼叫——同步client在Workers環境下用不了，必須用`AsyncAnthropic`。每個階段實際用哪個模型是執行期可調的設定（`/模型`，僅限管理員修改），不是寫死的常數，見下方。也可以選用OpenAI模型（`gpt-5`／`gpt-5-mini`）取代，只要設定了`OPENAI_API_KEY`。
- **LINE Messaging API**負責Bot本體，另外需要一個獨立的**LINE Login** channel給LIFF app用（LINE政策已不允許LIFF掛在Messaging API channel下）。

有一個比較特殊、值得知道的平台限制，如果要動webhook那條路徑的話：Cloudflare的`fetch()` handler有一個大約30秒的硬性執行上限，而且被平台強制中斷時**不會產生Python看得到的例外**，所以`try/except`有時候完全補不到。每小時的cron排程（`scheduled()`）不受這個限制，有自己獨立大約15分鐘的執行時間額度——這是為什麼任何可能跑比較久的工作都應該靠它，而不是webhook路徑。完整的來龍去脈記錄在`docs/plan.md`的發現清單裡。

## 費用

以朋友團體的用量來說，現實上**基本上是0元／月**。可能產生費用的地方：

- **Cloudflare（Workers、D1、R2、Cron Trigger）**：免費額度就夠用。一趟旅程的訊息量、D1讀寫次數、照片儲存空間，都遠遠碰不到免費額度的上限（Workers：每天10萬次請求；D1：5GB儲存空間、每天500萬次讀取＋10萬次寫入；R2：10GB儲存空間、沒有流量費）。這個專案開發至今從沒需要升級付費方案。
- **LINE Messaging API**：0元。Bot所有的回覆都是「reply訊息」（由指令觸發、用事件自帶的reply token），LINE不會針對這種訊息收費，也不計入任何額度。Bot完全不會主動發送push訊息。
- **LLM API呼叫**：唯一真正會花錢的地方，按token計費，實際費率看`/模型`把各階段設成哪個模型。預設：整理用`claude-sonnet-5`（每100萬input/output tokens各`$2`／`$10`），問答跟查核用`claude-haiku-4-5`（各`$1`／`$5`）。若改設OpenAI：`gpt-5`（各`$1.25`／`$10`）或`gpt-5-mini`（各`$0.25`／`$2`）。整理是批次處理、有上限，不是每則訊息都打一次API，而且只有真的有新內容待整理時才會呼叫——實測一趟正常聊天量的旅程，一個月的花費遠低於1美金。用`/花費`隨時可以查到你這趟旅程實際累積的美金花費。

有一個值得知道的情境：如果每小時的cron排程因為某些原因連續好幾次沒有成功清完積壓訊息（開發過程中真的發生過一次，細節記在`docs/plan.md`的發現清單裡），積壓的內容只是需要多跑幾個批次才能清完，**不會**變成無限重試迴圈、也不會因此讓花費暴增——`max_retries=0`就是特意為了避免這種情況設的。

## 建置步驟

需要準備：Cloudflare帳號、LINE Developers帳號、`uv`、Anthropic API key。

### 1. LINE Messaging API channel（Bot本體）

1. 在[LINE Developers Console](https://developers.line.biz/)建立一個Messaging API channel。
2. 記下**Channel secret**，並發一個長期有效的**Channel access token**。
3. 到[manager.line.biz](https://manager.line.biz/)（不是開發者後台）找到這個官方帳號，開啟**「允許加入群組聊天」**。這個設定很容易漏掉，沒開的話Bot完全收不到群組訊息。
4. webhook網址先留白，等deploy完再回來設定。

### 2. LINE Login channel（給LIFF app用）

1. 建立一個**LINE Login** channel（跟上面的Messaging API channel是分開的）。
2. 底下加一個LIFF app，endpoint網址設成`https://<你的worker>.workers.dev/liff`，scope選`profile`。
3. 記下LIFF ID。
4. 把這個channel發布（預設是「Developing」狀態，只有你自己能用LIFF頁面，其他人會被擋）。

### 3. Cloudflare資源

```sh
cd worker
npm install
npx wrangler login   # 如果還沒登入過
npx wrangler d1 create itinerary-manager-db   # 記下印出來的database_id
npx wrangler r2 bucket create <一個全域唯一的bucket名稱>
```

到Cloudflare dashboard把R2 bucket的公開存取打開（進bucket → Settings），記下它給的`pub-*.r2.dev`網址。

把`wrangler.jsonc.example`複製成`wrangler.jsonc`，把上面拿到的database名稱/ID、bucket名稱、R2公開網址、LIFF ID填進去。

套用schema：

```sh
npx wrangler d1 execute itinerary-manager-db --remote --file schema.sql
```

### 4. Secrets

```sh
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put LINE_CHANNEL_SECRET
npx wrangler secret put LINE_CHANNEL_ACCESS_TOKEN
npx wrangler secret put ADMIN_USER_ID   # 選填，見下方說明
npx wrangler secret put OPENAI_API_KEY  # 選填，見下方說明
```

本機開發的話，把`.dev.vars.example`複製成`.dev.vars`，填入一樣的值。

`ADMIN_USER_ID`是選填的：設定後，這個LINE user ID可以結束（或強制結束）任何旅程，不受「只有開始的人能結束」限制。不需要這個功能的話留空即可。

`OPENAI_API_KEY`是選填的：只有想讓`/模型`裡的`gpt5`／`gpt5mini`這兩個別名能用才需要設定（見下方）。不設定的話就只能用Claude模型。

### 5. Deploy並接上webhook

```sh
npm run deploy   # uv run pywrangler deploy
```

把Messaging API channel的webhook網址設成`https://<你的worker>.workers.dev/webhook`並在LINE Developers Console驗證。把Bot加進群組，下`/旅程 開始 <名稱>`，應該就會開始記錄了。

> **建議群組裡每個人都額外加這個Bot為一對一好友。** 顯示名稱是透過LINE的1:1 profile API查詢的，只有加過Bot好友的人才查得到名字，沒加好友的人在旅程文件跟回覆裡會顯示成他原始的LINE user ID，不會顯示名字。如果之後才補加好友，下次查詢到名字時會自動修正，不用重新做任何設定。

### 本機開發

```sh
uv venv && uv sync   # 讓編輯器有autocomplete跟型別提示
npm run dev           # uv run pywrangler dev，本機開發伺服器
```

注意：`ctx.waitUntil()`（所有webhook背景處理都靠它）在本機開發環境下不會正確執行——任何HTTP即時回應之後的邏輯，都要實際deploy才能測試。

## License

[MIT](./LICENSE)
