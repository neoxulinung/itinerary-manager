# Itinerary Manager — a LINE trip-planning bot

[繁體中文](./README.zh-TW.md)

A LINE bot that passively reads a group chat during an active "trip," uses Claude to
maintain a living markdown summary of what's decided and what's still open, and exposes it
through a LIFF web page with expense-splitting and voting built in.

Built for a small group of friends planning a trip together (2–10 people) — not a
multi-tenant SaaS product. See [`docs/user_stories.md`](docs/user_stories.md) and
[`docs/plan.md`](docs/plan.md) for the full design rationale, the "LLM-Wiki" architecture
this is based on, and a running log of real bugs found and fixed along the way.

## What it does

- **Passive capture**: while a trip is active, every message (and photo) sent in the LINE
  group is recorded. The bot stays silent otherwise — it never speaks unless a command asks
  it to.
- **LLM-organized doc**: on a schedule (and on demand via `/整理`), Claude folds newly
  captured messages into a single markdown document per trip — a timeline of decided plans,
  a list of open questions, and everything else (tickets, links, confirmations). Every
  conclusion carries a source citation (who said it, when).
- **Q&A with grounding**: `/問 <question>` answers from the trip document only, never from
  the model's own knowledge, and says "not decided yet" when the doc doesn't have an answer.
- **LIFF page**: a shareable web page rendering the current doc, expenses, and any active
  poll, reachable via `/懶人包`.
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
| `/旅程 開始 <name>` | Starts a trip. From here on, every message in the group is recorded. | `/旅程 開始 東京五日遊` → `🧳 旅程「東京五日遊」開始了！我會開始記錄接下來的討論。` |
| `/旅程 結束` | Ends the current trip: folds in anything not yet organized, then appends the final expense settlement. Only the person who started it can end it (or the `ADMIN_USER_ID` override). | `/旅程 結束` → `🏁 旅程「東京五日遊」結束了！` + a settlement message |

**🔍 Query (read-only, work anytime)**

| Command | What it does | Example |
| --- | --- | --- |
| `/問 <question>` | Answers strictly from the trip document — never guesses, says "not decided yet" if the doc doesn't cover it. | `/問 我們住哪間飯店` → an answer grounded in what was actually discussed, or `⚠️ 還沒決定` |
| `/未定事項` | Lists everything still open/undecided. | `/未定事項` → the current "未定事項" section as plain text |
| `/懶人包` | Sends a LIFF page link (timeline, expenses, poll all in one page). | `/懶人包` → a LINE button template linking to the LIFF page |
| `/花費` | Shows accumulated Anthropic API cost for this trip and overall. | `/花費` → `🤖 這趟旅程：12次AI呼叫，約 US$0.0187（輸入9,204／輸出2,150 tokens）` |
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

## Design choices worth knowing before you deploy this

- **All bot replies are command-triggered.** The only thing the bot ever does without being
  asked is capture messages while a trip is active — no unsolicited chatter.
- **The LIFF page has no access control.** Anyone with the link can view and edit it (expense
  edits, poll votes, etc.). This matches the trust model of a small friend group; it is not
  meant for anything with untrusted participants.
- **One trip active per group at a time.** Start a new one with `/旅程 開始`, close the
  current one with `/旅程 結束` (only the person who started it can end it, unless you set an
  `ADMIN_USER_ID` override — see below).
- **Traditional Chinese only**, currently. All commands, replies, and the LIFF UI are
  hardcoded in 繁體中文.

## Architecture

- **Cloudflare Python Workers** (Pyodide-based) for the whole backend — one `fetch()`
  handler for the LINE webhook + LIFF API, one `scheduled()` handler for the hourly
  organize/fact-check sweep.
- **D1** (SQLite) for everything relational: messages, the trip doc and its revision
  history, expenses, polls, LLM usage/cost tracking.
- **R2** for photo backups.
- **Anthropic API** (`claude-sonnet-5` for organizing, `claude-haiku-4-5` for Q&A and
  fact-checking) via the official Python SDK — the sync client doesn't work under Workers,
  `AsyncAnthropic` is required.
- **LINE Messaging API** for the bot itself, plus a separate **LINE Login** channel for the
  LIFF app (LINE no longer allows LIFF apps on a Messaging API channel).

A real, somewhat unusual platform constraint worth knowing if you touch the webhook path:
Cloudflare's `fetch()` handler has a hard ~30 second execution ceiling that a caught
exception can't always help with (a hard platform kill doesn't raise a catchable Python
exception). The hourly cron sweep (`scheduled()`) doesn't share this limit — it gets its own
~15-minute budget — which is why it's the reliable path for anything that might run long.
See `docs/plan.md`'s numbered discoveries list for the full story.

## Cost

Realistically **close to $0/month** at friend-group scale. Where it could come from:

- **Cloudflare (Workers, D1, R2, Cron Triggers)**: free tier. A small trip's message volume,
  D1 reads/writes, and photo storage don't come close to the free-tier limits (Workers: 100k
  requests/day; D1: 5GB storage, 5M reads + 100k writes/day; R2: 10GB storage, no egress
  fees). This project has never needed the paid plan.
- **LINE Messaging API**: $0. Every reply the bot sends is a *reply* message (triggered by a
  command, using the event's reply token), which LINE doesn't charge for or count against any
  quota. The bot never sends push messages.
- **Anthropic API**: the one real cost, billed per token. `claude-sonnet-5` organizes
  (`$2`/`$10` per 1M input/output tokens), `claude-haiku-4-5` handles Q&A and fact-checking
  (`$1`/`$5` per 1M input/output tokens). Organizing runs in small, capped batches (not one
  call per message), and only when there's something new to fold in — an active trip with
  a normal amount of chatter has cost well under $1/month in practice. `/花費` shows the
  actual running total for your own trip, in USD, at any time.

The one scenario worth knowing about: if the hourly cron sweep somehow falls behind for a
long stretch (see `docs/plan.md`'s discoveries list for how that happened once during
development), the backlog just takes a few more batches to clear — it doesn't retry in a
loop or otherwise multiply cost. `max_retries=0` is set deliberately for this reason.

## Setup

You'll need: a Cloudflare account, a LINE Developers account, `uv`, and an Anthropic API key.

### 1. LINE Messaging API channel (the bot itself)

1. Create a Messaging API channel in the [LINE Developers Console](https://developers.line.biz/).
2. Note the **Channel secret** and issue a long-lived **Channel access token**.
3. In [manager.line.biz](https://manager.line.biz/) (not the developers console) for this
   Official Account, enable **"Allow bot to join group chats."** This setting is easy to
   miss and the bot won't receive group messages without it.
4. Leave the webhook URL blank for now — you'll set it after deploying.

### 2. LINE Login channel (for the LIFF app)

1. Create a **LINE Login** channel (separate from the Messaging API channel above).
2. Add a LIFF app under it, endpoint URL `https://<your-worker>.workers.dev/liff`, scope
   `profile`.
3. Note the LIFF ID.
4. Publish the channel (it starts in "Developing" status, which blocks anyone but you from
   using the LIFF page).

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
```

For local development, copy `.dev.vars.example` to `.dev.vars` and fill in the same values.

`ADMIN_USER_ID` is optional: a LINE user ID that can end (or force-end) any trip regardless
of who started it. Leave it unset if you don't want that override.

### 5. Deploy and connect the webhook

```sh
npm run deploy   # uv run pywrangler deploy
```

Set the Messaging API channel's webhook URL to `https://<your-worker>.workers.dev/webhook`
and verify it in the LINE Developers Console. Add the bot to a group, run `/旅程 開始 <name>`,
and it should start recording.

> **Ask everyone in the group to add the bot as a 1:1 friend too.** Display names are looked
> up via LINE's 1:1 profile API, which only resolves for people who have friended the bot —
> anyone who hasn't will show up as their raw LINE user ID in the trip doc and replies
> instead of their name. If they friend the bot later, this self-heals automatically the next
> time their name is looked up (no need to re-add anything).

### Local development

```sh
uv venv && uv sync   # for editor autocomplete/type hints
npm run dev           # uv run pywrangler dev - local dev server
```

Note: `ctx.waitUntil()` (used for all deferred webhook processing) does not run correctly
under local dev — anything past the immediate HTTP response needs a real deployment to test.

## License

[MIT](./LICENSE)
