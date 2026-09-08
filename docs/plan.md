# 旅行規劃管理助手 — 技術規劃

## Context

`docs/user_stories.md` 已定案：一個被動加入 LINE 群組的 Bot，在主揪下 `/旅程 開始` 後記錄訊息、自動分類「已決定/未定事項」，全部互動一律指令觸發（`/問`、`/未定事項`），並透過 LIFF 頁面即時查看整理結果。個人/朋友團規模，主機採雲端代管。

本次規劃前已用 WebSearch 查證兩個攸關系統能否成立的前提：

1. **Bot 能被動收到群組全員訊息**：LINE Official Account 加入群組聊天室後，只要在 LINE Developers Console 開啟「Allow bot to join group chats」，就會像 1 對 1 聊天一樣，對群組內每個人的每則訊息都收到 webhook message event，不需要 @提及才觸發。（[LINE Developers - Group chats](https://developers.line.biz/en/docs/messaging-api/group-chats/)）
2. **回覆訊息（reply message）完全免費、不計入方案額度**：只有 push/multicast/broadcast/narrowcast 才算進每月訊息額度（免費方案 200則/月）。因為我們設計成「全部指令化」，Bot 一律用 replyToken 回覆，MVP 完全不需要主動推播，等於**訊息成本是 $0**，這與「全指令化」的產品決策互相印證。（[LINE Developers - Pricing](https://developers.line.biz/en/docs/messaging-api/pricing/)）

**一個需要跟你對齊的修正**：LINE 沒有公開的方式可以「連結回某一則過去的群組訊息」（無法產生deep link跳轉到聊天室的特定訊息）。所以 US-B2「附上原始訊息出處」的「溯源」，實際只能做到：**引用是誰、什麼時候說的、以及原話文字**（我們自己資料庫存的），而不是一個可點擊跳回 LINE 聊天室的連結。照片則可以連到我們自己備份在雲端的圖檔連結（這是我們能控制的）。下面改成markdown文件方案後，這些引用會直接寫進文件內文裡（每個段落旁一句「依XXX於MM/DD提及」），`/問`、LIFF顯示都是讀同一份文件，來源自然就帶著走，不用另外設計引用欄位。

另外查證到 Rich Menu 的行為在群組聊天室中不夠明確可靠，且你要求「Bot吐訊息都要指令觸發」，所以**拿掉 Rich Menu**，改用一個 `/懶人包` 指令直接回覆 LIFF 連結，更簡單也更符合全指令化的原則。

---

## 技術棧選擇（Cloudflare Workers 全家桶）

| 用途 | 選擇 | 為什麼（lazy 理由） |
|---|---|---|
| Webhook + API 主機 | Cloudflare Workers | 免費額度大（10萬request/天）、無「冷啟動/專案睡眠」問題——很適合這種「一年可能只有幾趟旅程、其餘時間完全沒流量」的間歇性使用模式。單一部署（`wrangler deploy`），不用管伺服器。 |
| 資料庫 | Cloudflare D1（SQLite） | 資料量小（trips/messages/items），SQLite關聯式模型完全夠用，跟Workers同平台，設定最少。 |
| 照片儲存 | Cloudflare R2 | S3相容物件儲存，免費10GB、免出口流量費，符合「長期保留不設期限」的需求。 |
| LIFF 前端 | 靜態頁面，由同一個Worker或Cloudflare Pages託管 | 資料量小，不需要框架級SPA，一支HTML+少量JS打API即可。前端是瀏覽器端JS，跟後端語言無關，這點不受下面的語言選擇影響。 |
| 後端語言 | Python（Cloudflare Python Workers） | 你自己比較熟悉/順手的語言。2026年Cloudflare的Python Workers已是一等公民，D1/R2/Cron Triggers這些我們要用的binding都原生支援，跑在Pyodide(WebAssembly)上，有memory snapshot時冷啟動約1秒（TS的V8更快，但webhook場景這點差異感覺不出來）。風險點：不是所有PyPI套件都能裝，尤其是依賴C extension的套件。**已於Phase 0實測確認**：Anthropic官方Python SDK（用`AsyncAnthropic`非同步版本，因為Workers只有async fetch、沒有原始socket，同步版client行不通）在Pyodide下import、建立client、真實API呼叫全部正常，連原本擔心的`pydantic-core`（Rust編譯的依賴）都有現成的Pyodide相容wheel可裝，不需要退回手動`httpx`呼叫REST API。 |
| LLM | Anthropic Claude API | **整理**（`organizeTrip`，維護文件本體的核心任務，判斷相關性、合併討論、決定要不要覆寫既有段落）用品質較好的模型（Sonnet）；**`/問`回答**只是讀已經整理乾淨的文件回答問題，任務簡單很多，用便宜快速模型（Haiku）即可。呼應LLM-Wiki精神：真正花心思的是維護wiki本身，query-time只是讀取。不做embedding/向量資料庫——單趟旅程的文件很小，直接塞進prompt context即可，不需要RAG基礎設施。 |
| 排程 | Cloudflare Cron Triggers | Workers內建功能，設定一行cron表達式即可定期執行，不用額外架排程服務，用來做批次整理（見下）。 |

之所以不用 Node.js+Cloud Run+Postgres 這類方案：Cloud Run背後常見的Cloud SQL沒有免費長期閒置選項，這種「一年用幾次」的使用模式下持續產生费用不划算；Supabase雖然一站式，但免費專案閒置一段時間會被暫停，重新喚醒可能讓使用者打開LIFF時卡住，不適合這種間歇性使用場景。

---

## 資料模型（D1 / SQLite）

```sql
trips (
  id TEXT PRIMARY KEY,          -- uuid
  line_group_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,         -- 'active' | 'ended'
  owner_line_user_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER
)
-- index (line_group_id, status) 用來快速找出某群組目前進行中的旅程
-- unique index (line_group_id) WHERE status='active'，DB層強制一個群組同時最多一趟進行中旅程

messages (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  line_message_id TEXT NOT NULL UNIQUE,   -- 用來做webhook重送的去重
  line_user_id TEXT NOT NULL,
  user_display_name TEXT,
  msg_type TEXT NOT NULL,       -- text | image | video | sticker | ...
  text TEXT,
  sent_at INTEGER NOT NULL,
  organized_at INTEGER          -- NULL=尚未併入trip_docs；批次整理處理過後才填時間戳，內容本身仍不可變
)
-- index (trip_id, organized_at) 用來快速撈出某趟旅程「尚未整理」的訊息

llm_usage (                     -- 每次Claude呼叫都記一筆，從第一次呼叫就開始追蹤
  id TEXT PRIMARY KEY,
  trip_id TEXT REFERENCES trips(id),
  purpose TEXT NOT NULL,        -- 'organize' | 'answer'
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  estimated_cost_usd REAL NOT NULL,
  created_at INTEGER NOT NULL
)

trip_docs (                     -- 整理結果本體：一趟旅程一份markdown文件（LLM維護、可自由組織結構）
  trip_id TEXT PRIMARY KEY REFERENCES trips(id),
  content_md TEXT NOT NULL,
  updated_at INTEGER NOT NULL
)

trip_doc_revisions (            -- 每次有效更新都存一版，避免LLM整份改寫時悄悄弄丟內容，可回溯
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  content_md TEXT NOT NULL,
  triggered_by_message_id TEXT REFERENCES messages(id),
  created_at INTEGER NOT NULL
)

attachments (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id),
  r2_key TEXT NOT NULL,
  content_type TEXT NOT NULL
)
```

---

## Webhook 處理流程

`POST /webhook`（單一Worker fetch handler）：

1. 用 channel secret 驗證 `X-Line-Signature`（Python stdlib `hmac`/`hashlib` 算HMAC-SHA256比對，不依賴任何官方LINE bot SDK）。
2. 對每個 message event：先用 `line_message_id` 查 `messages` 表去重（LINE可能重送webhook）。
3. 文字訊息以 `/` 開頭 → 進指令路由：
   - `/旅程 開始 <名稱>`：該群組若已有 active trip 則回覆錯誤；否則新增 trip row（owner=發話者），回覆確認。
   - `/旅程 結束`：檢查發話者是 active trip 的 owner，更新 status=ended，回覆確認。
   - `/問 <問題>`：取該群組目前 active trip（沒有則取最近一趟ended），讀出 `trip_docs.content_md`（整份文件，含文件裡已經寫好的來源引用）丟給 Claude 當context，要求「只根據提供的文件內容回答，找不到就說不確定」，回覆答案（文件裡已經有引用文字，Claude回答時一併帶出即可）。這個Claude呼叫也記進 `llm_usage`（purpose='answer'）。
   - `/未定事項`：讀 `trip_docs.content_md`，抓出 `## 未定事項` 這個章節（到下一個 `## ` 為止）直接回覆，不需要額外LLM呼叫；沒有進行中旅程或該章節是空的則提示。
   - `/整理`：手動觸發批次整理（見下方「整理邏輯」），把目前所有累積未整理的訊息一次併入文件，回覆「整理完成，更新了N則訊息」或「目前沒有新訊息可整理」。
   - `/懶人包`：取該群組的 active 或最近 ended trip，回覆一個LINE按鈕樣板訊息，URI導向 `https://liff.line.me/<liffId>?tripId=<id>`（用query string帶tripId，不依賴`liff.getContext()`判斷群組，更簡單可靠）。
   - `/花費`：查 `llm_usage`，回覆這趟旅程累積的token數與估算費用（見下方「Token/費用追蹤」）。
   - `/說明`：靜態文字，列出所有可用指令與用途。
4. 非指令文字/圖片訊息，且該群組有 active trip：
   - 寫入 `messages`（`organized_at`留NULL）。
   - 若是圖片：立刻呼叫 `GET https://api-data.line.me/v2/bot/message/{messageId}/content` 下載內容，上傳到R2（LINE的訊息內容有下載時效，必須即時處理），寫入 `attachments`。
   - **不**在這裡呼叫LLM——整理是批次觸發的（見下方），單純累積原始訊息，才不會每則訊息都打一次API。
5. 事件處理透過 `self.ctx.waitUntil(coroutine)` 丟到背景執行，webhook立刻回200（見下方Phase 3備註，這是實作中修正的重要架構決策）。

沒有 active trip 時，非指令訊息完全略過，不寫入任何資料——符合「Bot預設潛水」。1:1私訊或LINE「room」對Bot下指令時，回覆「僅支援群組使用」；一般聊天訊息仍靜默不回應。

---

## 整理邏輯（借鑑 LLM-Wiki 架構）

參考 Karpathy 的 LLM-Wiki 模式（[gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)）：三層架構是 **Raw Sources（不可變原始資料）→ Wiki（LLM持續增量維護的產出）→ Schema（規則定義）**，核心精神是「每次新資料進來，LLM對照現有wiki全貌，就地手術式更新，而不是每次從零重新推導」。用固定欄位的`items`表會強迫LLM把所有整理結果硬塞進category/status/title/description這幾個框框，反而限制了它組織資訊的方式（例如一段來回討論、一個帶但書的決定、日期還沒確定但地點已經定案這種混合狀態，硬塞欄位會失真）。改成：

- **Raw Sources = `messages`**：不變，只增不改。
- **Wiki = `trip_docs.content_md`**：一趟旅程一份markdown文件，LLM完全擁有內容與結構，自己決定要分幾個章節、怎麼排版。
- **Schema = 維護這份文件用的system prompt**：不再定義死欄位，而是定義一個**建議的文件骨架**（讓`/未定事項`、時間軸渲染有基本規律可循）與撰寫規則（每個結論旁要附來源引用、日期未定就寫「日期未定」不要瞎猜）。

### 文件骨架（schema，非強制鎖死，但要求LLM盡量遵守）

```markdown
# <旅程名稱>

## 時間軸
### Day1（YYYY-MM-DD，未定則寫「日期未定」）
- 09:00 xxx
  _(依 王小明 8/20 14:02 提及)_

### Day2
...

## 未定事項
- [ ] 住宿還沒訂，候選：A飯店 / B飯店
  _(討論串：王小明 8/19、陳小華 8/20)_

## 其他資訊
機票、訂房確認信、重要連結、照片等不屬於時間軸的資訊
```

保留 `## 未定事項` 這個固定章節標題是唯一的硬性要求，因為 `/未定事項` 指令要靠它做簡單的文字擷取（抓 `## 未定事項` 到下一個 `## ` 之間的內容），其餘章節、每個條目怎麼寫，交給LLM自由發揮。

### 觸發時機：批次整理，不是每則訊息都打API

不對每則訊息即時呼叫LLM（討論一來一回時可能幾分鐘內十幾則訊息，逐則打API既浪費又容易被上下文不足搞亂）。改成累積未整理訊息，用三種方式觸發批次整理，共用同一段整理邏輯：

1. **手動 `/整理`**：任何人都可以在想要的時間點（例如一輪討論告一段落）主動觸發，最直接、最可控。
2. **排程自動**：用 Cloudflare Cron Trigger，每小時掃一次所有 `status='active'` 的旅程，只要該旅程有 `organized_at IS NULL` 的訊息就跑一次批次整理——等於是「討論一段時間後自動整理」，不用做idle偵測這種複雜狀態機，固定間隔掃描已經夠用。
3. **`/旅程 結束` 時自動跑一次**：確保結束當下不會有漏未整理的訊息留在LIFF看不到。

三種觸發最終都呼叫同一個 `organize_trip(tripId)` 函式，行為完全一致。

### 批次整理流程（整份文件重寫法）

1. 撈出該trip所有 `organized_at IS NULL` 的訊息，依 `sent_at` 排序（這就是「這一段討論」）。若沒有任何待整理訊息，直接結束（`/整理`回覆「沒有新內容」，排程/結束時則靜默跳過）。
2. 讀出該trip目前的 `trip_docs.content_md`（沒有則用上面骨架的空白版本當起點）。
3. 呼叫 Claude **一次**，輸入：目前完整文件 + 這一批待整理訊息全文（依序，含發話人/時間）+ 這批訊息中若有圖片，附上各自備份後的R2 public URL。System prompt要求：
   - 判斷這批訊息裡哪些是旅遊規劃相關的（無關的略過）。
   - **只動受影響的部分**，其餘內容原樣保留，不要整篇重新詮釋。
   - 每個新增/修改的結論旁附上一句來源引用（人名+日期，簡短）。
   - 若某件事從未定變已定（或反過來被推翻），把該條目從「未定事項」章節移到時間軸（或反之），不要兩邊同時留著。
   - 若訊息附了相關照片，用 `![說明](R2圖片URL)` 直接嵌入對應段落。
   - 輸出：**完整的更新後markdown文件全文**。
4. 覆寫 `trip_docs.content_md`、更新 `updated_at`，在 `trip_doc_revisions` 新增一筆（存全文快照 + 這批訊息中最後一則的id），並把這批訊息全部標記 `organized_at = now()`。
5. 這次Claude呼叫的 `usage.input_tokens` / `usage.output_tokens`（API回應本身就有附）連同 `model` 一起寫入 `llm_usage`（purpose='organize'）。

因為每趟旅程只有一份文件，就算整趟討論很熱烈，文件大小通常也就幾KB到十幾KB，全文塞進context完全負擔得起，不需要向量搜尋/RAG。批次處理也讓LLM一次看到「一段完整討論」（而不是被切成單則訊息各自處理），更容易正確判斷最終結論是什麼。

### 這個做法的取捨（老實說清楚，不是只有優點）

- **優點**：schema不再限制LLM怎麼組織內容，混合狀態（日期未定但地點已定）可以自然表達；LIFF渲染直接顯示markdown，不用另外設計「item詳情展開」UI；來源引用內建在文字裡，`/問`回答時天然帶著走。
- **代價**：LLM每次是「整份文件重寫」，比起精準update單一欄位，理論上有更高機率不小心弄丟或改寫到不相關的段落——這是換取自由度必然的風險，用 `trip_doc_revisions` 版本歷史當安全網（真的整壞了可以人工回溯到上一版），但不會自動偵測「這次改壞了」。之後如果實際使用發現LLM常常誤刪內容，可以考慮改成「先outline要動哪個章節，再只重寫那個章節」的兩段式prompt來收斂風險，MVP先用最簡單的整份重寫版本。
- 未來如果想要「日曆匯出」「打勾checklist」這類需要結構化資料的功能，markdown自由文本會比固定欄位難處理（要另外解析），但這不在MVP範圍內，先不預先設計。

### 有意識跳過的部分（未做）

- **Lint/矛盾稽核批次工作**：整理是每則訊息當下即時就地修正，個人規模討論量下已經足夠，不做週期性掃描；若之後發現文件常常前後矛盾沒被蓋掉，再加。
- **併發寫入同一份文件的競態**：hourly cron、`/整理`、`/旅程 結束`的自動整理理論上可能同時對同一趟旅程呼叫`organize_trip`，沒有鎖，真的並發時會有一方的修改被覆蓋。個人規模、朋友揪團的訊息/操作頻率下這個race幾乎不會真的發生，先不處理；如果之後發現文件內容常常莫名其妙少東西，再加鎖（例如`organizing_at`guard欄位）。
- **人工修正機制**：LIFF是唯讀頁面，修正一律靠群組裡繼續講話讓LLM就地更新文件。

---

## Token / 費用追蹤

從第一次有Claude呼叫（Phase 3的批次整理）開始就有，不是後補的功能：

- 共用的 `call_claude(env, purpose, trip_id, model, system, user_content)` helper（`claude_client.py`），所有呼叫Claude的地方（整理、`/問`）都經過它。
- Claude API的回應本身就附 `usage.input_tokens` / `usage.output_tokens`，helper呼叫完直接讀出來，不用自己算token。
- 費用用程式碼裡一個寫死的價目表（`MODEL_PRICES`，每個用到的model對應每百萬input/output token的美金價格，Anthropic官網公告的固定費率）換算，寫進 `llm_usage.estimated_cost_usd`。之後Anthropic調價，改這個常數表即可。
- `/花費` 指令：查這趟旅程累積量，再加一個不篩trip_id的總計，兩個數字一起回覆。

---

## LIFF 頁面

- 單一HTML字串由Worker自己serve（`GET /liff`），不用額外的static assets部署，開啟時讀 URL 上的 `tripId` query param。
- 打 `GET /api/trips/:id` 拿該趟旅程的 `content_md` + 名稱/狀態，前端用`marked`（CDN，鎖定版本）轉成HTML顯示，`DOMPurify`（CDN，鎖定版本）在`innerHTML`賦值前清理，避免使用者訊息內容裡的HTML/script造成XSS。
- LIFF SDK做`liff.init()`（best-effort，失敗也不影響頁面顯示，因為頁面本來就不依賴LINE身分資料）。
- 不做LINE Login身分驗證，trip id用uuid，猜中機率低，先接受這個信任模型。
- R2 bucket 設為可公開讀取，文件裡內嵌的圖片直接用public URL顯示。
- `/懶人包` 指令回覆一個LINE按鈕樣板訊息（而非純文字），按鈕連到LIFF頁面。

---

## 記帳分攤與投票（Phase 7-8，MVP後新增）

跟Epic A-C不同：高頻率、需要挑選特定項目（哪一筆帳目、哪個選項），純聊天指令會洗版。採混合式：開始/結束/新增（一次性、打字當下最快）聊天指令與LIFF並存；編輯、刪除、投票/取消這種需要重複操作的只留LIFF。LIFF頁面因此從唯讀升級成可寫入，用`liff.getProfile()`取得操作者身分。

**記帳**：`expenses`/`expense_splits`兩張表。`/記帳 <金額> <說明> [@人...]`，付款人＝發話者且一定算入分攤（因為LINE不能@自己），@人就精確是那些人，沒@就用「這個LINE群組發言過的所有人」（`default_participants`，group-scoped、跨旅程，不是只看這趟）。結算用標準的貪心「settle up」演算法（債權人/債務人各自排序，最大配最大），`/結算`與LIFF頁面都能查，`/旅程 結束`自動附上。

**投票**：`polls`/`poll_options`/`poll_votes`三張表，一趟旅程同時只能一個進行中投票（DB unique index）。`/投票 開始|結束`只在聊天室（重大狀態變化，值得公告），`/投票 新增`聊天+LIFF並存，投票/取消投票（複選、toggle）只在LIFF。選項編號不存欄位、用`created_at`排序即時算，避免併發新增搶號。

---

## 整理品質改善與查核機制（MVP後新增）

實測發現兩類整理品質問題，加上補上LLM-Wiki原本就有、但這邊一直沒做的查核機制：

**上下文斷線**：`/整理`原本只餵`organized_at IS NULL`的新訊息，(a) 每小時跑一次的話，跨批次的接續句（例如上一批問「訂了嗎」、這一批才回「13號」）會斷線；(b) 就算同一批裡，問句和答句中間隔了無關訊息，LLM也可能因為個別評估每一行、沒有真的把對話串起來看而漏掉。(a)的修法是`organize_trip`額外撈最近`CONTEXT_MESSAGE_COUNT`（15，事後為了縮短prompt從原本的20調降）則已整理過的訊息，包成一段「近期對話紀錄（僅供參考）」夾在文件和新訊息中間送給LLM，並在system prompt明確規定這段不需要重新整理、不要重複引用。(b)純粹是prompt沒講清楚，加了一條規則明確要求LLM先通盤讀過這批訊息再判斷，不要因為答句簡短或跟問句隔了幾則訊息就忽略。

**結構安全網**：`## 未定事項`這個標題會被`extract_undecided_section`原文比對抓取，如果哪次LLM輸出把它拿掉或改名，`/未定事項`會靜默壞掉、不會有任何錯誤。`organize_trip`現在在寫回`trip_docs`前檢查這個標題還在不在，不在就`raise`（不覆寫、不標記這批訊息的`organized_at`，下次整理會重新嘗試），順便沿用了code review那次統一好的log機制。

**Fact-check（對應Karpathy LLM-Wiki的lint/reconciliation機制）**：`fact_check.py`用便宜的haiku模型，拿整理後的新文件+這批原始訊息，檢查有沒有幻覺捏造、人名日期張冠李戴、或跟原始訊息矛盾的內容，結果存進新表`doc_fact_check_flags`，`/檢查`指令查詢最近10筆。這個vault是單一文件（不是Karpathy原本的多頁wiki），所以只搬了「lint的fact-check部分」，像orphaned pages、broken cross-reference、stale embeddings這些多頁概念的檢查不適用。**刻意只掛在`scheduled()`（cron，15分鐘執行時間額度）不掛在`/整理`**：實測單次organize呼叫就要15秒左右，`/整理`的`waitUntil`只有30秒共用額度，同一個指令內再加一次LLM呼叫做fact-check風險太高，直接重演item 11那個「retry疊加撞上30秒上限」的教訓。

**踩到的坑**：`fact_check.py`第一版system prompt明講「不要用程式碼區塊包起來」，但haiku還是把JSON回應包成` ```json ... ``` `，導致`json.loads`丟`ValueError`、被原本設計成「格式錯誤就靜默跳過」的`except`吃掉，整個fact-check形同沒作用（用一個明顯捏造的假claim實測才抓到——沒有任何錯誤訊息，只是`doc_fact_check_flags`一直是空的）。修法是不跟模型的格式習慣打架，`json.loads`前先剝掉頭尾的` ``` `／` ```json `圍籬。這是「LLM沒照指令輸出格式」這類問題的通用教訓：與其加強措辭要求模型絕對遵守，不如在解析端做寬容處理。

---

## 可調整模型設定（MVP後新增，Phase A）

原本`ORGANIZE_MODEL`/`ANSWER_MODEL`是寫死在`claude_client.py`的常數，改動需要動程式碼＋重新deploy。新增一張`settings` key-value表（`key`/`value`/`updated_at`），`get_model(env, purpose)`／`set_model(env, purpose, model)`改成查資料庫、沒有對應設定才退回常數當預設值——改動立即生效，不用deploy。新增指令`/模型`：不帶參數查看目前整理／問答／查核三個階段各用哪個模型；帶參數（`/模型 整理 sonnet`）修改設定。

**權限設計**：查看任何人都能看，但**修改僅限admin**（`is_admin`，不含旅程擁有者）——這點刻意跟`/旅程 結束`的「擁有者或admin」不同，因為模型設定是影響全部旅程的系統層級設定，不是特定某趟旅程專屬的東西，不適合讓「剛好開了某趟旅程的人」也能動。

**別名限制**：`/模型`只接受`MODEL_ALIASES`裡列出的別名（目前只有`sonnet`／`haiku`），刻意不開放任意輸入模型ID字串——避免選到`MODEL_PRICES`沒有報價的模型，導致花費追蹤悄悄變成算出$0而不自知。

這個機制也順便把item 15那個「organize暫時改用haiku-4-5」的暫時措施正式化：`DEFAULT_ORGANIZE_MODEL`改回原本設計的`claude-sonnet-5`，然後手動在`settings`表寫入`organize_model=claude-haiku-4-5`這筆覆寫，讓目前的實際運作行為不變，但變成一個明確、可查詢、將來sonnet-5連線問題確認解決後可以直接下`/模型 整理 sonnet`切回去的設定，不用再改程式碼。

OpenAI/ChatGPT支援列為Phase B，暫緩：現有的`call_claude`/連線層完全綁死Anthropic SDK跟這次踩到的Cloudflare Emscripten專屬問題（`httpx2_jsfetch`），真的要支援OpenAI等於要重新做一層provider抽象，也可能要重新走一次「在這個特殊執行環境下踩地雷」的過程，工作量遠大於Phase A，另外規劃。

---

## 開發過程中的重要發現與修正

這些是規劃階段沒預料到、實作/測試時才發現的問題，記錄下來避免以後忘記：

1. **LINE webhook需要2秒內回2xx**：`organize_trip`（呼叫Claude+多次D1寫入）常常要3-8秒，超過LINE的容忍時間會被判定逾時、使用者感覺「指令沒反應」。解法是`self.ctx.waitUntil(coroutine)`——webhook收到事件立刻回200，實際處理丟到背景執行。**已知限制**：本機`wrangler dev`/`pywrangler dev`不會正確執行`waitUntil`丟出去的背景工作，只能在正式環境（`wrangler deploy`後）驗證，本機測試涉及背景處理的指令要記得改用真實部署測試。
2. **`getGroupMemberProfile`（group-scoped顯示名稱API）不穩定**：實測會404，就算對方已加Bot好友。改用一般的`GET /v2/bot/profile/{userId}`（1:1 profile API）就正常，前提是該成員要加過Bot好友——沒加好友的人訊息還是會存，只是`user_display_name`會退回顯示原始user_id。
3. **LINE聊天室不渲染markdown**：`/未定事項`、`/問`的回覆如果直接echo原始markdown（checkbox `- [ ]`、`_斜體引用_`、`**粗體**`），在LINE裡會變成字面符號雜訊。加了`organize.to_line_plaintext()`轉換函式套用在這些回覆上；`trip_docs`本身存的markdown不受影響（LIFF頁面渲染markdown還是需要原始語法）。
4. **XSS風險**：`content_md`來自使用者原始訊息，`marked.parse`預設不清理HTML，直接`innerHTML`有被注入攻擊的風險。加了`DOMPurify`清理，並把CDN依賴都鎖版本（原本用「latest」，未來套件更新可能默默改變行為/路徑，也確實因此踩到`marked@18`把瀏覽器檔案搬到`/lib/marked.umd.min.js`的路徑變更）。
5. **LIFF app不能掛在Messaging API channel下**：LINE平台政策變更，改成另外建一個LINE Login channel來放LIFF app，對外的`liff.line.me/<liffId>`連結行為不變。
6. **LINE Login channel預設「Developing」狀態**：只有channel的開發者/測試者能通過LIFF的LINE Login驗證流程，其他群組成員打開連結會收到400錯誤。到該channel頁面點「Developing」→「Publish」即可讓所有人使用，不影響任何金鑰安全性。
7. **LINE不能@提及自己**：這是LINE客戶端本身的限制，不是我們能改的。原本設計「有@人才算分攤，沒@自己就不算自己一份」在這個限制下行不通——所以改成payer一律自動算進分攤名單，@人只用來指定「除了自己以外還有誰」。
8. **「發言過的人」預設分攤名單，範圍要夠大**：一開始把`default_participants`限定在「這趟旅程」，實測發現如果某人是在很久以前的舊旅程（或完全在Bot記錄範圍外的時間點）講過話，這趟新旅程就選不到他，因為Bot只有旅程進行中才會被動記錄訊息，之前講的話完全沒被存下來、無法回溯。改成群組範圍（跨旅程），並且在LIFF記帳表單加一個「手動新增分攤對象」輸入框（用名字當識別key，同名字歸戶成同一人）當最後的escape hatch。
9. **顯示名稱會「凍結」在第一次查詢失敗的結果**：`get_display_name`第一次查詢時如果對方還沒加Bot好友，會退回顯示原始LINE user id，這個結果一旦存進資料庫（`messages`/`expenses`/`expense_splits`），就算對方之後加了好友也不會自動更新。修法是加一個self-healing機制：讀取資料時如果發現存的名稱其實等於原始id（代表當初沒查到），就重新查一次LINE API，查到就順便把所有相關資料列一起更新，之後就不用再修第二次。
10. **LIFF寫入端點要記得補齊跟聊天指令一樣的防護**：這個系列做了幾輪code review，重複出現的模式是「聊天指令那條路徑有檢查（例如投票要active才能新增/投票、trip擁有權檢查），但LIFF那條新開的API路徑忘記加同樣的檢查」。之後每加一個LIFF可寫入的新功能，要記得比照聊天指令那邊已有的防護（狀態檢查、歸屬關係檢查）一併補上，不能只查trip_id/poll_id對不對，也要查狀態合不合法。
11. **`/整理`間歇性回覆「處理指令時發生錯誤」**：`except Exception:`當初沒有印任何log，`wrangler tail`看不到真正的錯誤，第一步是補上`print(f"...{type(e).__name__}: {e}")`才看到`anthropic.APITimeoutError`。追下去發現是Cloudflare Python Workers的HTTP transport（`httpx2_jsfetch`，Emscripten上用`fetch()`+`AbortController`模擬httpx）預設**連線（connect）逾時只有5秒**，Worker到Anthropic API的TLS握手偶爾就是會超過5秒，跟訊息內容大小完全無關（測試時只有13-19則短訊息也會炸）。過程中兩個直覺修法都踩雷、記錄下來避免重踩：(a) 在`call_claude`裡包一層手動retry——結果讓單一指令的處理總時間超過Cloudflare `ctx.waitUntil()`對HTTP-triggered Worker的**硬性上限（回應送出後最多再跑30秒，且是整個invocation所有waitUntil工作共用同一個30秒額度，不是每次呼叫各自算）**，反而讓整個背景工作被平台直接砍掉、使用者連錯誤訊息都收不到（比原本的行為更差）；(b) 改用`client.messages.stream()`——這個Emscripten transport的SSE行讀取邏輯本身有bug（對`memoryview`呼叫`.splitlines()`直接crash），streaming在這個runtime上根本不能用。真正的修法是把`AsyncAnthropic`的`timeout`參數明確設成`httpx2.Timeout(600.0, connect=30.0)`，只放寬connect那段，不用retry也不用streaming。旅程層級的定時整理（`scheduled()`, Cron Trigger）沒有這個30秒限制（Cloudflare文件：cron-triggered Worker有15分鐘總執行時間），本來就是`/整理`萬一失敗的安全網。**事後code review補了一刀**：只放寬`timeout`還不夠——`anthropic` SDK預設`max_retries=2`，且`APITimeoutError`是`APIConnectionError`的子類別，SDK的`_should_retry_exception`一律會重試，等於connect逾時可能重試到3次、每次最多30秒，最壞情境接近90秒，一樣會撞上`waitUntil()`的30秒上限，重演(a)的悲劇。修法是同時加上`max_retries=0`，只留一次乾淨的嘗試、一次乾淨的失敗。另外`line_client.py`裡`reply_messages`/`get_display_name`/`get_message_content`用的是另一個套件`httpx`（不是`httpx2`），預設`Timeout(5.0)`（連connect都只有5秒，比anthropic預設更緊），同一個Cloudflare TLS握手延遲問題理論上一樣會發生在LINE API呼叫上，一併加了`timeout=httpx.Timeout(30.0, connect=30.0)`（`httpx`本身預設不重試，不用擔心retry疊加）。

12. **`/整理`積壓116則訊息、`/整理`連續多次卡住無回應**：連續錯過好幾次整點cron後，`/整理`開始間歇性完全沒回應（不是錯誤訊息，是完全靜默）。追查發現三個各自獨立、疊加起來才炸開的問題：(a) **Cloudflare的`fetch()` handler（不管有沒有用`waitUntil`）本身就有平台層級、約30秒的invocation總時長上限，且被砍掉時不會拋出Python看得到的例外**——這代表`try/except`跟任何log機制都補不到，之前以為只有`waitUntil`額度的理解不完整；(b) `claude-sonnet-5`在完全沒有要求`thinking`參數的情況下，偶爾會自己生成一個`thinking`內容區塊，吃掉大部分的`max_tokens`預算——第一版直覺修法是調低`max_tokens`（8000→2000）想「餓死」thinking，結果只是把輸出砍在thinking中途，導致文件被截斷到一半（`stop_reason`卡在`max_tokens`，內容結尾不完整，且既有的結構安全網只檢查標題還在不在，不會檢查內容有沒有被腰斬）；(c) 訊息則數不是好的prompt大小代理指標，同樣30則訊息，今天下午的真實對話（完整句子）比先前測試訊息（單字回覆）大上好幾倍。真正的修法：明確傳入`thinking={"type": "disabled"}`關掉這個非預期行為（而不是用`max_tokens`去卡預算），`max_tokens`可以放心恢復到8000（反正不會被thinking吃掉）；`organize_trip`改成用**累積字數**（`MAX_BATCH_CHARS`，非則數）動態決定這次要處理多少則待整理訊息，一次處理不完就留到下次`/整理`或cron繼續處理，不會卡死；近期對話上下文則數也一併調降。即使做完這些修正，webhook路徑（`/整理`）在測試當下仍然間歇性卡在30秒門檻附近失敗（不同batch大小都各自失敗過幾次），判斷是Anthropic API當下真實的回應時間變異，不是可以單靠調參數消除的確定性問題——`/整理`永遠會暴露在這種平台30秒上限的風險下，真正可靠的路徑是有獨立15分鐘執行時間、不受這個限制的cron排程（`scheduled()`），大量積壓訊息最終應該靠它慢慢清完，而不是無止盡地把`/整理`的batch size越調越小去賭運氣。

13. **`MAX_BATCH_CHARS`跟`read_timeout`一樣需要依呼叫者調整**：item 12修好後cron實測跑一次只要幾秒鐘就處理完800字的batch，遠低於cron實際擁有的15分鐘（`scheduled()`傳的`read_timeout=180.0`）——代表`/整理`（webhook）安全的800字上限，套用在cron身上反而是在浪費cron本來就有的餘裕，讓大量積壓訊息要多繞好幾個整點才清得完。改成`organize_trip`新增`max_batch_chars`參數（預設值即`/整理`用的保守值800），`scheduled()`呼叫時額外傳入更大的`CRON_MAX_BATCH_CHARS`（8000）。同時把`MAX_FETCH_ROWS`從300提高到1000，避免SQL撈取本身在字數上限生效前就先把候選訊息池切太小（很多真實訊息都是一兩個字的短句，8000字預算可能要撈超過300則才夠）。
14. **收回訊息（LINE unsend事件）原本完全沒有處理**：LINE針對「收回」會送出獨立的`"type": "unsend"`事件（不是`"type": "message"`），但`_handle_event`一開始就只認`"message"`型別，unsend事件被直接忽略——導致使用者收回訊息（例如重複貼的照片）後，資料庫完全不知道，內容還是照樣被整理進文件。修法：新增`_handle_unsend`，收到unsend時（a）如果是圖片，刪除R2上的實體檔案跟`attachments`資料列；（b）不論這則訊息先前有沒有被整理過，一律`UPDATE messages SET unsent_at = now(), organized_at = NULL`——重設`organized_at`讓它重新進入下一批待整理佇列，用同一條路徑處理「還沒整理過」（LLM看到「已收回」直接忽略，不新增內容）跟「已經整理過」（LLM看到後把文件裡對應的內容移除或修正）兩種情況，不用另外寫分支判斷。`_format_line`遇到`unsent_at`有值的訊息會改用「（已收回訊息，原內容：...）」格式呈現，system prompt也加了對應規則（rule 10）教LLM怎麼處理。
15. **`claude-sonnet-5`連續一週在連線階段（connect）就卡住，Anthropic帳單卻持續有費用**：使用者從Anthropic Console的費用頁面發現每天穩定被扣約$0.92，但`llm_usage`表從8/30之後完全沒有紀錄——兩邊對不起來。追查發現：(a) `scheduled()`cron確實每小時準時觸發（不是cron沒跑），但`organize_trip`的Claude呼叫每次都精準卡在connect逾時的秒數上失敗（先是30秒，後來連拉到180秒、600秒的`read_timeout`都沒用，因為卡住的根本不是「等回應」的read階段，而是更早的connect階段——這是先前調`read_timeout`時完全沒注意到的盲點，`connect=30.0`從item 11修好後就一直寫死沒再檢視過）；(b) 加了暫時的診斷log（量測`elapsed`時間、印出`e.__cause__`鏈）才第一次直接看到`ConnectTimeout`，且連續6次整點失敗的`elapsed`都是一模一樣的30.4秒，不像隨機網路延遲，比較像穩定被擋住；(c) 測試發現`/問`（用`claude-haiku-4-5`）在同一時段完全正常，換成haiku跑`organize`也馬上成功，把積壓一週的59則訊息一次清完。**目前尚未解開的謎**：Anthropic status page完全沒有對應到「持續一週」規模的sonnet-5事件記錄（只有零星幾次幾小時內就resolved的事件），如果connect真的每次都失敗、理論上根本打不到Anthropic的伺服器，那之前穩定產生的費用從何而來也還沒有答案。暫時解法：`ORGANIZE_MODEL`先改成`claude-haiku-4-5`（品質略遜於sonnet-5，但至少能動），`call_claude`/`organize_trip`新增`connect_timeout`參數（跟`read_timeout`一樣依呼叫者調整，cron用90秒），並在`scheduled()`留了診斷log方便之後追蹤。都標記了TEMPORARY，之後要視sonnet-5連線問題是否恢復，決定要不要切回去。

---

## 驗證方式

- 每個Phase都在一個真實LINE測試群組手動操作驗證（下指令、傳訊息、傳圖片），沒有自動化整合測試環境可用LINE的真實webhook。
- 資料寫入類：直接用 `wrangler d1 execute` 查資料庫內容，確認訊息/照片有正確寫入沒有重複、`organized_at`正確標記、`llm_usage`每次呼叫都有記錄、人工檢查`trip_docs`更新後內容是否合理（必要時對照`trip_doc_revisions`上一版）。
- 問答類：手動問幾個涵蓋「有答案」「查無資料/未定案」的問題，確認Bot不會瞎猜。
- LIFF頁面：手機LINE實際點開連結，確認時間軸、未定清單、來源引用顯示正確。
