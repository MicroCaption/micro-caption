// logs.js — Heroic-style log manager: a sources sidebar (system logs, live
// streams, stream history) + a viewer pane (file tails or caption history).
// API global comes from config.js.

const side    = document.getElementById('log-side');
const elTitle = document.getElementById('log-title');
const elMeta  = document.getElementById('log-meta');
const elActs  = document.getElementById('log-actions');
const elBody  = document.getElementById('log-body');

let current = null;       // { kind:'file'|'session', id, label, live }
let autoOn  = false;
let autoTimer = null;

function escH(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtSize(b){ if(!b) return '0 B'; const u=['B','KB','MB','GB']; let i=0; while(b>=1024&&i<u.length-1){b/=1024;i++;} return b.toFixed(b<10&&i>0?1:0)+' '+u[i]; }
function fmtClock(sec){ sec=Math.max(0,Math.floor(sec||0)); const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60; const mm=String(m).padStart(2,'0'),ss=String(s).padStart(2,'0'); return h?`${h}:${mm}:${ss}`:`${mm}:${ss}`; }
function fmtDate(epoch){ if(!epoch) return ''; const d=new Date(epoch*1000); return isNaN(d)?'':d.toLocaleString(); }
function dotClass(status){ return 'dot dot-'+({live:'live',starting:'starting',ended:'ended',error:'error'}[status]||'idle'); }

function stopAuto(){ if(autoTimer){clearInterval(autoTimer);autoTimer=null;} }
function setAuto(on){
  autoOn = on; stopAuto();
  if(on) autoTimer = setInterval(() => { if(current) (current.kind==='file'?loadFile():loadSession()); }, 3000);
  const t=document.getElementById('auto-toggle'); if(t) t.className='toggle'+(on?' on':'');
}

// ── Sidebar ────────────────────────────────────────────────────────────────
async function loadSources(){
  let data;
  try { data = await (await fetch(API+'/api/logs/sources')).json(); }
  catch(e){ side.innerHTML='<div class="empty">Failed to load sources.</div>'; return; }

  const html = [];
  html.push('<div class="log-group-h">System</div>');
  for(const f of data.system){
    html.push(itemHTML('file', f.id, f.label, f.exists?fmtSize(f.size):'empty', null));
  }
  if(data.live && data.live.length){
    html.push('<div class="log-group-h">Live streams</div>');
    for(const s of data.live) html.push(streamItem(s, true));
  }
  if(data.archive && data.archive.length){
    html.push('<div class="log-group-h">Stream history</div>');
    for(const s of data.archive) html.push(streamItem(s, false));
  }
  side.innerHTML = html.join('');
  side.querySelectorAll('.log-item').forEach(el => {
    el.onclick = () => select(el.dataset.kind, el.dataset.id, el.dataset.label, el.dataset.live==='1');
  });
  // Re-highlight the active item across refreshes.
  if(current) highlight();
}

function streamItem(s, live){
  const label = s.video_id || s.url || s.id;
  const sub = (live ? s.status : fmtDate(s.created_at)) + ' · ' + (s.cue_count||0) + ' cues';
  return itemHTML('session', s.id, label, sub, s.status, live);
}

function itemHTML(kind, id, label, sub, status, live){
  const dot = status!==null && status!==undefined ? `<span class="${dotClass(status)}"></span>` : '';
  return `<div class="log-item" data-kind="${kind}" data-id="${escH(id)}" data-label="${escH(label)}" data-live="${live?1:0}">
    ${dot}<span class="li-main">${escH(label)}</span><span class="li-sub">${escH(sub)}</span></div>`;
}

function highlight(){
  side.querySelectorAll('.log-item').forEach(el => {
    el.classList.toggle('active', current && el.dataset.kind===current.kind && el.dataset.id===current.id);
  });
}

// ── Selection ────────────────────────────────────────────────────────────────
function select(kind, id, label, live){
  current = { kind, id, label, live };
  highlight();
  setAuto(false);
  if(kind==='file') loadFile(); else loadSession();
}

function headActions(buttons){
  elActs.innerHTML = buttons.join('');
}
function autoToggleHTML(){
  return `<span id="auto-toggle" class="toggle${autoOn?' on':''}" onclick="setAuto(!autoOn)">● auto-refresh</span>`;
}
function btn(label, fn){ return `<button class="btn btn-neutral" onclick="${fn}">${label}</button>`; }

// ── File viewer ──────────────────────────────────────────────────────────────
async function loadFile(){
  const {id, label} = current;
  elTitle.textContent = label;
  let text='';
  try { text = await (await fetch(API+'/api/logs/file/'+id+'?tail=1000')).text(); }
  catch(e){ text='(failed to load)'; }
  elMeta.textContent = text ? (text.split('\n').length+' lines') : 'empty';
  headActions([ autoToggleHTML(), btn('Refresh','loadFile()'), btn('Copy','copyText()'), btn('Download','downloadFile()') ]);
  elBody.innerHTML = `<pre class="log-pre" id="log-pre">${escH(text)||'<span class="empty">No output yet.</span>'}</pre>`;
  elBody.scrollTop = elBody.scrollHeight;   // jump to newest
}

// ── Caption history viewer ───────────────────────────────────────────────────
async function loadSession(){
  const {id, label, live} = current;
  elTitle.textContent = (live?'▶ ':'') + label;
  let d;
  try { d = await (await fetch(API+'/api/logs/session/'+id)).json(); }
  catch(e){ elBody.innerHTML='<div class="empty">Failed to load.</div>'; return; }

  elMeta.textContent = (d.status||'') + ' · ' + (d.cue_count||(d.cues||[]).length) + ' cues';
  const acts = [];
  if(live) acts.push(autoToggleHTML());
  acts.push(btn('Copy transcript','copyTranscript()'));
  acts.push(btn('Download .vtt','downloadVtt()'));
  if(live) acts.push(`<a class="btn btn-neutral" href="${API}/webvtt/${escH(id)}" target="_blank">WebVTT</a>`);
  headActions(acts);

  window._cues = d.cues || [];
  const parts = [];
  const a = d.accuracy_summary || {};
  if(a && a.wer!=null){
    parts.push('<div class="acc-bar">' +
      stat('Accuracy', ((a.accuracy!=null?a.accuracy:1-a.wer)*100).toFixed(1)+'%') +
      stat('WER', (a.wer*100).toFixed(1)+'%') +
      stat('Ref words', a.ref_words||'—') +
      stat('Sub/Del/Ins', `${a.substitutions||0}/${a.deletions||0}/${a.insertions||0}`) +
      stat('Segments', a.segment_count||0) + '</div>');
  }
  if(d.url) parts.push(`<div class="acc-bar">${stat('URL', escH(d.url))}${stat('Started', fmtDate(d.created_at)||'—')}</div>`);

  if(!window._cues.length){
    parts.push('<div class="empty">No captions recorded'+(live?' yet':'')+'.</div>');
  } else {
    let last=null;
    for(const c of window._cues){
      const spk = (c.speaker && c.speaker!==last) ? escH(c.speaker) : '';
      last = c.speaker || last;
      parts.push(`<div class="cue-row"><span class="cue-t">${fmtClock(c.start)}</span>`+
                 (c.speaker?`<span class="cue-spk">${spk}</span>`:'')+
                 `<span class="cue-x">${escH(c.text)}</span></div>`);
    }
  }
  elBody.innerHTML = parts.join('');
  if(autoOn && live) elBody.scrollTop = elBody.scrollHeight;
}
function stat(label,val){ return `<div><div class="stat-label">${label}</div><div class="stat-val">${val}</div></div>`; }

// ── Actions ──────────────────────────────────────────────────────────────────
function copyText(){
  const pre=document.getElementById('log-pre');
  if(pre) navigator.clipboard.writeText(pre.textContent);
}
function copyTranscript(){
  navigator.clipboard.writeText((window._cues||[]).map(c=>c.text).join('\n'));
}
function _download(name, text){
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([text],{type:'text/plain'}));
  a.download=name; a.click(); URL.revokeObjectURL(a.href);
}
function downloadFile(){
  const pre=document.getElementById('log-pre');
  _download((current.id||'log')+'.log', pre?pre.textContent:'');
}
function vttTime(sec){
  sec=Math.max(0,sec||0); const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=(sec%60);
  return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+s.toFixed(3).padStart(6,'0');
}
function downloadVtt(){
  const cues=window._cues||[];
  let out='WEBVTT\n\n';
  cues.forEach((c,i)=>{ out += (i+1)+'\n'+vttTime(c.start)+' --> '+vttTime(c.end)+'\n'+(c.text||'')+'\n\n'; });
  _download((current.id||'captions')+'.vtt', out);
}

// Refresh the sidebar periodically so live streams / new history appear.
loadSources();
setInterval(loadSources, 5000);
