# 旅行規劃管理助手 — LINE旅行規劃Bot

[English](./README.md)

一個LINE Bot，會在旅程進行中被動讀取群組對話，用Claude維護一份即時更新的markdown文件，記錄已經決定跟還沒決定的事情，並透過LIFF網頁呈現，內建記帳分攤跟投票功能。

設計對象是2-10人的朋友小團體一起規劃旅行——不是多租戶SaaS產品。完整的設計脈絡、參考的「LLM-Wiki」架構、開發過程中踩過的坑跟修法，可以參考[`docs/user_stories.md`](docs/user_stories.md)跟[`docs/plan.md`](docs/plan.md)。

**這個repo裡有兩套部署**：[`cloudrun/`](cloudrun/)是目前正式在跑的版本，架在Google Cloud Run上——真實使用者用的就是這個。[`worker/`](worker/)是最早的Cloudflare Python Workers版本，留在repo裡當參考跟備用回退路徑，但已經不是正式服務的那一個。想知道為什麼會有兩套，看下面的「為什麼有兩套部署」；照自己想跑的那套對照對應的建置步驟章節即可。

## 功能

- **被動記錄**：旅程進行中，群組裡的每則訊息（含照片）都會被記錄下來。其他時間Bot完全不插話，也不會主動說話，除非有指令觸發。
- **LLM整理文件**：定時（也可以用`/整理`手動觸發）讓Claude把新訊息整理進一份markdown文件——時間軸、未定事項、其他資訊。每個結論旁邊都附來源引用（誰、什麼時候說的），聊天裡提到的連結（訂房確認信、地圖等）會原文保留，不會被摘要掉。
- **有根據的問答**：`/問 <問題>`只根據文件內容回答，不會用模型自己的知識瞎猜，文件裡沒有答案就明講「還沒決定」。
- **LIFF頁面**：`/懶人包`取得一個可分享的網頁連結，顯示目前文件、記帳、進行中的投票——`/旅程 開始`第一次成功時也會順便提醒大家加Bot好友（原因見下方「部署前該知道的設計決策」）。
- **每趟旅程各自選模型**：LIFF頁面可以針對這趟旅程單獨設定整理／問答／查核各自要用哪個模型（Claude Opus/Sonnet/Haiku，或設定好的話也可以用OpenAI模型）——留著「使用預設」就會跟著`llm_client.py`目前設定的常數走。
- **顯示名稱自動修正**：還沒加Bot好友的人一開始會顯示成一串LINE user ID；加了好友之後，下一次`/整理`跟LIFF頁面上的「🔄 修正名稱顯示」按鈕都能把已經寫進文件裡的舊引用修正過來。
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
| `/旅程 開始 <名稱>` | 開始一趟旅程，從此刻起群組裡的每則訊息都會被記錄，Bot也會發一則提醒大家加好友的訊息＋連結。 | `/旅程 開始 東京五日遊` → `🧳 旅程「東京五日遊」開始了！我會開始記錄接下來的討論。`＋加好友提醒 |
| `/旅程 結束` | 結束目前旅程：先把還沒整理的訊息整理進文件，再附上最終記帳結算。只有開始的人能結束（或用`ADMIN_USER_ID`覆寫）。 | `/旅程 結束` → `🏁 旅程「東京五日遊」結束了！` ＋一則結算訊息 |

**🔍 查詢（唯讀，隨時可用）**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/問 <問題>` | 只根據旅程文件回答，不會瞎猜，文件沒提到就說「還沒決定」。 | `/問 我們住哪間飯店` → 根據實際討論內容回答，或`⚠️ 還沒決定` |
| `/未定事項` | 列出目前所有還沒決定的事項。 | `/未定事項` → 目前「未定事項」章節的純文字版本 |
| `/懶人包` | 傳送LIFF頁面連結（時間軸、記帳、投票、每趟旅程的模型設定都在同一頁）。 | `/懶人包` → 一則連到LIFF頁面的LINE按鈕訊息 |
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
| `/說明` | 顯示完整指令列表。 | — |

模型設定原本是`/模型`聊天指令（僅限管理員），現在改成LIFF頁面上每趟旅程各自的選單——見上方「功能」。

## 部署前該知道的設計決策

- **所有回覆都靠指令觸發。** Bot唯一會主動做的事只有旅程進行中被動記錄訊息，不會有任何未經要求的插話。
- **LIFF頁面沒有存取控制。** 有連結的人都能看、都能編輯（記帳、投票、模型設定等）。這符合小型朋友團體的信任模型，不適合有不受信任成員的場合。
- **同一個群組同時只能有一趟進行中的旅程。** 用`/旅程 開始`開始新的一趟，用`/旅程 結束`結束目前這趟（只有開始的人能結束，除非設定了`ADMIN_USER_ID`覆寫權限，見下方）。
- **目前只有繁體中文介面**，所有指令、回覆、LIFF介面文字都是寫死的繁體中文。
- **要加好友，名字才會正常顯示。** LINE的profile API只有對已經加Bot好友的人才能查到真實顯示名稱（`/旅程 開始`時的提醒就是為了這件事）——還沒加的人會顯示成一串LINE user ID。這件事在對方加好友*之後*整理進文件的內容會自動修正；已經寫進文件的舊內容不會自動變，要嘛靠LIFF頁面的🔄修正名稱顯示按鈕，要嘛等還沒整理過的訊息跑下一次`/整理`。

## 為什麼有兩套部署

最早的實作（`worker/`）跑在Cloudflare Python Workers上。功能沒問題，但有一個真實、很尖銳的平台限制：Worker的`fetch()` handler有大約30秒的硬性執行上限，而且被平台強制中斷時**不會產生Python看得到的例外**，所以webhook路徑上一個比較慢的LLM呼叫，可能整個請求就這樣悄悄消失。每小時的cron排程（`scheduled()`）不受這個限制（有自己獨立大約15分鐘的額度）——這也是為什麼跟時間敏感的邏輯得依照觸發路徑分成兩套不同的timeout預算。完整的來龍去脈記錄在`docs/plan.md`的發現清單裡。

`cloudrun/`把同一個產品搬到Google Cloud Run上——一個真正長駐的container process，不是有短暫硬性timeout的FaaS沙箱——同時維持**同一個**D1資料庫跟R2 bucket（兩套部署之間沒有資料搬移，只有存取方式從Workers binding改成D1的REST API跟R2的S3相容API）。現在每個webhook事件都會完整同步處理完才回應LINE，比Cloudflare `ctx.waitUntil()`要求的「先回應、背景處理」模式更簡單也更可靠。`worker/`刻意保持部署、完全不動，就是為了讓「把webhook/LIFF網址切回去」隨時是一個快速、乾淨的回退方案。

## 架構

**目前版本（`cloudrun/`）：**

- **Google Cloud Run**（Python/FastAPI）撐起整個後端——一個`/webhook`路由，一個`/liff`＋LIFF用的REST API，一個`/internal/scheduled-organize`路由，由**Cloud Scheduler**取代Cloudflare的Cron Trigger來觸發。
- **Cloudflare D1**（SQLite）存所有關聯式資料，透過它的[REST API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)存取，不是Workers binding，因為Cloud Run不是Workers執行環境。
- **Cloudflare R2**存照片備份，透過它的S3相容API、用`boto3`存取（`cloudrun/src/r2.py`），理由跟D1一樣。
- **Anthropic或OpenAI API**透過官方Python SDK呼叫（Cloud Run上不需要`worker/`那種Pyodide專屬的傳輸層）。每個階段實際用哪個模型是每趟旅程在LIFF頁面上各自的設定（NULL＝跟著`llm_client.py`的常數走），不是全域、需要管理員權限的聊天指令。
- **LINE Messaging API**負責Bot本體，另外需要一個獨立的**LINE Login** channel給LIFF app用。

**舊版（`worker/`）：**

- **Cloudflare Python Workers**（Pyodide執行環境）——一個`fetch()` handler處理LINE webhook跟LIFF的API，一個`scheduled()` handler每小時掃描並整理/查核。
- 一樣用D1／R2，但是透過原生Workers binding，不是REST／S3 API。
- Anthropic API透過Pyodide專屬的`httpx2`傳輸層fork呼叫（同步client跟一般`httpx`在Workers的Emscripten執行環境下都用不了）。

## 費用

以朋友團體的用量來說，兩套部署現實上都**基本上是0元／月**：

- **Cloud Run**（如果跑`cloudrun/`）：免費額度涵蓋每月200萬次請求、18萬vCPU-秒——一趟旅程的webhook＋每小時cron流量遠遠碰不到。
- **Cloudflare Workers**（如果跑`worker/`）：免費額度（每天10萬次請求）就夠用。不管哪一套，**D1**（5GB儲存空間、每天500萬次讀取＋10萬次寫入）跟**R2**（10GB儲存空間、沒有流量費）的免費額度都綽綽有餘。
- **LINE Messaging API**：0元。Bot所有的回覆都是「reply訊息」（由指令觸發、用事件自帶的reply token），LINE不會針對這種訊息收費，也不計入任何額度。Bot完全不會主動發送push訊息。
- **LLM API呼叫**：唯一真正會花錢的地方，按token計費，實際費率看每趟旅程設成哪個模型（LIFF選單，或沒改過就是`llm_client.py`的預設）。整理是批次處理、有上限，不是每則訊息都打一次API，而且只有真的有新內容待整理時才會呼叫——實測一趟正常聊天量的旅程，一個月的花費遠低於1美金。用`/花費`隨時可以查到你這趟旅程實際累積的美金花費。

有一個值得知道的情境：如果整理排程因為某些原因連續好幾次沒有成功清完積壓訊息（開發過程中真的發生過一次，細節記在`docs/plan.md`的發現清單裡），積壓的內容只是需要多跑幾個批次才能清完，**不會**變成無限重試迴圈、也不會因此讓花費暴增——`max_retries=0`就是特意為了避免這種情況設的。

## 建置步驟：Cloud Run（`cloudrun/`，目前版本）

需要準備：已啟用計費的GCP帳號、Cloudflare帳號、LINE Developers帳號、`gcloud`、Anthropic或OpenAI其中一個的API key。

### 1. LINE Messaging API channel（Bot本體）

1. 在[LINE Developers Console](https://developers.line.biz/)建立一個Messaging API channel。
2. 記下**Channel secret**，並發一個長期有效的**Channel access token**。
3. 到[manager.line.biz](https://manager.line.biz/)（不是開發者後台）找到這個官方帳號，開啟**「允許加入群組聊天」**。這個設定很容易漏掉，沒開的話Bot完全收不到群組訊息。
4. webhook網址先留白，等deploy完再回來設定。

### 2. LINE Login channel（給LIFF app用）

1. 建立一個**LINE Login** channel（跟上面的Messaging API channel是分開的）。
2. 底下加一個LIFF app，endpoint網址設成`https://<你的Cloud Run網址>/liff`，scope選`profile`。
3. 記下LIFF ID。
4. 把這個channel發布（預設是「Developing」狀態，只有你自己能用LIFF頁面，其他人會被擋）。

### 3. Cloudflare D1跟R2

```sh
npx wrangler login   # 如果還沒登入過
npx wrangler d1 create itinerary-manager-db   # 記下印出來的database_id
npx wrangler d1 execute itinerary-manager-db --remote --file cloudrun/schema.sql
npx wrangler r2 bucket create <一個全域唯一的bucket名稱>
```

到Cloudflare dashboard把R2 bucket的公開存取打開（進bucket → Settings），記下它給的`pub-*.r2.dev`網址。

建立一個Cloudflare API token（dashboard → My Profile → API Tokens →「Create Token」，權限給`D1:Edit`、範圍限定在你的資料庫）——因為Cloud Run不是Workers執行環境，改用REST API存取D1，需要這組獨立的token。

另外再建立一個R2的API token（dashboard → R2 → Manage R2 API Tokens →「Create API token」，權限選「Object Read & Write」，範圍限定在你的bucket）給`R2_ACCESS_KEY_ID`／`R2_SECRET_ACCESS_KEY`用——這跟上面的D1 token是不同種類的token。

### 4. GCP專案與Cloud Run

```sh
gcloud projects create <你的專案ID>
gcloud billing projects link <你的專案ID> --billing-account=<你的billing帳號ID>
gcloud config set project <你的專案ID>
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com
```

把`cloudrun/.env.example`複製成`cloudrun/.env`，填入所有值（LINE channel secret/token、LLM API key、Cloudflare帳號ID／database ID／API token、R2金鑰跟bucket資訊、步驟2的LIFF ID，以及一個隨機產生的`SCHEDULER_SECRET`，例如`openssl rand -hex 32`）。

Deploy，把`.env`的內容轉成環境變數傳進去（不要把包含密鑰的`--set-env-vars`清單以明文留在shell history裡——直接從檔案pipe進去）：

```sh
cd cloudrun
gcloud run deploy itinerary-manager \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --env-vars-file <(awk -F= '!/^#/ && NF {print $1": \""$2"\""}' .env)
```

記下印出來的service網址。把Messaging API channel的webhook網址設成`https://<那個網址>/webhook`，並在LINE Developers Console驗證。

### 5. Cloud Scheduler（每小時整理）

```sh
gcloud scheduler jobs create http itinerary-manager-organize \
  --location asia-east1 \
  --schedule "0 * * * *" \
  --uri "https://<你的Cloud Run網址>/internal/scheduled-organize" \
  --http-method POST \
  --headers "x-scheduler-secret=$(grep ^SCHEDULER_SECRET= cloudrun/.env | cut -d= -f2-)"
```

把Bot加進LINE群組，下`/旅程 開始 <名稱>`，應該就會開始記錄了。

> **建議群組裡每個人都額外加這個Bot為一對一好友**（Bot自己在`/旅程 開始`時也會提醒）。顯示名稱是透過LINE的1:1 profile API查詢的，只有加過Bot好友的人才查得到名字，沒加好友的人在旅程文件跟回覆裡會顯示成他原始的LINE user ID，不會顯示名字。如果之後才補加好友，下次查詢到名字時會自動修正，或用LIFF頁面的🔄修正名稱顯示按鈕處理已經寫進文件的舊內容。

### 本機開發

```sh
cd cloudrun
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd src && uvicorn main:app --reload --port 8080
```

本機開發連的是跟正式環境同一個遠端D1（透過REST API）跟同一個R2/LLM provider，不需要另外架本機資料庫。

## 建置步驟：Cloudflare Workers（`worker/`，舊版）

留著當參考跟回退方案。需要準備：Cloudflare帳號、LINE Developers帳號、`uv`、Anthropic API key。

步驟1-2（LINE Messaging API channel、LINE Login channel）跟上面Cloud Run的做法一樣，差別只在LIFF endpoint網址是`https://<你的worker>.workers.dev/liff`。

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

`OPENAI_API_KEY`是選填的：只有想讓某個階段選用OpenAI模型才需要設定。不設定的話就只能用Claude模型。

### 5. Deploy並接上webhook

```sh
npm run deploy   # uv run pywrangler deploy
```

把Messaging API channel的webhook網址設成`https://<你的worker>.workers.dev/webhook`並在LINE Developers Console驗證。把Bot加進群組，下`/旅程 開始 <名稱>`，應該就會開始記錄了。

### 本機開發

```sh
uv venv && uv sync   # 讓編輯器有autocomplete跟型別提示
npm run dev           # uv run pywrangler dev，本機開發伺服器
```

注意：`ctx.waitUntil()`（所有webhook背景處理都靠它）在本機開發環境下不會正確執行——任何HTTP即時回應之後的邏輯，都要實際deploy才能測試。

## License

[MIT](./LICENSE)
