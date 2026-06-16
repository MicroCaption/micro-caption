function escH(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtUp(s){
  if(s==null)return'—';
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=Math.floor(s%60);
  return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(sc).padStart(2,'0');
}

function dotClass(status){
  return status==='live'?'dot-live'
    :status==='starting'?'dot-starting'
    :status==='ended'?'dot-ended':'dot-error';
}

function cardHtml(s){
  const ended=s.status==='ended';
  const badge=s.source_type==='youtube'
    ?'<span class="badge badge-yt">YouTube</span>'
    :'<span class="badge badge-stream">Stream</span>';
  const dotCls=dotClass(s.status);
  const urlD=s.url.length>64?s.url.slice(0,64)+'…':s.url;
  const cueHtml=s.last_cue
    ?`<div class="card-cue">"${escH(s.last_cue.slice(0,110)+(s.last_cue.length>110?'…':''))}"</div>`
    :'<div class="card-cue idle">No captions yet…</div>';
  const errHtml=s.error?`<div style="color:#7a2a2a;font-size:.72em;margin-bottom:8px">${escH(s.error)}</div>`:'';
  const watchUrl=s.code?window.location.origin+'/watch-viewer?code='+encodeURIComponent(s.code):'';
  const qrSrc=s.code?'https://api.qrserver.com/v1/create-qr-code/?data='+encodeURIComponent(watchUrl)+'&size=120x120&bgcolor=0f0f0f&color=cccccc&margin=4':'';
  const codeHtml=s.code?`<div class="card-watch-info"><span class="session-code">${escH(s.code)}</span>${qrSrc?`<img class="watch-qr" src="${qrSrc}" alt="QR">`:''}
</div>`:'';
  // Ended streams: keep the card so the caption log stays viewable, but swap
  // the live affordances for replay/remove.
  const stopBtn=ended
    ?`<button class="btn-stop" title="Remove from list" onclick="stopSess('${escH(s.id)}')">&times;&nbsp;Remove</button>`
    :`<button class="btn-stop" onclick="stopSess('${escH(s.id)}')">&#9632;</button>`;
  const watchBtn=ended
    ?`<a href="/player?id=${escH(s.id)}&mode=replay" target="_blank" rel="noopener" class="btn-watch">&#9198;&nbsp;Caption log</a>`
    :`<a href="/player?id=${escH(s.id)}" target="_blank" rel="noopener" class="btn-watch">&#9654;&nbsp;Watch live</a>`;
  const viewerLink=(!ended&&s.code)?`<a href="/watch-viewer?code=${encodeURIComponent(s.code)}" target="_blank" rel="noopener" class="btn-watch">&#128241;&nbsp;Viewer</a>`:'';
  return `<div class="session-card ${escH(s.status)}" id="sess-${escH(s.id)}" data-status="${escH(s.status)}">
<div class="card-header">
  <span class="dot ${dotCls}"></span>
  <span class="card-status">${s.status.toUpperCase()}</span>
  <span class="card-id">${escH(s.id)}</span>
  ${badge}
  ${stopBtn}
</div>
<div class="card-url">${escH(urlD)}</div>
<div class="card-stats">${fmtUp(s.uptime_s)}&nbsp;&middot;&nbsp;${s.cue_count}&nbsp;cues</div>
${errHtml}${cueHtml}
<div class="card-footer">
  ${codeHtml}
  <div style="display:flex;gap:8px;align-items:center">
    ${viewerLink}
    ${watchBtn}
  </div>
</div>
</div>`;
}

// Per-session card elements survive polling refreshes so QR images don't flicker.
const _cards={};
function _qrSrc(code){
  const url=window.location.origin+'/watch-viewer?code='+encodeURIComponent(code);
  return 'https://api.qrserver.com/v1/create-qr-code/?data='+encodeURIComponent(url)+'&size=120x120&bgcolor=0f0f0f&color=cccccc&margin=4';
}
function _injectCard(grid,s){
  const tmp=document.createElement('div');
  tmp.innerHTML=cardHtml(s);
  const card=tmp.firstElementChild;
  const qrImg=card.querySelector('.watch-qr');
  if(qrImg&&s.code)qrImg.src=_qrSrc(s.code);
  grid.appendChild(card);
  _cards[s.id]=card;
}
function _updateCard(card,s){
  // Status changed (e.g. live → ended): re-render so the card's class, dot,
  // and action buttons all reflect the new state.
  if(card.dataset.status!==s.status){
    const tmp=document.createElement('div');
    tmp.innerHTML=cardHtml(s);
    const fresh=tmp.firstElementChild;
    const qrImg=fresh.querySelector('.watch-qr');
    if(qrImg&&s.code)qrImg.src=_qrSrc(s.code);
    card.replaceWith(fresh);
    _cards[s.id]=fresh;
    return;
  }
  const dot=card.querySelector('.dot');
  const lbl=card.querySelector('.card-status');
  const dotCls=dotClass(s.status);
  if(dot)dot.className='dot '+dotCls;
  if(lbl)lbl.textContent=s.status.toUpperCase();
  const statsEl=card.querySelector('.card-stats');
  if(statsEl)statsEl.innerHTML=fmtUp(s.uptime_s)+'&nbsp;&middot;&nbsp;'+s.cue_count+'&nbsp;cues';
  const cueEl=card.querySelector('.card-cue');
  if(cueEl){
    if(s.last_cue){
      const t=s.last_cue.slice(0,110)+(s.last_cue.length>110?'…':'');
      cueEl.textContent='"'+t+'"';
      cueEl.classList.remove('idle');
    }else{
      cueEl.textContent='No captions yet…';
      cueEl.classList.add('idle');
    }
  }
}

async function refresh(){
  try{
    const data=await(await fetch(API+'/api/sessions')).json();
    const wrap=document.getElementById('sessions-grid-wrap');
    const cnt=document.getElementById('stream-count');
    if(cnt)cnt.textContent=data.length+' active';
    if(!wrap)return;
    if(!data.length){
      wrap.innerHTML='<div class="sessions-grid"><div class="empty-state">No active streams — add one below.</div></div>';
      for(const id of Object.keys(_cards))delete _cards[id];
      return;
    }
    let grid=wrap.querySelector('.sessions-grid');
    if(!grid){grid=document.createElement('div');grid.className='sessions-grid';wrap.innerHTML='';wrap.appendChild(grid);}
    // Drop the initial "Loading…" placeholder now that we have sessions.
    const placeholder=grid.querySelector('.empty-state');
    if(placeholder)placeholder.remove();
    // Adopt server-rendered cards so we don't create duplicates.
    for(const s of data){
      if(!_cards[s.id]){
        const el=document.getElementById('sess-'+s.id);
        if(el){
          _cards[s.id]=el;
          const qi=el.querySelector('.watch-qr');
          if(qi&&!qi.src&&s.code)qi.src=_qrSrc(s.code);
        }
      }
    }
    const liveIds=new Set(data.map(s=>s.id));
    for(const id of Object.keys(_cards)){
      if(!liveIds.has(id)){_cards[id].remove();delete _cards[id];}
    }
    for(const s of data){
      if(_cards[s.id]){_updateCard(_cards[s.id],s);}
      else{_injectCard(grid,s);}
    }
  }catch(e){}
}

async function stopSess(id){
  try{await fetch(API+'/api/stop/'+id,{method:'POST'});}catch(e){}
  refresh();
}

const addForm=document.getElementById('add-form');
if(addForm){
  addForm.addEventListener('submit',async e=>{
    e.preventDefault();
    const inp=document.getElementById('url-input');
    const btn=document.getElementById('add-btn');
    const url=inp.value.trim();
    if(!url)return;
    btn.disabled=true; btn.textContent='Starting…';
    try{
      const r=await fetch(API+'/api/start',{
        method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'url='+encodeURIComponent(url),
      });
      const d=await r.json();
      if(d.session_id){
        inp.value='';
        window.open('/player?id='+d.session_id,'_blank','noopener');
        await refresh();
      } else if(d.error){
        alert('Error: '+d.error);
      }
    }catch(e){alert('Request failed: '+e);}
    finally{btn.disabled=false; btn.textContent='▶ Caption';}
  });
}

refresh();
setInterval(refresh,2000);
