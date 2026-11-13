// scene-cut：素材池 + 项目 + 预览 + 镜头编辑（无构建）
'use strict';

const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

const S = {
  pool: [], projects: [],
  proj: null, analysis: null, qs: '',      // qs = 当前媒体的查询串（project=… 或 path=…）
  fps: 30, total: 0, frame: 0, playing: false,
  view: { start: 0, end: 0 },
  clips: [], checked: new Set(), cur: -1,
  editPos: 0, editLen: 0,
  shots: [], shotMap: new Map(),
  strips: new Map(), stripTimer: null,
  cache: new Map(), pending: new Set(),
  selPath: null, selName: '', collapsed: new Set(),
  poolChecked: new Set(), projChecked: new Set(),
  jobId: null, runningJobSource: null,
};

const video = $('video');
const canvas = $('frameCanvas');
const ctx = canvas.getContext('2d');
const timeline = $('timeline');
const tctx = timeline.getContext('2d');
const POOL_COLLAPSE_LIMIT = 400;

const KIND_COLOR = { cut: '#4ade80', dissolve: '#60a5fa', fade: '#fbbf24', flash: '#f87171', video_start: '#94a3b8' };
const TYPE_LABEL = { video_start: '起始', cut: 'cut', dissolve: '叠化', fade: '淡入淡出', flash: '闪光', unknown: '?' };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).error || ''; } catch { /* ignore */ }
    throw new Error(`${path} → ${r.status}${detail ? ' · ' + detail : ''}`);
  }
  return r.json();
}
const post = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

function tcOf(frame) {
  const ms = Math.round((frame / S.fps) * 1000);
  const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
}
const confColor = (c) => (c >= 0.75 ? '#4ade80' : c >= 0.45 ? '#fbbf24' : '#6b7280');
const setMsg = (t) => { $('msg').textContent = t; };

// ---------------- 素材池 ----------------
async function loadPool() {
  const data = await api('/api/pool');
  S.pool = data.roots || [];
  renderPool();
  const files = countPoolFiles(S.pool);
  $('poolInfo').textContent = `${S.pool.length} 根 · ${files} 个文件`;
  updateBatchButtons();
}

function countPoolFiles(nodes) {
  let n = 0;
  for (const node of nodes) {
    if (node.kind === 'file') n += 1;
    else if (node.children) n += countPoolFiles(node.children);
  }
  return n;
}

function updateBatchButtons() {
  const a = $('btnBatchAnalyze');
  const e = $('btnBatchExport');
  a.disabled = S.poolChecked.size === 0;
  a.textContent = S.poolChecked.size ? `批量检测 ${S.poolChecked.size}` : '批量检测';
  e.disabled = S.projChecked.size === 0;
  e.textContent = S.projChecked.size ? `批量导出 ${S.projChecked.size}` : '批量导出';
}

function renderPool() {
  const box = $('poolTree');
  box.innerHTML = '';
  if (!S.pool.length) {
    box.innerHTML = '<div class="groupHead">把文件/目录拖进来，或粘贴路径导入</div>';
    return;
  }
  box.appendChild(renderNodes(S.pool, 0));
}

function projectFor(path) {
  return S.projects.find((p) => (p.source || '').toLowerCase() === path.toLowerCase());
}

function renderNodes(nodes, depth) {
  const frag = document.createDocumentFragment();
  for (const node of nodes) {
    const row = document.createElement('div');
    row.className = 'node' + (node.kind === 'dir' ? ' dir' : '');
    const pad = 6 + depth * 12;
    row.style.paddingLeft = `${pad}px`;
    const isDir = node.kind === 'dir';
    const key = node.path;
    const isOpen = isDir && !S.collapsed.has(key);
    const proj = isDir ? null : projectFor(node.path);
    const badge = isDir ? '' :
      proj ? `<span class="badge ${proj.status === 'failed' ? 'fail' : proj.has_analysis ? 'ok' : 'raw'}">${proj.has_analysis ? proj.shots + ' 镜' : '已建项目'}</span>`
           : '<span class="badge raw">未分析</span>';
    const acts = node.root
      ? '<span class="acts"><button data-act="rename" title="重命名">✎</button><button data-act="remove" title="从素材池移除">✕</button></span>'
      : '';
    const ck = isDir ? '' : `<input type="checkbox" class="ck" ${S.poolChecked.has(node.path) ? 'checked' : ''} />`;
    row.innerHTML =
      `<span class="tw">${isDir ? (isOpen ? '▾' : '▸') : ''}</span>` + ck +
      `<span class="nm" title="${node.path}">${node.name}</span>${badge}${acts}`;
    if (S.selPath === node.path) row.classList.add('active');
    const ckEl = row.querySelector('input.ck');
    if (ckEl) {
      ckEl.onclick = (e) => {
        e.stopPropagation();
        e.target.checked ? S.poolChecked.add(node.path) : S.poolChecked.delete(node.path);
        updateBatchButtons();
      };
    }

    row.onclick = (e) => {
      if (e.target.dataset && e.target.dataset.act) return;
      if (isDir) {
        S.collapsed.has(key) ? S.collapsed.delete(key) : S.collapsed.add(key);
        renderPool();
      } else {
        selectPoolFile(node);
      }
    };
    row.ondblclick = () => { if (!isDir) analyzeSource(node.path); };
    const actsEl = row.querySelector('.acts');
    if (actsEl) {
      actsEl.onclick = async (e) => {
        e.stopPropagation();
        const act = e.target.dataset.act;
        if (!act) return;
        if (act === 'rename') {
          const alias = prompt('重命名（仅显示名，不改磁盘）', node.name);
          if (alias) { await post('/api/pool/rename', { root: node.id, alias }); await loadPool(); }
        } else if (act === 'remove') {
          if (!confirm(`从素材池移除「${node.name}」？（不删除磁盘文件）`)) return;
          await post('/api/pool/remove', { root: node.id });
          await loadPool();
        }
      };
    }
    frag.appendChild(row);
    if (isDir && isOpen && node.children) {
      frag.appendChild(renderNodes(node.children, depth + 1));
    }
  }
  return frag;
}

function selectPoolFile(node) {
  S.selPath = node.path; S.selName = node.name;
  S.proj = null; S.qs = `path=${encodeURIComponent(node.path)}`;
  renderPool();
  openMedia();
}

// ---------------- 项目 ----------------
async function loadProjects() {
  S.projects = await api('/api/projects');
  renderProjects();
  $('projInfo').textContent = `${S.projects.length} 个`;
  updateBatchButtons();
}

function renderProjects() {
  const box = $('projList');
  box.innerHTML = '';
  if (!S.projects.length) {
    box.innerHTML = '<div class="groupHead">选中素材后点「检测」自动建项目</div>';
    return;
  }
  for (const p of S.projects) {
    const row = document.createElement('div');
    row.className = 'projRow' + (S.proj && S.proj.id === p.id ? ' active' : '');
    const statusCls = p.status === 'failed' ? 'fail' : p.has_analysis ? 'ok' : p.status === 'analyzing' ? 'run' : 'raw';
    let statusText;
    if (p.has_analysis) statusText = `${p.shots} 镜`;
    else if (p.status === 'analyzing') {
      statusText = S.runningJobSource === p.source ? '检测中' : '上次未完成';
    } else if (p.status === 'failed') statusText = '失败';
    else statusText = '空';
    row.innerHTML =
      `<input type="checkbox" class="ck" ${S.projChecked.has(p.id) ? 'checked' : ''} />` +
      `<span class="nm" title="${p.dir}">${p.name}</span>` +
      `<span class="badge ${statusCls}">${statusText}</span>` +
      `<span class="acts"><button data-act="rename" title="重命名">✎</button><button data-act="remove" title="删除项目文件夹">✕</button></span>`;
    const ck = row.querySelector('input.ck');
    ck.onclick = (e) => {
      e.stopPropagation();
      e.target.checked ? S.projChecked.add(p.id) : S.projChecked.delete(p.id);
      updateBatchButtons();
    };
    row.onclick = (e) => {
      if (e.target.dataset && e.target.dataset.act) return;
      openProject(p.id);
    };
    row.querySelector('.acts').onclick = async (e) => {
      e.stopPropagation();
      const act = e.target.dataset.act;
      if (!act) return;
      if (act === 'rename') {
        const name = prompt('项目重命名（会改文件夹名）', p.name);
        if (!name || name === p.name) return;
        await post('/api/projects/rename', { project: p.id, name });
        await loadProjects();
        setMsg('已重命名');
      } else if (act === 'remove') {
        if (!confirm(`删除项目「${p.name}」及其解释文件/导出片段？`)) return;
        await post('/api/projects/remove', { project: p.id });
        if (S.proj && S.proj.id === p.id) { S.proj = null; S.clips = []; renderList(); }
        await loadProjects();
        setMsg('已删除项目');
      }
    };
    box.appendChild(row);
  }
}

async function openProject(pid) {
  const project = S.projects.find((p) => p.id === pid);
  if (!project) return;
  S.proj = project;
  S.selPath = project.source;
  S.qs = `project=${encodeURIComponent(pid)}`;
  renderPool(); renderProjects();
  await openMedia();
}

// ---------------- 媒体加载 ----------------
async function openMedia() {
  S.analysis = await api(`/api/analysis?${S.qs}`);
  const p = S.analysis.probe || {};
  S.fps = p.fps || 30;
  S.total = p.nb_frames || Math.round((p.duration || 0) * S.fps) || 1;
  S.view = { start: 0, end: S.total };
  S.frame = 0; S.cur = -1; S.checked.clear();
  S.cache.clear(); S.pending.clear(); S.strips.clear();
  S.shots = S.analysis.shots || [];
  S.shotMap.clear();
  for (const s of S.shots) S.shotMap.set(s.start, s);
  S.clips = (S.analysis.clips || []).map((c, i) => ({ ...c, index: i }));
  S.editPos = S.analysis.edit_pos ?? 0;
  S.editLen = S.analysis.edit_len ?? 0;

  video.src = `/api/video?${S.qs}`;
  video.load(); video.pause();
  renderMeta();
  renderList();
  drawTimeline();
  updateStatus();
  showFrame(0);
  requestStrip();
  hideAB();
  if (S.clips.length) selectClip(0);
}

function renderMeta() {
  const p = S.analysis.probe || {};
  const name = S.proj ? S.proj.name : S.selName;
  $('cutInfo').textContent = `${name} · ${p.width || '?'}×${p.height || '?'} · ${(p.fps || 0).toFixed(3)}fps · ${(p.duration || 0).toFixed(1)}s`;
  $('shotCount').textContent = `${S.clips.length} 镜头`;
}

// ---------------- 镜头列表 / 编辑 ----------------
function shotForClip(c) {
  const exact = S.shotMap.get(c.start);
  if (exact) return exact;
  const idx = Array.isArray(c.from_shots) ? c.from_shots[0] : null;
  return Number.isInteger(idx) && S.shots[idx] ? S.shots[idx] : null;
}

function renderList() {
  const list = $('shotList');
  list.innerHTML = '';
  S.clips.forEach((c, i) => {
    const shot = shotForClip(c);
    const type = shot ? shot.cut_type : (i === 0 ? 'video_start' : 'unknown');
    const conf = shot && shot.confidence != null ? shot.confidence : null;
    const row = document.createElement('div');
    row.className = 'row' + (i === S.cur ? ' active' : '') + (S.checked.has(i) ? ' checked' : '');
    row.innerHTML =
      `<input type="checkbox" ${S.checked.has(i) ? 'checked' : ''} />` +
      `<span class="no">#${String(i + 1).padStart(3, '0')}</span>` +
      `<span class="tc">${tcOf(c.start)}</span>` +
      `<span class="dur">${((c.end - c.start) / S.fps).toFixed(2)}s</span>` +
      `<span class="type"><i class="dot" style="background:${KIND_COLOR[type] || '#6b7280'}"></i>${TYPE_LABEL[type] || type}</span>` +
      `<span class="conf" style="color:${conf == null ? '#6b7280' : confColor(conf)}">${conf == null ? '—' : conf.toFixed(2)}</span>`;
    row.querySelector('input').onclick = (e) => {
      e.stopPropagation();
      e.target.checked ? S.checked.add(i) : S.checked.delete(i);
      row.classList.toggle('checked', e.target.checked);
      updateEditButtons();
    };
    row.onclick = () => { selectClip(i); seek(c.start); };
    list.appendChild(row);
  });
  $('shotCount').textContent = `${S.clips.length} 镜头`;
  updateEditButtons();
}

function updateEditButtons() {
  $('btnMerge').disabled = S.checked.size < 2;
  $('btnMerge').textContent = S.checked.size >= 2 ? `合并 ${S.checked.size}` : '合并';
  $('btnUndo').disabled = S.editPos <= 0;
  $('btnRedo').disabled = S.editPos >= S.editLen;
  $('btnSplit').disabled = !canSplit() || !S.proj;
  $('selCount').textContent = S.checked.size ? `已选 ${S.checked.size}` : '';
}

function canSplit() {
  const f = S.frame;
  if (f <= 1 || f >= S.total - 1) return false;
  return S.clips.every((c) => Math.abs(c.start - f) > 1) && S.clips.every((c) => Math.abs(c.end - f) > 1);
}

function selectClip(i) {
  S.cur = i;
  const c = S.clips[i];
  renderList(); updateEditButtons();
  if (!c) { hideAB(); return; }
  const shot = shotForClip(c);
  setAB(i === 0 ? 0 : Math.max(0, c.start - 1), i === 0 ? Math.min(S.total - 1, c.end - 1) : c.start);
  $('timeInfo').textContent = tcOf(c.start);
  const row = document.querySelectorAll('.row')[i];
  if (row) row.scrollIntoView({ block: 'nearest' });
  return shot;
}

function setAB(a, b) {
  for (const [el, f] of [[$('abA'), a], [$('abB'), b]]) {
    el.parentElement.classList.remove('empty');
    el.src = `/api/frame?${S.qs}&frame=${f}&width=640&prefetch=0`;
  }
}
function hideAB() {
  for (const el of [$('abA'), $('abB')]) { el.removeAttribute('src'); el.parentElement.classList.add('empty'); }
}

async function doEdit(op, payload = {}) {
  if (!S.proj) { setMsg('先在素材池里选中素材并检测，才能编辑'); return; }
  try {
    const res = await post('/api/edit', { project: S.proj.id, op, ...payload });
    S.clips = res.clips.map((c, i) => ({ ...c, index: i }));
    S.editPos = res.history_pos; S.editLen = res.history_len;
    S.checked.clear();
    if (S.cur >= S.clips.length) S.cur = S.clips.length - 1;
    renderList(); drawTimeline();
    if (S.cur >= 0) selectClip(S.cur);
    setMsg(`已编辑 · 历史 ${S.editPos}/${S.editLen}`);
  } catch (e) { setMsg(`编辑失败：${e.message}`); }
}

const splitAtPlayhead = () => { if (S.proj && canSplit()) doEdit('split', { frame: S.frame }); };
const mergeSelected = () => { if (S.checked.size >= 2) doEdit('merge', { indices: [...S.checked] }); };
const undo = () => doEdit('undo');
const redo = () => doEdit('redo');

$('btnSplit').onclick = splitAtPlayhead;
$('btnMerge').onclick = mergeSelected;
$('btnUndo').onclick = undo;
$('btnRedo').onclick = redo;

// ---------------- 播放 / 定位 ----------------
function seek(frame) {
  S.frame = clamp(Math.round(frame), 0, S.total - 1);
  video.currentTime = (S.frame + 0.5) / S.fps;
  updateStatus();
  if (!S.playing) showFrame(S.frame);
}
function step(d) { pause(); seek(S.frame + d); }
function play() {
  S.playing = true; canvas.style.display = 'none';
  $('playOverlay').style.display = 'none';
  video.play().catch(() => {});
}
function pause() {
  S.playing = false; video.pause();
  canvas.style.display = 'block'; showFrame(S.frame);
}
const togglePlay = () => (S.playing ? pause() : play());

function updateStatus() {
  $('frameInfo').textContent = `frame ${S.frame} / ${S.total - 1}`;
  $('timeInfo').textContent = tcOf(S.frame);
  updateEditButtons();
  drawTimeline();
}

// ---------------- 取帧 ----------------
function drawImage(img) {
  const w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
  if (!w || !h) return;
  canvas.width = w; canvas.height = h;
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(img, 0, 0, w, h);
}
function showFrame(frame) {
  frame = clamp(frame, 0, S.total - 1);
  const hit = S.cache.get(frame);
  if (hit) { drawImage(hit); canvas.style.display = 'block'; return; }
  if (S.pending.size > 4) return;
  S.pending.add(frame);
  const img = new Image();
  img.onload = () => {
    S.pending.delete(frame);
    S.cache.set(frame, img);
    if (S.cache.size > 600) S.cache.delete(S.cache.keys().next().value);
    if (!S.playing && S.frame === frame) { canvas.style.display = 'block'; drawImage(img); }
  };
  img.onerror = () => S.pending.delete(frame);
  img.src = `/api/frame?${S.qs}&frame=${frame}`;
}

// ---------------- 时间轴 ----------------
const viewLen = () => Math.max(1, S.view.end - S.view.start);
const frameToX = (f) => ((f - S.view.start) / viewLen()) * (timeline.clientWidth || 800);
const xToFrame = (x) => S.view.start + (x / (timeline.clientWidth || 800)) * viewLen();

function requestStrip() {
  clearTimeout(S.stripTimer);
  S.stripTimer = setTimeout(() => {
    const key = `${Math.round(S.view.start)}_${Math.round(S.view.end)}`;
    if (S.strips.has(key)) return;
    const img = new Image();
    img.onload = () => { S.strips.set(key, img); if (S.strips.size > 12) S.strips.delete(S.strips.keys().next().value); drawTimeline(); };
    img.src = `/api/strip?${S.qs}&tiles=40&start=${(S.view.start / S.fps).toFixed(2)}&end=${(S.view.end / S.fps).toFixed(2)}`;
  }, 220);
}

function drawTimeline() {
  const dpr = window.devicePixelRatio || 1;
  const w = timeline.clientWidth || 800;
  timeline.width = w * dpr; timeline.height = 92 * dpr;
  tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  tctx.clearRect(0, 0, w, 92);
  const strip = S.strips.get(`${Math.round(S.view.start)}_${Math.round(S.view.end)}`);
  if (strip) tctx.drawImage(strip, 0, 10, w, 44);
  else { tctx.fillStyle = '#1b212a'; tctx.fillRect(0, 10, w, 44); }

  S.clips.slice(1).forEach((c) => {
    if (c.start < S.view.start || c.start > S.view.end) return;
    const shot = shotForClip(c);
    const conf = shot && shot.confidence != null ? shot.confidence : 0.5;
    const x = frameToX(c.start);
    tctx.strokeStyle = (shot && KIND_COLOR[shot.cut_type]) || '#94a3b8';
    tctx.globalAlpha = 0.3 + 0.7 * Math.min(1, conf / 0.9);
    tctx.lineWidth = 1;
    tctx.beginPath(); tctx.moveTo(x, 6); tctx.lineTo(x, 82); tctx.stroke();
  });
  tctx.globalAlpha = 1;

  const px = frameToX(S.frame);
  tctx.strokeStyle = '#fff'; tctx.lineWidth = 1.5;
  tctx.beginPath(); tctx.moveTo(px, 0); tctx.lineTo(px, 92); tctx.stroke();
  tctx.fillStyle = '#fff';
  tctx.beginPath(); tctx.moveTo(px - 4, 0); tctx.lineTo(px + 4, 0); tctx.lineTo(px, 7); tctx.closePath(); tctx.fill();

  tctx.fillStyle = '#8b98a5'; tctx.font = '10px Segoe UI';
  const span = viewLen() / S.fps;
  const step = Math.max(0.5, niceStep(span / 8));
  for (let t = Math.ceil((S.view.start / S.fps) / step) * step; t * S.fps <= S.view.end; t += step) {
    tctx.fillText(`${t.toFixed(step < 1 ? 1 : 0)}s`, Math.min(frameToX(t * S.fps) + 2, w - 26), 88);
  }
  $('viewInfo').textContent = `视图 ${tcOf(Math.round(S.view.start))} – ${tcOf(Math.round(S.view.end))} · ${span.toFixed(1)}s`;
}

function niceStep(v) {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}

function zoomAt(factor, anchorFrame) {
  const len = viewLen();
  const newLen = clamp(len * factor, 24, S.total);
  if (newLen === len) return;
  const anchor = anchorFrame == null ? S.frame : anchorFrame;
  const ratio = (anchor - S.view.start) / len;
  const start = clamp(anchor - ratio * newLen, 0, S.total - newLen);
  S.view = { start: Math.round(start), end: Math.round(start + newLen) };
  requestStrip(); drawTimeline();
}

let dragging = false;
const timelineSeek = (ev) => {
  const rect = timeline.getBoundingClientRect();
  seek(xToFrame(clamp(ev.clientX - rect.left, 0, rect.width)));
};
timeline.addEventListener('mousedown', (e) => { dragging = true; pause(); timelineSeek(e); });
window.addEventListener('mousemove', (e) => { if (dragging) timelineSeek(e); });
window.addEventListener('mouseup', () => { dragging = false; });
timeline.addEventListener('wheel', (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const rect = timeline.getBoundingClientRect();
  zoomAt(e.deltaY > 0 ? 1.35 : 1 / 1.35, xToFrame(clamp(e.clientX - rect.left, 0, rect.width)));
}, { passive: false });
$('zoomIn').onclick = () => zoomAt(1 / 1.6, S.frame);
$('zoomOut').onclick = () => zoomAt(1.6, S.frame);
$('zoomFit').onclick = () => { S.view = { start: 0, end: S.total }; requestStrip(); drawTimeline(); };

// ---------------- 导入 ----------------
async function importPaths(paths) {
  for (const p of paths) {
    if (!p) continue;
    try {
      const res = await post('/api/pool/import', { path: p });
      setMsg(`已导入：${res.root.name}`);
    } catch (e) { setMsg(`导入失败：${e.message}`); }
  }
  await loadPool();
}

$('btnImport').onclick = async () => {
  const raw = $('importPath').value.trim().replace(/^"|"$/g, '');
  if (!raw) return;
  await importPaths([raw]);
  $('importPath').value = '';
};

async function pick(mode) {
  setMsg('等待系统选择框…');
  try {
    const res = await post('/api/pick', { mode });
    if (res.error) { setMsg(res.error); return; }
    if (res.paths && res.paths.length) await importPaths(res.paths);
    else setMsg('已取消');
  } catch (e) { setMsg(`选择失败：${e.message}`); }
}
$('btnPickDir').onclick = () => pick('dir');
$('btnPickFile').onclick = () => pick('file');

const poolBox = $('poolTree');
for (const evt of ['dragenter', 'dragover']) {
  poolBox.addEventListener(evt, (e) => { e.preventDefault(); poolBox.classList.add('dragover'); });
}
poolBox.addEventListener('dragleave', () => poolBox.classList.remove('dragover'));
poolBox.addEventListener('drop', async (e) => {
  e.preventDefault();
  poolBox.classList.remove('dragover');
  const paths = [];
  if (e.dataTransfer.getData('text/plain')) {
    const t = e.dataTransfer.getData('text/plain').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    paths.push(...t);
  }
  for (const f of e.dataTransfer.files || []) if (f.path) paths.push(f.path);
  if (!paths.length) {
    setMsg('浏览器拿不到拖入项的绝对路径：请用「选目录 / 选文件」或粘贴路径（桌面壳里可直接拖）');
    return;
  }
  await importPaths(paths);
});

// ---------------- 侧栏 ----------------
$('sideToggle').onclick = () => { document.body.classList.add('side-collapsed'); setTimeout(drawTimeline, 180); };
$('sideRestore').onclick = () => { document.body.classList.remove('side-collapsed'); setTimeout(drawTimeline, 180); };

// ---------------- 检测 ----------------
const JOB_PHASE_LABEL = {
  queued: '排队', probe: '读取信息', t1: '统计检测', decode: '解码',
  t2: 'AI 检测', fuse: '融合边界', write: '写入结果', done: '完成',
};

function setJobBar(job) {
  const bar = $('jobBar');
  if (!bar) return;
  if (!job || job.status !== 'running') {
    bar.classList.add('hidden');
    S.runningJobSource = null;
    return;
  }
  bar.classList.remove('hidden');
  S.runningJobSource = job.source || S.runningJobSource;
  const pct = Math.round((job.progress || 0) * 100);
  const phase = JOB_PHASE_LABEL[job.phase] || job.phase || '检测';
  const note = job.message ? ` · ${job.message}` : '';
  const secs = ((job.elapsed_ms || 0) / 1000).toFixed(1);
  $('jobText').textContent = `${phase} ${pct}%${note} · ${secs}s`;
}

function setBusy(on) {
  const btn = $('btnAnalyze');
  if (btn) btn.disabled = !!on;
}

async function cancelCurrentJob() {
  if (!S.jobId) return;
  try {
    await post('/api/job/cancel', { id: S.jobId });
    setMsg('已请求取消，等待当前阶段结束…');
  } catch (e) { setMsg(`取消失败：${e.message}`); }
}
$('btnCancelJob').onclick = cancelCurrentJob;

async function trackJob(jobId, path) {
  S.jobId = jobId;
  setBusy(true);
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 500));
      const st = await api(`/api/job?id=${jobId}`);
      setJobBar(st);
      if (st.status === 'running') continue;
      if (st.status === 'failed') setMsg(`检测失败：${st.error || '未知错误'}`);
      else if (st.status === 'cancelled') setMsg('检测已取消');
      else setMsg(`完成：${st.shots} 镜头，${((st.elapsed_ms || 0) / 1000).toFixed(1)}s`);
      return st;
    }
  } finally {
    S.jobId = null;
    setBusy(false);
    setJobBar(null);
  }
}

async function analyzeSource(path) {
  if (!path) { setMsg('先在素材池里选中一个文件'); return; }
  if (S.jobId) { setMsg('已有检测在进行中，先取消或等它结束'); return; }
  setMsg('检测中…（首次会建项目文件夹）');
  try {
    const res = await post('/api/projects/analyze', { source: path, ai: true });
    if (res.reused) setMsg('这条素材已在检测中，接上现有进度…');
    await trackJob(res.job.id, path);
    await loadProjects();
    const proj = S.projects.find((p) => p.source.toLowerCase() === path.toLowerCase());
    if (proj) await openProject(proj.id);
  } catch (e) {
    setBusy(false);
    setMsg(`检测失败：${e.message}`);
  }
}
$('btnAnalyze').onclick = () => analyzeSource(S.selPath);

/** 刷新页面后重新挂接正在跑的检测（不再显示"永远的检测中"） */
async function resumeRunningJob() {
  try {
    const data = await api('/api/jobs');
    const job = (data.jobs || []).find((j) => (j.kind || 'analyze') === 'analyze');
    if (!job) return;
    setMsg(`接上正在进行的检测：${JOB_PHASE_LABEL[job.phase] || job.phase || ''}…`);
    await trackJob(job.id, job.source);
    await loadProjects();
    if (job.project) {
      const proj = S.projects.find((p) => p.id === job.project);
      if (proj) await openProject(proj.id);
    }
  } catch { /* 忽略：旧版本服务没有 /api/jobs */ }
}

async function initLogLevel() {
  try {
    const info = await api('/api/log/level');
    $('logLevel').value = info.level || 'info';
  } catch { /* ignore */ }
}
$('logLevel').onchange = async (e) => {
  try {
    const r = await post('/api/log/level', { level: e.target.value });
    setMsg(`控制台日志级别：${r.level}`);
  } catch (err) { setMsg(`切换日志级别失败：${err.message}`); }
};

// ---------------- 设置（数据目录 / 工作区 / 日志） ----------------
const S2 = { settings: null };
const DATA_SOURCE_LABEL = {
  env: '环境变量 / 启动参数', pointer: '已自定义（指针文件）', default: '默认位置', fallback: '回退位置',
};

function setSetMsg(t) { $('setMsg').textContent = t || ''; }

async function refreshSettings() {
  const s = await api('/api/settings');
  S2.settings = s;
  $('setWorkspace').value = s.workspace;
  $('setDataRoot').value = s.data_root;
  $('setDataInfo').textContent =
    `来源：${DATA_SOURCE_LABEL[s.data_root_source] || s.data_root_source} · 指针文件：${s.data_root_pointer}`;
  $('setAiFast').checked = !!s.ai_fast;
  $('setSampleLimit').value = s.t2_sample_limit;
  $('setLogLevel').value = s.log_level;
  $('logLevel').value = s.log_level;
  return s;
}

async function openSettings() {
  $('settingsModal').classList.remove('hidden');
  setSetMsg('');
  try { await refreshSettings(); } catch (e) { setSetMsg(`读取设置失败：${e.message}`); }
}
function closeSettings() { $('settingsModal').classList.add('hidden'); }
$('btnSettings').onclick = openSettings;
$('setClose').onclick = closeSettings;
$('setCancel').onclick = closeSettings;

async function pickDir() {
  const res = await post('/api/pick', { mode: 'dir' });
  const p = (res.paths || [])[0];
  return p || null;
}

$('setWorkspacePick').onclick = async () => {
  try {
    const p = await pickDir();
    if (!p) { setSetMsg('已取消'); return; }
    setSetMsg(`切换工作区到 ${p} …`);
    const r = await post('/api/settings', { workspace: p });
    S2.settings = r;
    await refreshSettings();
    await loadProjects();
    setSetMsg(`工作区已切换：${r.workspace}`);
  } catch (e) { setSetMsg(`切换失败：${e.message}`); }
};

$('setDataRootPick').onclick = async () => {
  try {
    const p = await pickDir();
    if (!p) { setSetMsg('已取消'); return; }
    $('setDataRoot').value = p;
    setSetMsg('已选择新数据目录：点「保存」写入并迁移，再点「重启程序」生效');
  } catch (e) { setSetMsg(`选择失败：${e.message}`); }
};

$('setSave').onclick = async () => {
  try {
    setSetMsg('保存中…');
    await post('/api/settings', {
      ai_fast: $('setAiFast').checked,
      t2_sample_limit: Number($('setSampleLimit').value || 20000),
      log_level: $('setLogLevel').value,
    });
    const wanted = ($('setDataRoot').value || '').trim();
    if (S2.settings && wanted && wanted !== S2.settings.data_root) {
      const r = await post('/api/settings/data-dir', { path: wanted, migrate: $('setMigrate').checked });
      S2.settings = r.settings;
      await refreshSettings();
      const files = r.stats && r.stats.files ? `，已复制 ${r.stats.files} 个文件` : '，未迁移旧数据';
      setSetMsg(`数据目录已指向 ${r.target}${files}。点「重启程序」生效`);
      return;
    }
    await refreshSettings();
    setSetMsg('已保存');
  } catch (e) { setSetMsg(`保存失败：${e.message}`); }
};

$('setRestart').onclick = async () => {
  try {
    setSetMsg('正在重启…');
    await post('/api/app/restart', {});
    waitForRestart();
  } catch (e) { setSetMsg(`重启失败：${e.message}`); }
};

$('setOpenData').onclick = async () => {
  try { await post('/api/app/reveal', {}); } catch (e) { setSetMsg(`打开失败：${e.message}`); }
};

async function waitForRestart() {
  for (let i = 0; i < 90; i += 1) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const r = await fetch('/', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch { /* 服务还没起来 */ }
    setSetMsg(`等待服务重启… ${i + 1}s`);
  }
  setSetMsg('服务没有自动回来，请手动重新打开程序');
}

// ---------------- 批量 ----------------
async function pollBatch(jid, kind) {
  const label = kind === 'analyze' ? '批量检测' : '批量导出';
  for (;;) {
    await new Promise((r) => setTimeout(r, 900));
    const job = await api(`/api/job?id=${jid}`);
    const running = (job.items || []).find((i) => i.status === 'running') ||
      (job.items || [])[job.done - 1];
    setMsg(`${label} ${job.done}/${job.total} · ${running ? running.name : ''}`);
    if (job.status !== 'running') {
      const items = job.items || [];
      const ok = items.filter((i) => i.status === 'done').length;
      const bad = items.filter((i) => i.status === 'failed').length;
      const errs = items.filter((i) => i.status === 'failed').map((i) => i.name);
      setMsg(`${label}完成：成功 ${ok} · 失败 ${bad} · ${(job.elapsed_ms / 1000).toFixed(1)}s` +
             (errs.length ? `（${errs.slice(0, 3).join('、')}${errs.length > 3 ? '…' : ''}）` : ''));
      await loadPool();
      await loadProjects();
      return job;
    }
  }
}

async function batchAnalyze() {
  const sources = [...S.poolChecked];
  if (!sources.length) return;
  setMsg(`批量检测 ${sources.length} 条…`);
  try {
    const res = await post('/api/batch/analyze', { sources, ai: true, workers: 2 });
    await pollBatch(res.job.id, 'analyze');
    S.poolChecked.clear();
    renderPool(); updateBatchButtons();
  } catch (e) { setMsg(`批量检测失败：${e.message}`); }
}

async function batchExport() {
  const ids = [...S.projChecked];
  if (!ids.length) return;
  const exp = (S.proj && S.proj.export) || {};
  const mode = exp.mode || 'smart';
  const out = exp.out || 'clips';
  const naming = exp.naming || '{kind}{index:04d}_{source}_{start_tc}';
  setMsg(`批量导出 ${ids.length} 个项目（${mode}）…`);
  try {
    const res = await post('/api/batch/export', { projects: ids, mode, out, naming, workers: 2 });
    await pollBatch(res.job.id, 'export');
    S.projChecked.clear();
    renderProjects(); updateBatchButtons();
  } catch (e) { setMsg(`批量导出失败：${e.message}`); }
}

$('btnBatchAnalyze').onclick = batchAnalyze;
$('btnBatchExport').onclick = batchExport;

// ---------------- 导出 ----------------
const exportState = { polling: false, jobId: null };

function expParams() {
  const range = $('expRange').value;
  const indices = range === 'checked' ? [...S.checked] : null;
  return {
    project: S.proj ? S.proj.id : '',
    indices,
    out: $('expOut').value,
    naming: $('expNaming').value || '{kind}{index:04d}_{source}_{start_tc}',
    mode: $('expMode').value,
  };
}

async function refreshExportPreview() {
  if (!S.proj) { $('expPreview').textContent = '先在素材池里检测一个素材并建立项目'; return; }
  const p = expParams();
  const qs = new URLSearchParams({ project: p.project, out: p.out, naming: p.naming });
  if (p.indices) qs.set('indices', p.indices.join(','));
  try {
    const res = await api(`/api/export/preview?${qs}`);
    $('expCount').textContent = `${res.count} 段 → ${res.dir}`;
    $('expPreview').textContent = res.segments.slice(0, 8).map((s) => s.name).join('\n') +
      (res.count > 8 ? `\n… 共 ${res.count} 段` : '');
  } catch (e) {
    $('expPreview').textContent = `预览失败：${e.message}`;
  }
}

function openExport() {
  const exp = (S.proj && S.proj.export) || {};
  if (exp.mode) $('expMode').value = exp.mode;
  if (exp.out) $('expOut').value = exp.out;
  if (exp.naming) $('expNaming').value = exp.naming;
  $('exportModal').classList.remove('hidden');
  $('expResults').innerHTML = '';
  $('expMsg').textContent = '';
  $('expBar').style.width = '0%';
  if (!S.checked.size) $('expRange').value = 'all';
  refreshExportPreview();
}

function closeExport() { $('exportModal').classList.add('hidden'); }

async function startExport() {
  if (!S.proj) { $('expMsg').textContent = '没有可导出的项目'; return; }
  const p = expParams();
  $('expStart').disabled = true;
  $('expMsg').textContent = '导出中…';
  try {
    const res = await post('/api/export', p);
    exportState.jobId = res.job.id;
    const planned = res.planned.length;
    for (;;) {
      await new Promise((r) => setTimeout(r, 600));
      const job = await api(`/api/job?id=${exportState.jobId}`);
      const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
      $('expBar').style.width = `${pct}%`;
      $('expMsg').textContent = job.status === 'running'
        ? `导出中 ${job.done}/${job.total} · ${job.current || ''}`
        : job.status === 'done'
          ? `完成 ${planned} 段，用时 ${(job.elapsed_ms / 1000).toFixed(1)}s`
          : `失败：${job.error || ''}`;
      if (job.status !== 'running') { renderExportResults(job); break; }
    }
  } catch (e) {
    $('expMsg').textContent = `导出失败：${e.message}`;
  } finally {
    $('expStart').disabled = false;
    try {
      const keepId = S.proj ? S.proj.id : null;
      await loadProjects();
      const fresh = S.projects.find((p) => p.id === keepId);
      if (fresh) S.proj = fresh;
    } catch { /* ignore */ }
  }
}

function openClipPreview(name) {
  if (!S.proj) return;
  $('clipTitle').textContent = name;
  $('clipMeta').textContent = `项目 ${S.proj.name} · 点击关闭或按 Esc 返回`;
  const v = $('clipVideo');
  v.src = `/api/export/file?project=${encodeURIComponent(S.proj.id)}&name=${encodeURIComponent(name)}`;
  $('clipPreview').classList.remove('hidden');
  v.play().catch(() => {});
}

function closeClipPreview() {
  const v = $('clipVideo');
  v.pause(); v.removeAttribute('src'); v.load();
  $('clipPreview').classList.add('hidden');
}

$('clipClose').onclick = closeClipPreview;
$('clipPreview').onclick = (e) => { if (e.target.id === 'clipPreview') closeClipPreview(); };

function renderExportResults(job) {
  const rows = job.results || [];
  const bad = rows.filter((r) => r.status !== 'ok').length;
  const drift = rows.filter((r) => r.status === 'ok' && r.frames !== r.expected);
  const maxDelta = drift.reduce((m, r) => Math.max(m, (r.frames || 0) - (r.expected || 0)), 0);
  $('expMsg').textContent +=
    `　|　失败 ${bad} · 尾部补帧 ${drift.length} 段` +
    (drift.length ? `（最多 +${maxDelta} 帧；copy/smart 固有，要严格帧数请用 exact）` : '');
  const html = ['<table>'];
  for (const r of rows.slice(0, 40)) {
    if (r.status !== 'ok') {
      html.push(`<tr><td>${r.file}</td><td colspan="3">失败：${r.error || ''}</td></tr>`);
      continue;
    }
    const delta = (r.frames || 0) - (r.expected || 0);
    const frameCell = delta === 0
      ? `<td class="num" style="color:#4ade80">${r.frames}/${r.expected}</td>`
      : `<td class="num" style="color:#fbbf24">${r.frames}/${r.expected} (${delta > 0 ? '+' : ''}${delta})</td>`;
    const offsetCell = `<td class="num">${r.offset_s ? `${r.offset_s.toFixed(2)}s` : '0'}</td>`;
    const thumb = S.proj
      ? `<td class="thumb"><img loading="lazy" src="/api/export/frame?project=${encodeURIComponent(S.proj.id)}&name=${encodeURIComponent(r.file)}&frame=0&width=96" alt="" /></td>`
      : '<td class="thumb"></td>';
    html.push(`<tr class="row-link" data-name="${r.file}">${thumb}<td>${r.file}</td>${frameCell}${offsetCell}` +
              `<td class="num">${(r.size / 1e6).toFixed(1)}MB</td></tr>`);
  }
  html.push('</table>');
  if (rows.length > 40) html.push(`<div class="dim">… 共 ${rows.length} 段，完整清单见 _manifest/manifest.csv</div>`);
  $('expResults').innerHTML = html.join('');
  $('expResults').querySelectorAll('tr.row-link').forEach((tr) => {
    tr.onclick = () => openClipPreview(tr.dataset.name);
  });
}

$('btnExport').onclick = openExport;
$('expClose').onclick = closeExport;
$('expCancel').onclick = closeExport;
$('expStart').onclick = startExport;
$('expLast').onclick = loadLastResults;

async function loadLastResults() {
  if (!S.proj) { $('expMsg').textContent = '没有项目'; return; }
  try {
    const res = await api(`/api/export/last?project=${encodeURIComponent(S.proj.id)}`);
    if (!res.count) { $('expMsg').textContent = '本项目还没有导出记录'; return; }
    $('expMsg').textContent = `上次导出 ${res.count} 段 · ${res.mode || ''} · ${res.exported_at || ''}`;
    $('expResults').innerHTML = '';
    renderExportResults({ results: res.results, total: res.count });
  } catch (e) {
    $('expMsg').textContent = `读取失败：${e.message}`;
  }
}
for (const id of ['expRange', 'expMode', 'expOut']) $(id).onchange = refreshExportPreview;
$('expNaming').onchange = refreshExportPreview;

// ---------------- 键盘 ----------------
video.addEventListener('timeupdate', () => {
  if (!S.playing) return;
  S.frame = Math.round(video.currentTime * S.fps);
  updateStatus();
});
video.addEventListener('ended', pause);
$('playOverlay').onclick = play;

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.key === 'Escape') {
    if (!$('clipPreview').classList.contains('hidden')) { closeClipPreview(); return; }
    if (!$('exportModal').classList.contains('hidden')) { closeExport(); return; }
  }
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  switch (e.key) {
    case ' ': e.preventDefault(); togglePlay(); break;
    case 'ArrowLeft': e.preventDefault(); step(e.shiftKey ? -10 : -1); break;
    case 'ArrowRight': e.preventDefault(); step(e.shiftKey ? 10 : 1); break;
    case 'b': case 'B': splitAtPlayhead(); break;
    case 'm': case 'M': mergeSelected(); break;
    case ',': { const p = [...S.clips].reverse().find((c) => c.start < S.frame); if (p) { selectClip(p.index); seek(p.start); } break; }
    case '.': { const n = S.clips.find((c) => c.start > S.frame); if (n) { selectClip(n.index); seek(n.start); } break; }
    case 'Home': seek(0); break;
    case 'End': seek(S.total - 1); break;
    default: break;
  }
});
window.addEventListener('resize', drawTimeline);

// ---------------- 启动 ----------------
(async function boot() {
  await initLogLevel();
  refreshSettings().catch(() => {});
  await loadPool();
  await loadProjects();
  await resumeRunningJob();
  if (S.projects.length) {
    const params = new URLSearchParams(location.search);
    const pick = S.projects.find((p) => p.id === params.get('project')) || S.projects[0];
    await openProject(pick.id);
    const params2 = new URLSearchParams(location.search);
    if (params2.get('export') === '1') openExport();
    if (params2.get('autorun') === '1') {
      openExport();
      setTimeout(startExport, 600);
    }
    if (params2.get('results') === '1') {
      openExport();
      setTimeout(loadLastResults, 400);
    }
    if (params2.get('clip')) openClipPreview(params2.get('clip'));
  } else {
    setMsg('把素材拖进左侧素材池，或点「选目录 / 选文件」');
  }
  const params3 = new URLSearchParams(location.search);
  if (params3.get('settings') === '1') openSettings();
})().catch((err) => setMsg(`启动失败：${err.message}`));
