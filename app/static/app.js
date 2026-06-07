/* ── PodScribe shared utilities ─────────────────────────────────────────── */

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) { const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) { const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async patch(path, body={}) {
    const r = await fetch(path, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) { const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async put(path, body) {
    const r = await fetch(path, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) { const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method:'DELETE' });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }
};

function toast(msg, type='info') {
  const c = document.getElementById('toast-container') || (() => {
    const el = document.createElement('div');
    el.id = 'toast-container';
    document.body.appendChild(el);
    return el;
  })();
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

async function copyText(text, label='Kopiert!') {
  try {
    await navigator.clipboard.writeText(text);
    toast(label, 'success');
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    toast(label, 'success');
  }
}

function statusBadge(status) {
  const labels = { done:'Fertig', error:'Fehler', pending:'Ausstehend', queued:'Warteschlange', downloading:'Lädt…', transcribing:'Transkribiert…' };
  return `<span class="status status-${status}">${labels[status]||status}</span>`;
}

function fmtDate(str) {
  if (!str) return '';
  try { return new Date(str).toLocaleDateString('de-DE', { day:'2-digit', month:'short', year:'numeric' }); }
  catch { return str; }
}

function fmtDuration(sec) {
  if (!sec) return '';
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function pageId() {
  return parseInt(location.pathname.split('/').pop()) || null;
}

function setActive(href) {
  document.querySelectorAll('.topbar-nav a').forEach(a => {
    a.classList.toggle('active', a.getAttribute('href') === href);
  });
}

// Nav markup (injected into each page)
function renderNav(activePath) {
  return `
    <nav class="topbar">
      <a class="topbar-logo" href="/">Pod<span>Scribe</span></a>
      <div class="topbar-logo" style="font-size:.8rem;color:var(--text-muted);margin-left:.5rem;display:none" id="queue-indicator"></div>
      <div class="search-bar" style="flex:1;max-width:280px;margin-left:1rem">
        <input type="text" id="global-search" placeholder="Suchen…" autocomplete="off">
      </div>
      <nav class="topbar-nav">
        <a href="/" ${activePath==='/'?'class="active"':''}>📚 <span>Bibliothek</span></a>
        <a href="/digests" ${activePath==='/digests'?'class="active"':''}>📰 <span>Zeitung</span></a>
        <a href="/settings" ${activePath==='/settings'?'class="active"':''}>⚙️ <span>Settings</span></a>
      </nav>
    </nav>`;
}

function initGlobalSearch() {
  const inp = document.getElementById('global-search');
  if (!inp) return;
  let timer;
  inp.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = inp.value.trim();
      if (q.length < 2) { hideSearchResults(); return; }
      try {
        const results = await API.get(`/api/search?q=${encodeURIComponent(q)}&limit=8`);
        showSearchResults(results, inp);
      } catch {}
    }, 350);
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.search-bar') && !e.target.closest('#search-dropdown')) hideSearchResults();
  });
}

function showSearchResults(results, anchor) {
  let dd = document.getElementById('search-dropdown');
  if (!dd) {
    dd = document.createElement('div');
    dd.id = 'search-dropdown';
    dd.style.cssText = 'position:absolute;top:calc(100% + 4px);left:0;right:0;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);z-index:200;max-height:320px;overflow-y:auto;';
    anchor.parentNode.style.position = 'relative';
    anchor.parentNode.appendChild(dd);
  }
  if (!results.length) { dd.innerHTML = '<div style="padding:.75rem;color:var(--text-muted);font-size:.85rem">Keine Treffer</div>'; return; }
  dd.innerHTML = results.map(r => `
    <a href="/episode/${r.id}" style="display:block;padding:.625rem .875rem;border-bottom:1px solid var(--border);color:var(--text);" onclick="hideSearchResults()">
      <div style="font-size:.85rem;font-weight:500">${escHtml(r.title)}</div>
      <div style="font-size:.75rem;color:var(--text-muted)">${escHtml(r.podcast_title||'')} · ${r.snippet||''}</div>
    </a>`).join('');
}

function hideSearchResults() {
  const dd = document.getElementById('search-dropdown');
  if (dd) dd.remove();
}

async function pollQueue() {
  try {
    const q = await API.get('/api/queue');
    const active = q.filter(e => ['queued','downloading','transcribing'].includes(e.status));
    const ind = document.getElementById('queue-indicator');
    if (ind) {
      if (active.length) {
        ind.style.display = 'block';
        ind.textContent = `⏳ ${active.length}`;
      } else {
        ind.style.display = 'none';
      }
    }
  } catch {}
}
