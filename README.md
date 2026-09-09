# Itinerary Manager — a LINE trip-planning bot

[繁體中文](./README.zh-TW.md)

A LINE bot that passively reads a group chat during an active "trip," uses Claude to
maintain a living markdown summary of what's decided and what's still open, and exposes it
through a LIFF web page with expense-splitting and voting built in.

Built for a small group of friends planning a trip together (2–10 people) — not a
multi-tenant SaaS product. See [`docs/user_stories.md`](docs/user_stories.md) and
[`docs/plan.md`](docs/plan.md) for the full design rationale, the "LLM-Wiki" architecture
this is based on, and a running log of real bugs found and fixed along the way.

**Two deployments live in this repo**: [`cloudrun/`](cloudrun/) is the current, live
implementation on Google Cloud Run — this is what real users are on. [`worker/`](worker/) is
the original Cloudflare Python Workers implementation; it's kept in the repo as a reference
and an instant rollback path, but it's no longer the live deployment. See "Why two
deployments?" below for what prompted the move, and pick whichever setup section matches the
one you actually want to run.

## What it does

- **Passive capture**: while a trip is active, every message (and photo) sent in the LINE
  group is recorded. The bot stays silent otherwise — it never speaks unless a command asks
  it to.
- **LLM-organized doc**: on a schedule (and on demand via `/整理`), Claude folds newly
  captured messages into a single markdown document per trip — a timeline of decided plans,
  a list of open questions, and everything else (tickets, links, confirmations). Every
  conclusion carries a source citation (who said it, when), and a source link mentioned in
  chat (a booking confirmation, a map) is kept verbatim rather than summarized away.
- **Q&A with grounding**: `/問 <question>` answers from the trip document only, never from
  the model's own knowledge, and says "not decided yet" when the doc doesn't have an answer.
- **LIFF page**: a shareable web page rendering the current doc, expenses, and any active
  poll, reachable via `/懶人包` — which also nudges the group to add the bot as a 1:1 friend
  the first time a trip starts (see "Design choices" below for why that matters).
- **Per-trip model picker**: the LIFF page lets you choose which model (Claude Opus/Sonnet/
  Haiku, or an OpenAI model if configured) handles organizing, Q&A, and fact-checking for
  that specific trip — leave it on "use default" and it follows whatever `llm_client.py`'s
  constants are set to.
- **Display-name self-heal**: anyone who spoke before friending the bot shows up as a raw
  LINE user ID at first; once they friend it, both the next `/整理` pass and a manual
  "🔄 修正名稱顯示" button on the LIFF page fix already-written citations.
- **Expense splitting**: `/記帳` records who paid, computes a settlement with a greedy
  "settle up" algorithm, editing happens on the LIFF page.
- **Voting**: `/投票` runs a multi-select poll; voting itself happens on the LIFF page to
  avoid flooding the chat.
- **Fact-check pass**: a cheap second LLM pass (cron only) flags anything the organize step
  added that doesn't actually trace back to a real message, queryable via `/檢查`.
- **Message recall handling**: if someone unsends a message, the bot retracts whatever
  content it contributed to the doc (and deletes the photo backup, if any) on the next pass.

Run `/說明` in the group chat for the full command list.

## Usage

All commands are Traditional Chinese slash-commands, typed directly in the LINE group. `/說明`
always shows the live, in-app version of this list.

**🧳 Trip lifecycle**

| Command | What it does | Example |
| --- | --- | --- |
| `/旅程 開始 <name>` | Starts a trip. From here on, every message in the group is recorded, and the bot posts a reminder + link nudging everyone to add it as a 1:1 friend. | `/旅程 開始 東京五日遊` → `🧳 旅程「東京五日遊」開始了！我會開始記錄接下來的討論。` + an add-friend prompt |
| `/旅程 結束` | Ends the current trip: folds in anything not yet organized, then appends the final expense settlement. Only the person who started it can end it (or the `ADMIN_USER_ID` override). | `/旅程 結束` → `🏁 旅程「東京五日遊」結束了！` + a settlement message |

**🔍 Query (read-only, work anytime)**

| Command | What it does | Example |
| --- | --- | --- |
| `/問 <question>` | Answers strictly from the trip document — never guesses, says "not decided yet" if the doc doesn't cover it. | `/問 我們住哪間飯店` → an answer grounded in what was actually discussed, or `⚠️ 還沒決定` |
| `/未定事項` | Lists everything still open/undecided. | `/未定事項` → the current "未定事項" section as plain text |
| `/懶人包` | Sends a LIFF page link (timeline, expenses, poll, and the per-trip model picker all in one page). | `/懶人包` → a LINE button template linking to the LIFF page |
| `/花費` | Shows accumulated LLM API cost for this trip and overall. | `/花費` → `🤖 這趟旅程：12次AI呼叫，約 US$0.0187（輸入9,204／輸出2,150 tokens）` |
| `/結算` | Shows the current expense settlement without ending the trip. | `/結算` → who owes whom, computed with a greedy settle-up |

**💰 Expenses**

| Command | What it does | Example |
| --- | --- | --- |
| `/記帳 <amount> <description> [@person...]` | Records who paid. The payer is always included in the split (LINE doesn't let you @-mention yourself). No @-mentions → splits across everyone who has spoken during the trip. | `/記帳 1500 晚餐燒肉 @小華 @小明` → `💰 已記錄：Neo 代墊 1500元（晚餐燒肉），由3人分攤（Neo、小華、小明），每人約500.00元` |

Editing or deleting an existing expense is LIFF-only (`/懶人包`) — repeated per-item actions
like that would flood the chat as commands.

**🗳️ Voting**

| Command | What it does | Example |
| --- | --- | --- |
| `/投票 開始 <topic>` | Starts a poll (one active poll per trip at a time). | `/投票 開始 晚餐吃什麼` → `🗳️ 投票「晚餐吃什麼」開始了！...` |
| `/投票 新增 <option>` | Adds a candidate option. | `/投票 新增 燒肉` → `➕ 已新增選項「燒肉」。目前選項：\n1. 燒肉` |
| `/投票 結果` | Shows current vote counts and who voted for what. | `/投票 結果` → per-option tallies with voter names |
| `/投票 結束` | Locks the poll and shows the final result. Anyone can end it. | `/投票 結束` → `🏁 投票結束！` + final tallies |

Actually casting/changing a vote is LIFF-only (`/懶人包`), multi-select, toggle on tap — again,
to avoid every vote being its own chat message.

**⚙️ Other**

| Command | What it does | Example |
| --- | --- | --- |
| `/整理` | Manually triggers the organize step (normally runs hourly on its own). | `/整理` → `✅ 整理完成，處理了 8 則訊息。` or `⚠️ 目前沒有新訊息可整理` |
| `/檢查` | Shows anything the fact-check pass flagged as unsupported by the source messages. | `/檢查` → a list of suspect claims + why, or `✅ 目前沒有發現可疑內容` |
| `/說明` | Shows the full command list. | — |

Model selection used to be a `/模型` chat command (admin-only). It's now a per-trip picker on
the LIFF page instead — see "What it does" above.

## Design choices worth knowing before you deploy this

- **All bot replies are command-triggered.** The only thing the bot ever does without being
  asked is capture messages while a trip is active — no unsolicited chatter.
- **The LIFF page has no access control.** Anyone with the link can view and edit it (expense
  edits, poll votes, model picker, etc.). This matches the trust model of a small friend
  group; it is not meant for anything with untrusted participants.
- **One trip active per group at a time.** Start a new one with `/旅程 開始`, close the
  current one with `/旅程 結束` (only the person who started it can end it, unless you set an
  `ADMIN_USER_ID` override — see below).
- **Traditional Chinese only**, currently. All commands, replies, and the LIFF UI are
  hardcoded in 繁體中文.
- **Friending the bot matters.** LINE's profile API only resolves a real display name for
  people who've added the bot as a friend (the `/懶人包` nudge on `/旅程 開始` exists because
  of this) — anyone who hasn't shows up as a raw LINE user ID instead. This self-heals
  automatically for anything organized *after* they friend the bot; text already written into
  the doc before that needs the LIFF page's 🔄 修正名稱顯示 button (or another `/整理` pass,
  for messages not yet organized).

## Why two deployments?

The original implementation (`worker/`) runs on Cloudflare Python Workers. It works, but has
one real, sharp-edged platform constraint: a Worker's `fetch()` handler has a hard ~30 second
execution ceiling that a caught exception can't always help with — a hard platform kill
doesn't raise a catchable Python exception, so a slow LLM call on the webhook path can just
silently vanish mid-request. The hourly cron sweep (`scheduled()`) doesn't share this limit
(its own ~15-minute budget), which is why time-sensitive logic had to be split across two
different timeout budgets depending on which path triggered it. See `docs/plan.md`'s numbered
discoveries list for the full story of what that cost in development pain.

`cloudrun/` replatforms the same product onto Google Cloud Run — a real long-lived container
process, not a FaaS sandbox with a short hard ceiling — while keeping the *same* D1 database
and R2 bucket (no data migration between the two; only the access method changes, from a
Workers binding to D1's REST API and R2's S3-compatible API). Every webhook event is now
processed fully synchronously before replying to LINE, which is both simpler and more
reliable than the "ack fast, process in the background" pattern Cloudflare's `ctx.waitUntil()`
required. `worker/` is kept deployed and untouched specifically so that switching the LINE
webhook/LIFF URLs back to it is always a fast, clean rollback if `cloudrun/` ever needs one.

## Architecture

**Current (`cloudrun/`):**

- **Google Cloud Run** (Python/FastAPI) for the whole backend — one `/webhook` route, one
  `/liff` + REST API for the LIFF page, one `/internal/scheduled-organize` route driven by
  **Cloud Scheduler** in place of Cloudflare's Cron Trigger.
- **Cloudflare D1** (SQLite) for everything relational, accessed via its
  [REST API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
  rather than a Workers binding, since Cloud Run isn't a Workers runtime.
- **Cloudflare R2** for photo backups, accessed via its S3-compatible API through `boto3`
  (`cloudrun/src/r2.py`) for the same reason.
- **Anthropic or OpenAI API** via the official Python SDKs (no Pyodide-specific transport
  needed on Cloud Run, unlike `worker/`). Which model runs each stage is a per-trip LIFF
  setting (NULL = fall back to `llm_client.py`'s constants), not a global admin-gated chat
  command.
- **LINE Messaging API** for the bot itself, plus a separate **LINE Login** channel for the
  LIFF app.

**Legacy (`worker/`):**

- **Cloudflare Python Workers** (Pyodide-based) — one `fetch()` handler for the LINE webhook
  + LIFF API, one `scheduled()` handler for the hourly organize/fact-check sweep.
- Same D1/R2 usage, but through native Workers bindings instead of REST/S3 APIs.
- Anthropic API via a Pyodide-specific `httpx2` transport fork (the sync client and stock
  `httpx` don't work under Workers' Emscripten runtime).

## Cost

Realistically **close to $0/month** at friend-group scale, on either deployment:

- **Cloud Run** (if running `cloudrun/`): free tier covers 2M requests/month and 180k
  vCPU-seconds/month — a trip's webhook + hourly-cron traffic doesn't come close.
- **Cloudflare Workers** (if running `worker/`): free tier (100k requests/day). Either way,
  **D1** (5GB storage, 5M reads + 100k writes/day) and **R2** (10GB storage, no egress fees)
  free tiers comfortably cover a small trip's message volume and photo storage.
- **LINE Messaging API**: $0. Every reply the bot sends is a *reply* message (triggered by a
  command, using the event's reply token), which LINE doesn't charge for or count against any
  quota. The bot never sends push messages.
- **LLM API calls**: the one real cost, billed per token, to whichever model each trip is
  using (LIFF picker, or the `llm_client.py` defaults if never changed). Organizing runs in
  small, capped batches (not one call per message), and only when there's something new to
  fold in — an active trip with a normal amount of chatter has cost well under $1/month in
  practice. `/花費` shows the actual running total for your own trip, in USD, at any time.

The one scenario worth knowing about: if the organize sweep somehow falls behind for a long
stretch (see `docs/plan.md`'s discoveries list for how that happened once during
development), the backlog just takes a few more batches to clear — it doesn't retry in a loop
or otherwise multiply cost. `max_retries=0` is set deliberately for this reason.

## Setup: Cloud Run (`cloudrun/`, current)

You'll need: a GCP account with billing enabled, a Cloudflare account, a LINE Developers
account, `gcloud`, and either an Anthropic or OpenAI API key.

### 1. LINE Messaging API channel (the bot itself)

1. Create a Messaging API channel in the [LINE Developers Console](https://developers.line.biz/).
2. Note the **Channel secret** and issue a long-lived **Channel access token**.
3. In [manager.line.biz](https://manager.line.biz/) (not the developers console) for this
   Official Account, enable **"Allow bot to join group chats."** This setting is easy to
   miss and the bot won't receive group messages without it.
4. Leave the webhook URL blank for now — you'll set it after deploying.

### 2. LINE Login channel (for the LIFF app)

1. Create a **LINE Login** channel (separate from the Messaging API channel above).
2. Add a LIFF app under it, endpoint URL `https://<your-cloud-run-url>/liff`, scope `profile`.
3. Note the LIFF ID.
4. Publish the channel (it starts in "Developing" status, which blocks anyone but you from
   using the LIFF page).

### 3. Cloudflare D1 and R2

```sh
npx wrangler login   # if you haven't already
npx wrangler d1 create itinerary-manager-db   # note the database_id it prints
npx wrangler d1 execute itinerary-manager-db --remote --file cloudrun/schema.sql
npx wrangler r2 bucket create <a-globally-unique-bucket-name>
```

Enable public access on the R2 bucket (Cloudflare dashboard → your bucket → Settings) and
note the `pub-*.r2.dev` URL it gives you.

Create a Cloudflare API token (dashboard → My Profile → API Tokens → "Create Token",
permission `D1:Edit` scoped to your database) — Cloud Run isn't a Workers runtime, so it
reaches D1 over its REST API instead of a binding, and that needs its own token.

Create an R2 API token too (dashboard → R2 → Manage R2 API Tokens → "Create API token",
permission `Object Read & Write` scoped to your bucket) for `R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY` — a different kind of token than the D1 one above.

### 4. GCP project and Cloud Run

```sh
gcloud projects create <your-project-id>
gcloud billing projects link <your-project-id> --billing-account=<your-billing-account-id>
gcloud config set project <your-project-id>
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com
```

Copy `cloudrun/.env.example` to `cloudrun/.env` and fill in every value (LINE channel
secret/token, LLM API key, the Cloudflare account/database ID and API token, the R2 keys and
bucket info, the LIFF ID from step 2, and a random `SCHEDULER_SECRET`, e.g.
`openssl rand -hex 32`).

Deploy, passing `.env`'s contents as environment variables (don't leave a
`--set-env-vars` list containing secrets in plain text in shell history — pipe it from the
file):

```sh
cd cloudrun
gcloud run deploy itinerary-manager \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --env-vars-file <(awk -F= '!/^#/ && NF {print $1": \""$2"\""}' .env)
```

Note the service URL it prints. Set the Messaging API channel's webhook URL to
`https://<that-url>/webhook` and verify it in the LINE Developers Console.

### 5. Cloud Scheduler (hourly organize)

```sh
gcloud scheduler jobs create http itinerary-manager-organize \
  --location asia-east1 \
  --schedule "0 * * * *" \
  --uri "https://<your-cloud-run-url>/internal/scheduled-organize" \
  --http-method POST \
  --headers "x-scheduler-secret=$(grep ^SCHEDULER_SECRET= cloudrun/.env | cut -d= -f2-)"
```

Add the bot to a LINE group and run `/旅程 開始 <name>` — it should start recording.

> **Ask everyone in the group to add the bot as a 1:1 friend too** (the bot itself reminds
> people of this on `/旅程 開始`). Display names are looked up via LINE's 1:1 profile API,
> which only resolves for people who have friended the bot — anyone who hasn't will show up
> as their raw LINE user ID in the trip doc and replies instead of their name. If they friend
> the bot later, this self-heals automatically the next time their name is looked up, or via
> the LIFF page's 🔄 修正名稱顯示 button for text already written into the doc.

### Local development

```sh
cd cloudrun
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd src && uvicorn main:app --reload --port 8080
```

Local runs hit the same remote D1 (via REST API) and remote R2/LLM provider as production —
there's no local database to spin up separately.

## Setup: Cloudflare Workers (`worker/`, legacy)

Kept for reference and as a rollback path. You'll need: a Cloudflare account, a LINE
Developers account, `uv`, and an Anthropic API key.

Steps 1–2 (LINE Messaging API channel, LINE Login channel) are identical to the Cloud Run
setup above, except the LIFF endpoint URL is `https://<your-worker>.workers.dev/liff`.

### 3. Cloudflare resources

```sh
cd worker
npm install
npx wrangler login   # if you haven't already
npx wrangler d1 create itinerary-manager-db   # note the database_id it prints
npx wrangler r2 bucket create <a-globally-unique-bucket-name>
```

Enable public access on the R2 bucket (Cloudflare dashboard → your bucket → Settings) and
note the `pub-*.r2.dev` URL it gives you.

Copy `wrangler.jsonc.example` to `wrangler.jsonc` and fill in the database name/ID, bucket
name, R2 public URL, and LIFF ID from the steps above.

Apply the schema:

```sh
npx wrangler d1 execute itinerary-manager-db --remote --file schema.sql
```

### 4. Secrets

```sh
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put LINE_CHANNEL_SECRET
npx wrangler secret put LINE_CHANNEL_ACCESS_TOKEN
npx wrangler secret put ADMIN_USER_ID   # optional - see below
npx wrangler secret put OPENAI_API_KEY  # optional - see below
```

For local development, copy `.dev.vars.example` to `.dev.vars` and fill in the same values.

`ADMIN_USER_ID` is optional: a LINE user ID that can end (or force-end) any trip regardless
of who started it. Leave it unset if you don't want that override.

`OPENAI_API_KEY` is optional: only needed if you want to select an OpenAI model for a stage.
Leave it unset to run on Claude models only.

### 5. Deploy and connect the webhook

```sh
npm run deploy   # uv run pywrangler deploy
```

Set the Messaging API channel's webhook URL to `https://<your-worker>.workers.dev/webhook`
and verify it in the LINE Developers Console. Add the bot to a group, run `/旅程 開始 <name>`,
and it should start recording.

### Local development

```sh
uv venv && uv sync   # for editor autocomplete/type hints
npm run dev           # uv run pywrangler dev - local dev server
```

Note: `ctx.waitUntil()` (used for all deferred webhook processing) does not run correctly
under local dev — anything past the immediate HTTP response needs a real deployment to test.

## License

[MIT](./LICENSE)
