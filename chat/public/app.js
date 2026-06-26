'use strict';
const $ = (s) => document.querySelector(s);
let ws = null, current = null, sessions = [];

async function api(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { showLogin(); throw new Error('unauthorized'); }
  return r;
}

// ---------- markdown (minimal, safe) ----------
function esc(s) { return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
function md(src) {
  const blocks = String(src).split(/```/);
  let out = '';
  blocks.forEach((b, i) => {
    if (i % 2) { // fenced code
      const nl = b.indexOf('\n');
      const code = nl >= 0 ? b.slice(nl + 1) : b;
      out += `<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`;
    } else {
      out += b.split(/\n{2,}/).filter((p) => p.trim()).map((p) => {
        let h = esc(p)
          .replace(/`([^`]+)`/g, '<code>$1</code>')
          .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
          .replace(/\n/g, '<br>');
        return `<p>${h}</p>`;
      }).join('');
    }
  });
  return out || '<p></p>';
}

// ---------- event rendering ----------
function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function toolInputSummary(input) {
  if (!input) return '';
  if (input.command) return esc(String(input.command).slice(0, 120));
  if (input.file_path) return esc(input.file_path);
  if (input.path) return esc(input.path);
  if (input.pattern) return esc(input.pattern);
  if (input.prompt) return esc(String(input.prompt).slice(0, 120));
  return esc(JSON.stringify(input).slice(0, 120));
}
function renderEvent(ev) {
  switch (ev.kind) {
    case 'user': {
      const m = el('div', 'msg user'); m.append(el('div', 'role', 'you'), el('div', 'body', md(ev.text))); return m;
    }
    case 'text': {
      const m = el('div', 'msg assistant'); m.append(el('div', 'role', 'claude'), el('div', 'body', md(ev.text))); return m;
    }
    case 'thinking': {
      const m = el('div', 'msg assistant'); m.append(el('div', 'body thinking', md(ev.text))); return m;
    }
    case 'tool_use': {
      const d = el('details', 'tool');
      d.append(el('summary', null, `🔧 <b>${esc(ev.name)}</b> &nbsp; ${toolInputSummary(ev.input)}`));
      d.append(el('pre', null, esc(JSON.stringify(ev.input, null, 2))));
      const w = el('div', 'msg assistant'); w.append(d); return w;
    }
    case 'tool_result': {
      const d = el('details', 'tool' + (ev.isError ? ' err' : ''));
      d.append(el('summary', null, `${ev.isError ? '⚠️' : '↳'} result`));
      d.append(el('pre', null, esc(ev.content || '')));
      const w = el('div', 'msg assistant'); w.append(d); return w;
    }
    case 'result': {
      if (!ev.result) return null; // final text already streamed as 'text'
      return null;
    }
    case 'error': {
      const m = el('div', 'msg assistant'); m.append(el('div', 'body evt-err', esc(ev.text))); return m;
    }
    default: return null;
  }
}
function appendEvent(ev) {
  const node = renderEvent(ev);
  if (!node) return;
  const box = $('#messages');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  box.append(node);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

// ---------- sessions ----------
function sessionById(id) { return sessions.find((s) => s.id === id); }
function renderSessionList() {
  const ul = $('#session-list'); ul.innerHTML = '';
  for (const s of sessions) {
    const li = el('li', s.id === current ? 'active' : '');
    li.append(el('span', 'dot' + (s.busy ? ' busy' : '')));
    li.append(el('span', 's-title', esc(s.title || 'Chat')));
    const del = el('button', 's-del', '✕'); del.title = 'Delete';
    del.onclick = async (e) => { e.stopPropagation(); if (confirm('Delete this chat?')) { await api('DELETE', `/api/sessions/${s.id}`); if (current === s.id) { current = null; clearMain(); } } };
    li.append(del);
    li.onclick = () => selectSession(s.id);
    ul.append(li);
  }
  updateComposer();
}
function updateComposer() {
  const s = current && sessionById(current);
  const busy = !!(s && s.busy);
  $('#input').disabled = !current || busy;
  $('#send').disabled = !current || busy;
  $('#status').textContent = busy ? '● agent working…' : '';
  if (s) { $('#title').textContent = s.title || 'Chat'; $('#cwd').textContent = s.cwd || ''; }
}
function clearMain() { $('#messages').innerHTML = '<div class="empty">Select or create a chat.</div>'; $('#title').textContent = 'No session'; $('#cwd').textContent = ''; updateComposer(); }

async function selectSession(id) {
  current = id;
  const r = await api('GET', `/api/sessions/${id}`); const data = await r.json();
  $('#messages').innerHTML = '';
  if (!data.transcript.length) $('#messages').append(el('div', 'empty', 'Send the first message to start this Claude session.'));
  for (const ev of data.transcript) appendEvent(ev);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  renderSessionList();
}

// ---------- websocket ----------
function setConn(up) { $('#conn').classList.toggle('up', up); $('#conn-txt').textContent = up ? 'live' : 'reconnecting…'; }
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connectWS, 1500); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'sessions') { sessions = msg.sessions; renderSessionList(); updateComposer(); }
    else if (msg.type === 'event' && msg.sessionId === current) {
      if (msg.event.kind === 'turn_end') { const em = $('#messages .empty'); if (em) em.remove(); }
      else appendEvent(msg.event);
    }
  };
}

// ---------- composer ----------
async function sendMessage() {
  const ta = $('#input'); const text = ta.value.trim();
  if (!text || !current) return;
  ta.value = ''; ta.style.height = 'auto';
  const em = $('#messages .empty'); if (em) em.remove();
  const r = await api('POST', `/api/sessions/${current}/message`, { text });
  if (r.status === 409) { alert('A turn is already running in this chat.'); }
}

// ---------- new chat modal ----------
function openNewChat() {
  const node = document.importNode($('#new-chat-tpl').content, true);
  document.body.append(node);
  const back = document.body.lastElementChild;
  back.querySelector('[data-act=cancel]').onclick = () => back.remove();
  back.querySelector('[data-act=create]').onclick = async () => {
    const get = (n) => back.querySelector(`[name=${n}]`).value.trim();
    const r = await api('POST', '/api/sessions', { title: get('title') || undefined, cwd: get('cwd') || undefined, model: get('model') || undefined });
    const meta = await r.json(); back.remove(); current = meta.id; await selectSession(meta.id);
  };
}

// ---------- login ----------
function showLogin() { $('#login').classList.remove('hidden'); $('#app').classList.add('hidden'); }
function showApp() { $('#login').classList.add('hidden'); $('#app').classList.remove('hidden'); }

async function init() {
  $('#login-form').onsubmit = async (e) => {
    e.preventDefault();
    const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: $('#token').value }) });
    if (r.ok) { start(); } else { $('#login-err').textContent = 'Wrong token.'; }
  };
  $('#new-chat').onclick = openNewChat;
  $('#composer').onsubmit = (e) => { e.preventDefault(); sendMessage(); };
  const ta = $('#input');
  ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'; });
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });

  const me = await (await fetch('/api/me')).json();
  if (me.note && me.env !== 'prod') {
    const b = $('#env-banner');
    b.textContent = (me.env ? me.env.toUpperCase() + ' · ' : '') + me.note;
    b.classList.remove('hidden');
    document.body.classList.add('has-banner');
  }
  if (me.authed) start(); else showLogin();
}
async function start() {
  showApp(); clearMain();
  const r = await api('GET', '/api/sessions'); sessions = (await r.json()).sessions;
  renderSessionList();
  connectWS();
}
init();
