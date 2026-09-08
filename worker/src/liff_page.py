_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>旅程時間軸</title>
<script src="https://cdn.jsdelivr.net/npm/marked@18.0.11/lib/marked.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.4.14/dist/purify.min.js"></script>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang TC", "Microsoft JhengHei", sans-serif;
         margin: 0; padding: 16px; line-height: 1.65; color: #222; background: #f5f6f8; }
  .status { display: inline-block; font-size: 13px; padding: 3px 12px; border-radius: 999px;
            margin-bottom: 14px; font-weight: 600; }
  .status.active { background: #e6f4ea; color: #1a7f37; }
  .status.ended { background: #eee; color: #666; }
  h1 { font-size: 22px; margin: 8px 0; }
  h2 { font-size: 17px; margin-top: 32px; margin-bottom: 12px; padding: 9px 12px; background: #fff;
       border-left: 4px solid #06c755; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.05); }
  h3 { font-size: 15px; color: #444; margin-top: 16px; }
  ul { padding-left: 22px; }
  li { margin-bottom: 8px; }
  li:has(> input[type=checkbox]) {
    list-style: none; margin-left: -22px; background: #fff8ec; border: 1px solid #ffe4b5;
    border-radius: 6px; padding: 8px 10px;
  }
  input[type=checkbox] { transform: scale(1.25); margin-right: 8px; accent-color: #ff9800; vertical-align: middle; }
  img { max-width: 100%; border-radius: 8px; margin: 6px 0; }
  em { color: #888; font-size: 13px; font-style: normal; }
  #page { max-width: 640px; margin: 0 auto; }
  .loading, .error { text-align: center; color: #888; padding: 40px 0; }
  .card { background: #fff; border-radius: 10px; padding: 14px; margin-bottom: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .card .meta { color: #888; font-size: 13px; margin-top: 4px; }
  .card .row { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .card button { border: none; background: #eee; border-radius: 6px; padding: 5px 12px;
                 font-size: 13px; cursor: pointer; white-space: nowrap; }
  .card button.danger { background: #fde8e8; color: #b42318; }
  #settlementText { white-space: pre-wrap; background: #fff; padding: 14px; border-radius: 10px; font-size: 14px;
                     border-left: 4px solid #06c755; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .field { margin-bottom: 10px; }
  .field label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; font-weight: 600; }
  .field input[type=number], .field input[type=text] {
    width: 100%; box-sizing: border-box; padding: 9px; border: 1px solid #ddd; border-radius: 6px; font-size: 15px;
  }
  .checks { display: flex; flex-wrap: wrap; gap: 6px 14px; }
  .checks label { font-size: 14px; font-weight: normal; display: flex; align-items: center; gap: 4px; }
  .primary-btn { background: #06c755; color: #fff; border: none; border-radius: 6px;
                 padding: 10px 16px; font-size: 15px; cursor: pointer; margin-right: 8px; font-weight: 600; }
  .link-btn { background: none; border: none; color: #666; font-size: 14px; cursor: pointer; }
</style>
</head>
<body>
<div id="page" class="loading">載入中...</div>
<script>
let TRIP_ID = null;
let TRIP_DATA = null;
let ME = null;           // { userId, displayName } from liff.getProfile()
let EDITING_ID = null;   // expense id being edited, or null for "create new"
let DOC_HISTORY = [];    // last fetched revisions list, for viewDocRevision(index) to read back

function escapeHtml(s) {
  // Covers all five characters that matter in both text and (double-quoted) attribute
  // context - the textContent round-trip some earlier code used only covered &<>, which
  // left a stored-XSS hole wherever an escaped name lands inside value="..."/data-*="...".
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const err = new Error('request failed: ' + res.status);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function loadTrip() {
  const data = await api('/api/trips/' + encodeURIComponent(TRIP_ID));
  TRIP_DATA = data;
  document.title = data.name;
  render(data);
}

// The doc's headings are a fixed schema written by organize.py's prompt (## 時間軸 /
// ## 未定事項 / ## 其他資訊) - decorate those known headings with an icon before parsing,
// simplest way to give each section a visual identity without per-section CSS targeting
// (CSS can't select by heading text).
function decorateHeadings(md) {
  return md
    .replace(/^## 時間軸/m, '## 🗓️ 時間軸')
    .replace(/^## 未定事項/m, '## ❓ 未定事項')
    .replace(/^## 其他資訊/m, '## 📎 其他資訊');
}

function render(data) {
  const statusLabel = data.status === 'active' ? '進行中' : '已結束';
  // content_md is built from raw LINE group messages via an LLM prompt that only
  // screens for travel relevance, not HTML safety - sanitize before it ever touches
  // innerHTML, since marked does not escape/sanitize embedded HTML on its own.
  const itineraryBody = data.content_md
    ? DOMPurify.sanitize(marked.parse(decorateHeadings(data.content_md)))
    : '<p style="color:#888">這趟旅程還沒有整理出任何內容</p>';

  document.getElementById('page').innerHTML =
    '<span class="status ' + data.status + '">' + statusLabel + '</span>' +
    '<div style="margin:6px 0 10px;">' +
      '<button class="link-btn" onclick="startDocEdit()">✏️ 編輯文件</button>　' +
      '<button class="link-btn" onclick="toggleDocHistory()">🕘 編輯紀錄</button>' +
    '</div>' +
    '<div id="docView">' + itineraryBody + '</div>' +
    '<div id="docEditForm" style="display:none">' +
      '<textarea id="docTextarea" style="width:100%; box-sizing:border-box; min-height:280px; ' +
        'font-family:monospace; font-size:13px; padding:10px; border:1px solid #ddd; border-radius:6px;"></textarea>' +
      '<div style="margin-top:8px;">' +
        '<button class="primary-btn" onclick="submitDocEdit()">儲存</button>' +
        '<button class="link-btn" onclick="cancelDocEdit()">取消</button>' +
      '</div>' +
    '</div>' +
    '<div id="docHistory" style="display:none"></div>' +
    '<h2>💰 記帳</h2>' +
    '<pre id="settlementText"></pre>' +
    '<div id="expenseList"></div>' +
    renderExpenseForm() +
    '<h2>🗳️ 投票</h2>' +
    '<div id="pollSection"></div>';

  document.getElementById('settlementText').textContent = data.settlement;
  document.getElementById('expenseList').innerHTML = data.expenses.map(renderExpenseCard).join('') ||
    '<p style="color:#888">還沒有任何帳目</p>';
  renderParticipantChecks(data.participants);
  document.getElementById('pollSection').innerHTML = renderPoll(data.poll);
}

function startDocEdit() {
  document.getElementById('docHistory').style.display = 'none';
  document.getElementById('docView').style.display = 'none';
  document.getElementById('docTextarea').value = TRIP_DATA.content_md || '';
  document.getElementById('docEditForm').style.display = 'block';
}

function cancelDocEdit() {
  document.getElementById('docEditForm').style.display = 'none';
  document.getElementById('docView').style.display = 'block';
}

async function submitDocEdit() {
  if (!ME || !ME.userId) {
    alert('無法取得你的LINE身分，請確認是在LINE App內開啟這個頁面');
    return;
  }
  const content_md = document.getElementById('docTextarea').value;
  try {
    await api('/api/trips/' + TRIP_ID + '/doc', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content_md, userId: ME.userId, displayName: ME.displayName }),
    });
    await loadTrip();
  } catch (e) {
    // 400 = validate_doc rejected it (missing title or 未定事項 heading) - the only failure
    // mode worth a specific message, since it's the one thing an editor can actually fix.
    alert(e.status === 400 ? '儲存失敗：文件開頭要有「# 旅程名稱」，且不能刪掉「## 未定事項」這個標題' : '儲存失敗，請稍後再試');
  }
}

async function toggleDocHistory() {
  const el = document.getElementById('docHistory');
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    return;
  }
  document.getElementById('docEditForm').style.display = 'none';
  document.getElementById('docView').style.display = 'block';
  try {
    const data = await api('/api/trips/' + TRIP_ID + '/doc/revisions');
    DOC_HISTORY = data.revisions;
    el.innerHTML = renderDocHistory(DOC_HISTORY);
    el.style.display = 'block';
    // The list renders below the whole doc body - on a phone screen that's well off the
    // bottom of the viewport, so without this the button visibly does nothing (reported live:
    // "按了沒有反應" - it had actually worked, just out of view below the fold).
    el.scrollIntoView({ behavior: 'smooth' });
  } catch (e) {
    alert('讀取編輯紀錄失敗，請稍後再試');
  }
}

function renderDocHistory(revisions) {
  if (!revisions.length) return '<p style="color:#888">還沒有任何紀錄</p>';
  return revisions.map((r, i) => {
    const time = new Date(r.created_at * 1000).toLocaleString('zh-TW', { hour12: false });
    return '<div class="card"><div class="row">' +
      '<div><strong>' + escapeHtml(r.editor) + '</strong><div class="meta">' + time + '</div></div>' +
      '<div>' +
        '<button onclick="viewDocRevision(' + i + ')">查看</button> ' +
        '<button onclick="restoreDocRevision(\\'' + r.id + '\\')">還原到這版</button>' +
      '</div>' +
    '</div></div>';
  }).join('');
}

function viewDocRevision(index) {
  const r = DOC_HISTORY[index];
  if (!r) return;
  document.getElementById('docHistory').style.display = 'none';
  document.getElementById('docEditForm').style.display = 'none';
  const time = new Date(r.created_at * 1000).toLocaleString('zh-TW', { hour12: false });
  const view = document.getElementById('docView');
  view.style.display = 'block';
  view.innerHTML =
    '<div class="card">' +
      '<div class="meta">正在查看歷史版本：' + escapeHtml(r.editor) + '　' + time + '</div>' +
      '<div style="margin-top:8px;">' +
        '<button onclick="render(TRIP_DATA)">↩️ 返回目前版本</button> ' +
        '<button onclick="restoreDocRevision(\\'' + r.id + '\\')">還原到這版</button>' +
      '</div>' +
    '</div>' +
    DOMPurify.sanitize(marked.parse(decorateHeadings(r.content_md)));
}

async function restoreDocRevision(revisionId) {
  if (!ME || !ME.userId) {
    alert('無法取得你的LINE身分，請確認是在LINE App內開啟這個頁面');
    return;
  }
  if (!confirm('確定要還原到這個版本嗎？目前的內容不會被刪除，會留在編輯紀錄裡。')) return;
  try {
    await api('/api/trips/' + TRIP_ID + '/doc/revisions/' + revisionId + '/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId: ME.userId, displayName: ME.displayName }),
    });
    await loadTrip();
  } catch (e) {
    alert('還原失敗，請稍後再試');
  }
}

function renderPoll(poll) {
  if (!poll) {
    return '<p style="color:#888">還沒有任何投票</p>';
  }
  const statusLabel = poll.status === 'active' ? '進行中' : '已結束';
  const optionsHtml = poll.options.map(o => renderPollOption(poll, o)).join('') ||
    '<p style="color:#888">還沒有任何選項</p>';
  const addForm = poll.status === 'active'
    ? '<div style="margin-top:10px; display:flex; gap:6px;">' +
        '<input type="text" id="pollOptionInput" placeholder="新增選項" style="flex:1; padding:6px 8px; border:1px solid #ddd; border-radius:6px;">' +
        '<button type="button" onclick="submitPollOption()">新增</button>' +
      '</div>'
    : '';
  return '<div class="card"><strong>' + escapeHtml(poll.topic) + '</strong> ' +
    '<span class="status ' + poll.status + '">' + statusLabel + '</span></div>' +
    optionsHtml + addForm;
}

function renderPollOption(poll, o) {
  const votedByMe = ME && o.votes.some(v => v.line_user_id === ME.userId);
  const names = o.votes.map(v => escapeHtml(v.display_name)).join('、') || '（尚無人投）';
  const voteBtn = poll.status === 'active'
    ? '<button onclick="castVote(\\'' + o.id + '\\')"' +
        (votedByMe ? ' style="background:#06c755;color:#fff;"' : '') + '>' +
        (votedByMe ? '✅ 已投' : '🗳️ 投給這個') + '</button>'
    : '';
  return '<div class="card"><div class="row">' +
    '<div>' + escapeHtml(o.text) + '<div class="meta">' + o.votes.length + '票：' + names + '</div></div>' +
    '<div>' + voteBtn + '</div>' +
  '</div></div>';
}

async function submitPollOption() {
  const input = document.getElementById('pollOptionInput');
  const text = input.value.trim();
  if (!text || !TRIP_DATA.poll) return;
  try {
    await api('/api/trips/' + TRIP_ID + '/polls/' + TRIP_DATA.poll.id + '/options', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    await loadTrip();
  } catch (e) {
    alert('新增失敗，請稍後再試');
  }
}

async function castVote(optionId) {
  if (!ME || !ME.userId) {
    alert('無法取得你的LINE身分，請確認是在LINE App內開啟這個頁面');
    return;
  }
  try {
    await api('/api/trips/' + TRIP_ID + '/polls/' + TRIP_DATA.poll.id + '/options/' + optionId + '/vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId: ME.userId, displayName: ME.displayName }),
    });
    await loadTrip();
  } catch (e) {
    alert('投票失敗，請稍後再試');
  }
}

function renderExpenseCard(e) {
  const names = e.participants.map(p => escapeHtml(p.display_name)).join('、');
  return '<div class="card">' +
    '<div class="row">' +
      '<div>' +
        '<strong>' + escapeHtml(e.payer_display_name) + '</strong> 代墊 ' + e.amount.toFixed(2) + ' 元' +
        '<div class="meta">' + escapeHtml(e.description) + '　·　分攤：' + names + '</div>' +
      '</div>' +
      '<div>' +
        '<button onclick="startEdit(\\'' + e.id + '\\')">✏️ 編輯</button> ' +
        '<button class="danger" onclick="removeExpense(\\'' + e.id + '\\')">🗑️ 刪除</button>' +
      '</div>' +
    '</div>' +
  '</div>';
}

function renderExpenseForm() {
  return '<div id="expenseForm">' +
    '<h3 id="formTitle">新增帳目</h3>' +
    '<div class="field"><label>金額</label><input type="number" id="amountInput" step="0.01" min="0"></div>' +
    '<div class="field"><label>說明</label><input type="text" id="descInput"></div>' +
    '<div class="field"><label>分攤對象</label><div class="checks" id="participantChecks"></div>' +
      '<div style="margin-top:8px; display:flex; gap:6px;">' +
        '<input type="text" id="guestNameInput" placeholder="名單裡沒有的人，輸入名字新增" style="flex:1; padding:6px 8px; border:1px solid #ddd; border-radius:6px;">' +
        '<button type="button" onclick="addGuestParticipant()">新增</button>' +
      '</div>' +
    '</div>' +
    '<button class="primary-btn" onclick="submitExpense()">送出</button>' +
    '<button class="link-btn" id="cancelBtn" style="display:none" onclick="cancelEdit()">取消編輯</button>' +
  '</div>';
}

function participantCheckboxHtml(id, name, checked) {
  return '<label><input type="checkbox" value="' + escapeHtml(id) + '" data-name="' + escapeHtml(name) +
    '"' + (checked ? ' checked' : '') + '> ' + escapeHtml(name) + '</label>';
}

function renderParticipantChecks(participants) {
  document.getElementById('participantChecks').innerHTML =
    participants.map(p => participantCheckboxHtml(p.line_user_id, p.display_name, true)).join('');
}

function addGuestParticipant() {
  const input = document.getElementById('guestNameInput');
  const name = input.value.trim();
  if (!name) return;
  const id = 'guest:' + name;
  const el = document.getElementById('participantChecks');
  if (el.querySelector('input[value="' + CSS.escape(id) + '"]')) { input.value = ''; return; }
  el.insertAdjacentHTML('beforeend', participantCheckboxHtml(id, name, true));
  input.value = '';
}

function checkedParticipants() {
  const boxes = document.querySelectorAll('#participantChecks input:checked');
  return Array.from(boxes).map(b => ({ line_user_id: b.value, display_name: b.dataset.name }));
}

async function submitExpense() {
  const amount = parseFloat(document.getElementById('amountInput').value);
  const description = document.getElementById('descInput').value.trim();
  const participants = checkedParticipants();
  if (!amount || amount <= 0) { alert('請輸入正確的金額'); return; }
  if (!description) { alert('請輸入說明'); return; }
  if (participants.length === 0) { alert('至少要選一個分攤對象'); return; }
  if (!EDITING_ID && (!ME || !ME.userId)) {
    alert('無法取得你的LINE身分，請確認是在LINE App內開啟這個頁面');
    return;
  }

  try {
    if (EDITING_ID) {
      await api('/api/trips/' + TRIP_ID + '/expenses/' + EDITING_ID, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount, description, participants }),
      });
    } else {
      await api('/api/trips/' + TRIP_ID + '/expenses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId: ME.userId, displayName: ME.displayName, amount, description, participants }),
      });
    }
    cancelEdit();
    await loadTrip();
  } catch (e) {
    alert('送出失敗，請稍後再試');
  }
}

function startEdit(expenseId) {
  const e = TRIP_DATA.expenses.find(x => x.id === expenseId);
  if (!e) return;
  EDITING_ID = expenseId;
  document.getElementById('formTitle').textContent = '編輯帳目';
  document.getElementById('amountInput').value = e.amount;
  document.getElementById('descInput').value = e.description;
  const participantIds = new Set(e.participants.map(p => p.line_user_id));
  document.querySelectorAll('#participantChecks input').forEach(b => { b.checked = participantIds.has(b.value); });
  document.getElementById('cancelBtn').style.display = 'inline';
  document.getElementById('expenseForm').scrollIntoView({ behavior: 'smooth' });
}

function cancelEdit() {
  EDITING_ID = null;
  document.getElementById('formTitle').textContent = '新增帳目';
  document.getElementById('amountInput').value = '';
  document.getElementById('descInput').value = '';
  document.querySelectorAll('#participantChecks input').forEach(b => { b.checked = true; });
  document.getElementById('cancelBtn').style.display = 'none';
}

async function removeExpense(expenseId) {
  if (!confirm('確定要刪除這筆帳目嗎？')) return;
  try {
    await api('/api/trips/' + TRIP_ID + '/expenses/' + expenseId, { method: 'DELETE' });
    await loadTrip();
  } catch (e) {
    alert('刪除失敗，請稍後再試');
  }
}

async function main() {
  const pageEl = document.getElementById('page');
  try { await liff.init({ liffId: "__LIFF_ID__" }); } catch (e) { /* fine outside LINE too */ }
  try { ME = await liff.getProfile(); } catch (e) { ME = null; }

  TRIP_ID = new URLSearchParams(location.search).get('tripId');
  if (!TRIP_ID) {
    pageEl.className = 'error';
    pageEl.textContent = '網址缺少 tripId 參數';
    return;
  }

  try {
    await loadTrip();
    pageEl.className = '';
  } catch (e) {
    pageEl.className = 'error';
    pageEl.textContent = e.status === 404 ? '找不到這趟旅程' : '載入失敗，請稍後再試';
  }
}
main();
</script>
</body>
</html>
"""


def render(liff_id: str) -> str:
    return _TEMPLATE.replace("__LIFF_ID__", liff_id)
