/* ── PodScribe shared utilities ─────────────────────────────────────────── */

// On a 403 from a mutating call the user is a read-only guest — surface it once.
function _guard403(r) {
  if (r.status === 403) { try { toast('Nur für Eigentümer — bitte anmelden.', 'error'); } catch(_){} }
}

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) { const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) { _guard403(r); const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async patch(path, body={}) {
    const r = await fetch(path, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) { _guard403(r); const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async put(path, body) {
    const r = await fetch(path, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) { _guard403(r); const e = await r.json().catch(()=>({detail:r.statusText})); throw new Error(e.detail||r.statusText); }
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method:'DELETE' });
    if (!r.ok) { _guard403(r); throw new Error(r.statusText); }
    return r.json();
  }
};

// ── Role gate (owner vs read-only guest) ───────────────────────────────────
// window.ME is populated once from /api/me; body.role-guest drives CSS hiding
// of [data-owner-only] controls. Open-by-default → role is "owner" for everyone.
window.ME = { role: 'owner', owner_configured: false, guest_rag_enabled: false };
async function initRole() {
  try {
    window.ME = await API.get('/api/me');
  } catch (_) { /* keep optimistic default */ }
  document.body.classList.toggle('role-guest', window.ME.role !== 'owner');
}
function isOwner() { return window.ME && window.ME.role === 'owner'; }
async function logout() {
  try { await fetch('/api/logout', { method:'POST' }); } catch(_){}
  location.href = '/';
}
document.addEventListener('DOMContentLoaded', initRole);

// Deterministic placeholder avatar (offline, no external calls). Newsletters get
// a mail badge so they're recognizable; other feeds without artwork get initials.
function avatarFor(name, opts = {}) {
  const s = String(name || '?').trim();
  let hash = 0;
  for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) >>> 0;
  const hue = hash % 360;
  const initials = (s.split(/\s+/).slice(0, 2).map(w => w[0] || '').join('') || '?').toUpperCase();
  const badge = opts.newsletter ? `<span class="gen-avatar-badge">${icon('mail', { size: 13 })}</span>` : '';
  return `<div class="gen-avatar" style="--av-bg:hsl(${hue} 42% 42%)">`
       + `<span class="gen-avatar-initials">${escHtml(initials)}</span>${badge}</div>`;
}

// Visual for a podcast/feed tile: newsletter → mail avatar; artwork → img; else initials.
function tileVisual(p) {
  if (p.feed_type === 'newsletter') return avatarFor(p.title, { newsletter: true });
  if (p.artwork_url) return `<img src="${escHtml(p.artwork_url)}" alt="${escHtml(p.title)}" loading="lazy" onerror="podcastImgFallback(this, ${JSON.stringify(escHtml(p.title))})">`;
  return avatarFor(p.title);
}

// Image load failure → swap to a deterministic initials avatar.
function podcastImgFallback(img, title) {
  if (img && img.parentNode) img.parentNode.innerHTML = avatarFor(title || img.alt || '?');
}

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

function confirmModal(title, body, confirmLabel = 'Löschen') {
  return new Promise(resolve => {
    const ov = document.createElement('div');
    ov.className = 'modal-overlay';
    ov.innerHTML = `
      <div class="modal">
        <div class="modal-title">${escHtml(title)}</div>
        <p style="font-size:.875rem;color:var(--text-muted);margin:.5rem 0 1.25rem">${escHtml(body)}</p>
        <div class="modal-actions">
          <button class="btn btn-secondary" id="cm-cancel">Abbrechen</button>
          <button class="btn btn-danger" id="cm-confirm">${escHtml(confirmLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const close = (val) => { ov.remove(); resolve(val); };
    ov.querySelector('#cm-cancel').onclick = () => close(false);
    ov.querySelector('#cm-confirm').onclick = () => close(true);
    ov.addEventListener('click', e => { if (e.target === ov) close(false); });
  });
}

/* Reliable download that works in the installed PWA (the service worker no longer
   intercepts /export, but fetching as a blob + programmatic click is the most
   robust path across mobile browsers). */
async function downloadFile(url, filename) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error('Download fehlgeschlagen');
    const blob = await r.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objUrl; a.download = filename || 'download';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
  } catch (e) { toast(e.message || 'Download fehlgeschlagen', 'error'); }
}

/* Bottom-sheet (mobile) / centered dialog (desktop).
   items: [{ icon, label, onClick, danger }] */
function openSheet(title, items) {
  const ov = document.createElement('div');
  ov.className = 'sheet-overlay';
  const rows = items.map((it, i) => `
    <button class="sheet-item${it.danger ? ' danger' : ''}" data-i="${i}">
      ${it.icon ? icon(it.icon, { size: 20 }) : ''}<span>${escHtml(it.label)}</span>
    </button>`).join('');
  ov.innerHTML = `<div class="sheet" role="menu">
    ${title ? `<div class="sheet-title">${escHtml(title)}</div>` : ''}${rows}</div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.addEventListener('click', e => { if (e.target === ov) close(); });
  ov.querySelectorAll('.sheet-item').forEach(btn => {
    btn.onclick = () => { close(); const it = items[+btn.dataset.i]; it.onClick && it.onClick(); };
  });
  return close;
}

/* Small context menu anchored to a trigger element.
   items: [{ icon, label, onClick, danger }] */
function openMenu(anchorEl, items) {
  document.querySelectorAll('.menu-popover').forEach(m => m.remove());
  const menu = document.createElement('div');
  menu.className = 'menu-popover';
  menu.innerHTML = items.map((it, i) => `
    <button class="${it.danger ? 'danger' : ''}" data-i="${i}">
      ${it.icon ? icon(it.icon, { size: 18 }) : ''}<span>${escHtml(it.label)}</span>
    </button>`).join('');
  document.body.appendChild(menu);
  const rect = anchorEl.getBoundingClientRect();
  const mw = menu.offsetWidth;
  let left = rect.right + window.scrollX - mw;
  if (left < 8) left = 8;
  menu.style.top = (rect.bottom + window.scrollY + 4) + 'px';
  menu.style.left = left + 'px';
  const close = () => { menu.remove(); document.removeEventListener('click', onDoc, true); };
  const onDoc = (e) => { if (!menu.contains(e.target) && e.target !== anchorEl) close(); };
  setTimeout(() => document.addEventListener('click', onDoc, true), 0);
  menu.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => { close(); const it = items[+btn.dataset.i]; it.onClick && it.onClick(); };
  });
  return close;
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

// Nav markup (injected into each page) — topbar (desktop) + bottom-nav (mobile)
function renderNav(activePath) {
  const isLib = activePath === '/' || activePath.startsWith('/podcast') || activePath.startsWith('/episode');
  const isTags = activePath.startsWith('/tags');
  return `
    <div id="read-progress"></div>
    <nav class="topbar">
      <a class="topbar-logo" href="/">Pod<span>Scribe</span></a>
      <a href="/settings" class="queue-indicator" style="font-size:.78rem;color:var(--text-muted);margin-left:.5rem;display:none;text-decoration:none" id="queue-indicator" title="Warteschlange ansehen"></a>
      <div class="search-bar" style="flex:1;max-width:280px;margin-left:1rem">
        <input type="text" id="global-search" placeholder="Transkripte durchsuchen…" autocomplete="off">
      </div>
      <nav class="topbar-nav">
        <a href="/" ${isLib?'class="active"':''}>${icon('library')} <span>Bibliothek</span></a>
        <a href="/?add=1">${icon('plus')} <span>Abonnieren</span></a>
        <a href="/search" ${activePath==='/search'?'class="active"':''}>${icon('sparkles')} <span>Fragen</span></a>
        <a href="/radar" ${activePath==='/radar'?'class="active"':''}>${icon('radar')} <span>Radar</span></a>
        <a href="/tags" ${isTags?'class="active"':''}>${icon('tag')} <span>Tags</span></a>
        <a href="/digests" ${activePath==='/digests'?'class="active"':''}>${icon('newspaper')} <span>Redaktion</span></a>
        <a href="/settings" data-owner-only ${activePath==='/settings'?'class="active"':''}>${icon('settings')} <span>Settings</span></a>
        <a href="/login" data-guest-only>${icon('lock')} <span>Anmelden</span></a>
        <button class="btn btn-ghost theme-btn" onclick="toggleTheme()" title="Design wechseln">${icon('sun')}</button>
      </nav>
    </nav>
    <nav class="bottom-nav">
      <a href="/" ${isLib?'class="active"':''}><span class="bn-icon">${icon('library', {size:22})}</span>Bibliothek</a>
      <a href="/?add=1" data-owner-only><span class="bn-icon">${icon('plus', {size:22})}</span>Abonnieren</a>
      <a href="/search" ${activePath==='/search'?'class="active"':''}><span class="bn-icon">${icon('sparkles', {size:22})}</span>Fragen</a>
      <a href="/digests" ${activePath==='/digests'?'class="active"':''}><span class="bn-icon">${icon('newspaper', {size:22})}</span>Redaktion</a>
      <a href="/settings" data-owner-only ${activePath==='/settings'?'class="active"':''}><span class="bn-icon">${icon('settings', {size:22})}</span>Mehr</a>
      <a href="/login" data-guest-only><span class="bn-icon">${icon('lock', {size:22})}</span>Anmelden</a>
    </nav>`;
}

// ── Service worker (PWA + offline) ─────────────────────────────────────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(()=>{});
  });
}

// ── Reading progress bar ───────────────────────────────────────────────────
function initReadProgress() {
  const bar = document.getElementById('read-progress');
  if (!bar) return;
  const update = () => {
    const h = document.documentElement.scrollHeight - window.innerHeight;
    bar.style.width = h > 0 ? `${(window.scrollY / h) * 100}%` : '0%';
  };
  window.addEventListener('scroll', update, { passive: true });
  update();
}

// ── Skeleton helpers ───────────────────────────────────────────────────────
function skeletonCards(n=6) {
  return Array.from({length:n}, () => `
    <div class="podcast-card">
      <div class="skeleton skel-card"></div>
      <div class="podcast-card-body">
        <div class="skeleton skel-line med"></div>
        <div class="skeleton skel-line short"></div>
      </div>
    </div>`).join('');
}
function skeletonRows(n=5) {
  return Array.from({length:n}, () => `
    <div class="episode-item" style="cursor:default">
      <div style="flex:1">
        <div class="skeleton skel-line med"></div>
        <div class="skeleton skel-line short"></div>
      </div>
    </div>`).join('');
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
        const results = await API.get(`/api/search?q=${encodeURIComponent(q)}&limit=10`);
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
    // Anchor to the input's left edge and grow rightward, so the panel can be much
    // wider than the narrow search box without spilling off the right of the screen.
    dd.style.cssText = 'position:absolute;top:calc(100% + 4px);left:0;right:auto;width:min(560px,92vw);background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-sm);z-index:200;max-height:70vh;overflow-y:auto;box-shadow:0 4px 16px rgba(0,0,0,.25);';
    anchor.parentNode.style.position = 'relative';
    anchor.parentNode.appendChild(dd);
  }
  if (!results.length) { dd.innerHTML = '<div style="padding:.75rem;color:var(--text-muted);font-size:.85rem">Keine Treffer</div>'; return; }
  dd.innerHTML = results.map(r => {
    // tags_csv format: "label|id,label|id"
    const tags = (r.tags_csv || '').split(',').filter(Boolean).slice(0, 3)
      .map(t => { const [label, id] = t.split('|'); return `<a href="/tags/${id||''}" onclick="hideSearchResults()" class="tag" style="font-size:.65rem">${escHtml(label||t)}</a>`; }).join('');
    return `
    <a href="/episode/${r.id}" style="display:block;padding:.75rem .875rem;border-bottom:1px solid var(--border);color:var(--text);text-decoration:none;" onclick="hideSearchResults()">
      <div style="font-size:.85rem;font-weight:500">${escHtml(r.title)}</div>
      <div style="font-size:.72rem;color:var(--text-muted);margin:.125rem 0">${escHtml(r.podcast_title||'')}${r.pub_date ? ' · ' + fmtDate(r.pub_date) : ''}</div>
      ${r.snippet ? `<div style="font-size:.78rem;color:var(--text-muted);margin-top:.25rem;line-height:1.5">${r.snippet}</div>` : ''}
      ${tags ? `<div style="margin-top:.375rem;display:flex;flex-wrap:wrap;gap:.25rem">${tags}</div>` : ''}
    </a>`;
  }).join('');
}

function hideSearchResults() {
  const dd = document.getElementById('search-dropdown');
  if (dd) dd.remove();
}

// ── Audio Player with synced transcript ────────────────────────────────────
const AudioPlayer = {
  audio: null, el: null, segTimes: [], curIdx: -1, autoScroll: true, speeds: [1, 1.25, 1.5, 1.75, 2], speedIdx: 0,

  mount(episodeId, title, segments) {
    this.segTimes = segments.map(s => timeToSecGlobal(s.time));
    document.body.classList.add('has-player');

    const player = document.createElement('div');
    player.className = 'audio-player visible';
    player.innerHTML = `
      <div class="ap-row">
        <button class="ap-btn small" id="ap-back" title="15s zurück">«15</button>
        <button class="ap-btn" id="ap-play" title="Abspielen">▶</button>
        <button class="ap-btn small" id="ap-fwd" title="15s vor">15»</button>
        <span class="ap-time" id="ap-cur">0:00</span>
        <div class="ap-scrubber" id="ap-scrub">
          <div class="ap-scrubber-fill" id="ap-fill"></div>
          <div class="ap-scrubber-thumb" id="ap-thumb"></div>
        </div>
        <span class="ap-time" id="ap-dur">--:--</span>
        <button class="ap-speed" id="ap-speed">1×</button>
      </div>
      <div class="ap-row">
        <span class="ap-title">🎧 ${escHtml(title)}</span>
        <label style="display:flex;align-items:center;gap:.35rem;font-size:.72rem;color:var(--text-muted);cursor:pointer;white-space:nowrap">
          <input type="checkbox" id="ap-autoscroll" checked style="width:auto"> Auto-Scroll
        </label>
      </div>`;
    document.body.appendChild(player);
    this.el = player;

    this.audio = new Audio(`/api/episodes/${episodeId}/audio`);
    this.audio.preload = 'metadata';

    const playBtn = player.querySelector('#ap-play');
    const fill = player.querySelector('#ap-fill');
    const thumb = player.querySelector('#ap-thumb');
    const curEl = player.querySelector('#ap-cur');
    const durEl = player.querySelector('#ap-dur');
    const scrub = player.querySelector('#ap-scrub');

    playBtn.onclick = () => this.toggle();
    player.querySelector('#ap-back').onclick = () => { this.audio.currentTime = Math.max(0, this.audio.currentTime - 15); };
    player.querySelector('#ap-fwd').onclick = () => { this.audio.currentTime += 15; };
    player.querySelector('#ap-speed').onclick = (e) => {
      this.speedIdx = (this.speedIdx + 1) % this.speeds.length;
      this.audio.playbackRate = this.speeds[this.speedIdx];
      e.target.textContent = this.speeds[this.speedIdx] + '×';
    };
    player.querySelector('#ap-autoscroll').onchange = (e) => { this.autoScroll = e.target.checked; };

    this.audio.addEventListener('play', () => playBtn.textContent = '⏸');
    this.audio.addEventListener('pause', () => playBtn.textContent = '▶');
    this.audio.addEventListener('loadedmetadata', () => durEl.textContent = fmtClock(this.audio.duration));
    this.audio.addEventListener('error', () => { toast('Audio konnte nicht geladen werden', 'error'); });
    this.audio.addEventListener('timeupdate', () => {
      const t = this.audio.currentTime, d = this.audio.duration || 0;
      curEl.textContent = fmtClock(t);
      const pct = d ? (t / d) * 100 : 0;
      fill.style.width = pct + '%';
      thumb.style.left = pct + '%';
      this.syncSegment(t);
    });

    let seeking = false;
    const seek = (clientX) => {
      const r = scrub.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
      if (this.audio.duration) this.audio.currentTime = ratio * this.audio.duration;
    };
    scrub.addEventListener('pointerdown', e => { seeking = true; seek(e.clientX); scrub.setPointerCapture(e.pointerId); });
    scrub.addEventListener('pointermove', e => { if (seeking) seek(e.clientX); });
    scrub.addEventListener('pointerup', () => seeking = false);

    // Clicking a transcript segment seeks the audio
    document.querySelectorAll('.segment').forEach((seg, idx) => {
      seg.addEventListener('click', (e) => {
        if (e.target.closest('a')) return;
        const time = this.segTimes[idx];
        if (time != null) { this.audio.currentTime = time; if (this.audio.paused) this.audio.play(); }
      });
    });
  },

  toggle() { this.audio.paused ? this.audio.play() : this.audio.pause(); },

  syncSegment(t) {
    // find last segment whose start <= t
    let idx = -1;
    for (let i = 0; i < this.segTimes.length; i++) { if (this.segTimes[i] <= t + 0.3) idx = i; else break; }
    if (idx === this.curIdx) return;
    if (this.curIdx >= 0) document.getElementById(`seg-${this.curIdx}`)?.classList.remove('playing');
    this.curIdx = idx;
    if (idx >= 0) {
      const el = document.getElementById(`seg-${idx}`);
      if (el) {
        el.classList.add('playing');
        if (this.autoScroll) {
          const rect = el.getBoundingClientRect();
          if (rect.top < 120 || rect.bottom > window.innerHeight - 160) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        }
      }
    }
  },

  destroy() {
    if (this.audio) { this.audio.pause(); this.audio.src = ''; }
    this.el?.remove();
    document.body.classList.remove('has-player');
    this.curIdx = -1;
  }
};

function timeToSecGlobal(t) {
  if (!t) return 0;
  const p = String(t).split(':').map(Number);
  if (p.length === 3) return p[0]*3600 + p[1]*60 + p[2];
  if (p.length === 2) return p[0]*60 + p[1];
  return p[0] || 0;
}
function fmtClock(s) {
  if (!s || isNaN(s)) return '0:00';
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${m}:${String(sec).padStart(2,'0')}`;
}

// ── Theme management (CR 8) ─────────────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem('ps-theme');
  const preferred = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  _applyTheme(saved || preferred);
}

function _applyTheme(theme) {
  if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.innerHTML = theme === 'light' ? icon('moon') : icon('sun');
    btn.title = theme === 'light' ? 'Dunkles Design aktivieren' : 'Helles Design aktivieren';
  });
}

function toggleTheme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  const next = isLight ? 'dark' : 'light';
  localStorage.setItem('ps-theme', next);
  _applyTheme(next);
}

// Call early so there's no flash of wrong theme
initTheme();

// ── Share content (CR 13) ────────────────────────────────────────────────────
async function shareContent(title, text, url) {
  const fullUrl = url ? (location.origin + url) : location.href;
  if (navigator.share) {
    try { await navigator.share({ title, text: text || title, url: fullUrl }); return; } catch {}
  }
  await copyText(fullUrl, '🔗 Link kopiert');
}

async function pollQueue() {
  if (!isOwner()) return;  // /api/queue is owner-only; guests skip silently
  try {
    const q = await API.get('/api/queue');
    const active = q.filter(e => ['queued','downloading','transcribing'].includes(e.status));
    const errors = q.filter(e => e.status === 'error');
    const ind = document.getElementById('queue-indicator');
    if (ind) {
      const parts = [];
      if (active.length) parts.push(`⏳ ${active.length}`);
      if (errors.length) parts.push(`<span style="color:var(--error)">❌ ${errors.length}</span>`);
      if (parts.length) {
        ind.style.display = 'inline';
        ind.innerHTML = parts.join(' · ');
      } else {
        ind.style.display = 'none';
      }
    }
  } catch {}
}
