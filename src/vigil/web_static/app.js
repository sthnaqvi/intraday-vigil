// Stamped by the server at request time. If this page is older than the running server,
// render() says so instead of leaving buttons that quietly do nothing.
const BUILT = window.VIGIL_BUILD;
// Surface JS failures instead of letting a button silently do nothing.
window.onerror=(m,src,l,c,e)=>{
  const b=document.getElementById('jserr');
  b.style.display='block';
  b.textContent='UI error: '+m+' ('+(src||'').split('/').pop()+':'+l+') — hard-reload the page (Cmd-Shift-R).';
};

const $=id=>document.getElementById(id);
const v=id=>{const el=$(id); return el?el.value.trim():'';};
const c=id=>{const el=$(id); return el?el.checked:false;};
const n=(x,d=2)=>x==null?'—':Number(x).toFixed(d);
const sign=x=>x>0?'up':x<0?'down':'dim';
const esc=s=>String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));
const money=x=>x==null?'—':'₹'+Number(x).toLocaleString('en-IN',{maximumFractionDigits:0});

document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('nav a').forEach(x=>x.classList.remove('on'));
  a.classList.add('on');
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
  $('p_'+a.dataset.pane).classList.add('on');
  if(a.dataset.pane==='logs') loadLog();
});

// Command output lives next to the form that triggered it (Trade vs Daemon), not in one
// shared panel far below the fold — a refusal or a fill report should show up right where
// you're looking, not require a scroll to notice.
function outEl(){ return $('p_trade').classList.contains('on') ? $('out_trade') : $('out_daemon'); }

async function run(cmd,params={},confirm=null){
  const out=outEl();
  out.textContent='running '+cmd+'…'; out.className='cmd-out';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd,params,confirm})});
    const j=await r.json();
    out.textContent=(j.ok?'✓ ':'✗ ')+(j.argv||cmd)+'\n\n'+j.output;
    out.className='cmd-out '+(j.ok?'ok':'fail');
    out.scrollIntoView({behavior:'smooth', block:'nearest'});
    if(cmd==='start'){
      // `start` returns as soon as the child is spawned; the child may exit immediately
      // (market closed). Re-check after a beat and say so plainly.
      setTimeout(async()=>{
        const s=await (await fetch('/api/state')).json();
        if(!s.daemon.running){
          out.textContent += '\n\n⚠ The daemon exited straight after starting'
            + (s.market_open ? '.' : ' — the market is CLOSED (now '+s.now+').')
            + '\nUse `vigil monitor --force` in a terminal to run outside market hours.';
          out.className='cmd-out fail';
        }
      }, 1500);
    }
  }catch(e){ out.textContent='request failed: '+e; out.className='cmd-out fail'; }
  tick();
}
async function askMode(m){
  await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:m})}); tick();
}
async function loadLog(){
  const src=v('log_src')||$('log_src').value; if(!src) return;
  try{
    const j=await (await fetch(`/api/logs?src=${encodeURIComponent(src)}&lines=${v('log_n')||300}`)).json();
    $('log_text').textContent=j.text;
    $('log_meta').textContent=(j.path||'')+(j.lines?`  ·  ${j.lines} lines total`:'');
    const el=$('log_text'); el.scrollTop=el.scrollHeight;
  }catch(e){ $('log_text').textContent='failed to load: '+e; }
}

/* ---------- table/list builders ---------- */
const dirArrow=d=>`<span class="dir-arrow ${d==='LONG'?'long':'short'}">${d==='LONG'?'▲':'▼'}</span>`;
const phaseChip=p=>{
  const cls = p>=3 ? 'p3' : p>=2 ? 'p2' : 'p1';
  const label = p>=3 ? 'P3 trailing' : p>=2 ? 'P2 breakeven' : 'P1 hold';
  return `<span class="phase-chip ${cls}">${label}</span>`;
};

function posTable(s){
  if(!s.positions.length) return '<div class="empty-hint">No open positions.</div>';
  return `<table><thead><tr>
    <th>Symbol</th><th>Qty</th><th>Entry</th><th>LTP</th><th>R</th><th>P&amp;L</th><th>Phase</th><th>Stop</th></tr></thead><tbody>`+
    s.positions.map(p=>{
      const stop = p.protected===false
        ? `<span class="stop-bad">NO STOP (${esc(p.sl_order_status||'?')})</span>`
        : `<span class="stop-ok">${n(p.sl_price)}</span>`;
      return `<tr class="pos-row">
        <td><div class="sym">${dirArrow(p.direction)}${esc(p.symbol)}</div></td>
        <td>${p.qty}</td><td>${n(p.entry)}</td><td>${n(p.ltp)}</td>
        <td class="${sign(p.profit_r)}">${p.profit_r>0?'+':''}${n(p.profit_r)}</td>
        <td class="${sign(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</td>
        <td>${phaseChip(p.phase)}</td>
        <td><div class="stop-cell">${stop}</div></td>
      </tr>`;
    }).join('')
    +`</tbody></table>`;
}

function trigTable(s){
  if(!s.triggers.length) return '<div class="empty-hint">Nothing armed.</div>';
  return `<table><thead><tr>
    <th>Symbol</th><th>Dir</th><th>Side</th><th>Level</th><th>Qty</th><th>SL%</th><th>Auto</th><th>Status</th></tr></thead><tbody>`+
    s.triggers.map(t=>`<tr class="pos-row">
      <td><div class="sym">${dirArrow(t.direction)}${esc(t.symbol)}</div></td>
      <td style="color:var(--text-dim)">${t.direction}</td>
      <td style="color:var(--text-dim)">${t.side}</td>
      <td>${n(t.level)}</td><td>${t.qty}</td><td>${n(t.sl_pct*100)}%</td>
      <td>${t.auto?'<span class="phase-chip auto">AUTO</span>':'<span style="color:var(--text-dim)">alert</span>'}</td>
      <td style="color:var(--text-dim)">${esc(t.status)}</td>
    </tr>`).join('')
    +`</tbody></table>`;
}

function evList(events,limit){
  const e=events.slice(0,limit);
  // Head line (time/type/symbol) and data run on separate lines — a data string can be
  // long, and squeezing it into a third column of a flex row is what wrapped it into
  // 6-8 half-empty lines in a narrow sidebar. As its own full-width line below, it wraps
  // at most once or twice, and stays legible even inside a 300px column.
  return e.length ? e.map(x=>`<div class="ev">
      <div class="ev-head"><span class="t">${esc(x.ts)}</span><span class="k">${esc(x.type)}</span>${x.symbol?`<span class="sym">${esc(x.symbol)}</span>`:''}</div>
      ${x.data?`<div class="d" data-full="${esc(x.data)}">${esc(x.data)}</div>`:''}
    </div>`).join('')
    : '<div class="empty-hint">No events yet.</div>';
}

function closedTable(s){
  if(!s.closed_today.length) return '<div class="pb"><span class="hint">No closed positions today.</span></div>';
  return `<table><thead><tr><th>Symbol</th><th>Reason</th><th>Exit</th><th>R</th><th>P&amp;L</th></tr></thead><tbody>`+
    s.closed_today.map(x=>`<tr class="pos-row"><td>${esc(x.symbol)}</td>
      <td style="color:var(--text-dim)">${esc(x.exit_reason)}</td><td>${n(x.exit_price)}</td>
      <td class="${sign(x.realized_r)}">${n(x.realized_r)}</td>
      <td class="${sign(x.realized_pnl)}">${money(x.realized_pnl)}</td></tr>`).join('')
    +`</tbody></table>`;
}

function renderAccount(a){
  if(!a || !a.ok){
    $('acctWho').innerHTML=`<span class="pill bad">KITE TOKEN</span> ${a?esc(a.error||'unavailable'):'…'}`;
    $('acctFunds').innerHTML='';
    $('acct_full').innerHTML=`<dt style="grid-column:1/-1;color:var(--bad)">Cannot read the account: ${a?esc(a.error||''):'…'}</dt>
      <dd style="grid-column:1/-1;color:var(--text-faint);font-family:'Manrope',sans-serif;font-size:11.5px">
      The daemon token dies around 06:00 daily. Re-auth with <code>vigil login</code>.</dd>`;
    $('acct_funds').innerHTML='<dt>—</dt><dd>—</dd>';
    return;
  }
  $('acctWho').innerHTML=`<b>${esc(a.client_id||'?')}</b> ${esc(a.name||'')}`;
  $('acctFunds').innerHTML=a.paper
    ? `<span class="pill paper">PAPER — no real money</span>`
    : `avail <b class="up">${money(a.available)}</b> · used <b>${money(a.used)}</b>`
      + (a.m2m_unrealised!=null ? ` · M2M <b class="${sign(a.m2m_unrealised)}">${money(a.m2m_unrealised)}</b>` : '');

  $('acct_full').innerHTML=`
    <dt>Client ID</dt><dd>${esc(a.client_id||'—')}</dd>
    <dt>Name</dt><dd style="font-family:'Manrope',sans-serif">${esc(a.name||'—')}</dd>
    <dt>Broker</dt><dd>${esc(a.broker||'—')}</dd>
    <dt>Email</dt><dd>${esc(a.email||'—')}</dd>
    <dt>Exchanges</dt><dd>${esc((a.exchanges||[]).join(', ')||'—')}</dd>
    <dt>Products</dt><dd>${esc((a.products||[]).join(', ')||'—')}</dd>`;

  const used=Number(a.used||0), total=used+Number(a.available||0);
  const pct=total?Math.round(used/total*100):0;
  $('acct_funds').innerHTML=`
    <dt>Opening</dt><dd>${money(a.opening)}</dd>
    <dt>Net</dt><dd>${money(a.net)}</dd>
    <dt>Available</dt><dd class="up">${money(a.available)}</dd>
    <dt>Used</dt><dd>${money(a.used)} <span style="color:var(--text-faint)">${pct}% deployed</span></dd>
    <dt>M2M unrealised</dt><dd class="${sign(a.m2m_unrealised)}">${money(a.m2m_unrealised)}</dd>
    <dt>M2M realised</dt><dd class="${sign(a.m2m_realised)}">${money(a.m2m_realised)}</dd>`;
}

function render(s){
  $('ver').textContent='v'+s.ui_version;
  renderAccount(s.account);
  if(s.ui_version!==BUILT){
    const b=$('jserr'); b.style.display='block';
    b.textContent=`This page is v${BUILT} but the server is v${s.ui_version} — hard-reload (Cmd-Shift-R).`;
  }
  const d=s.daemon;
  $('marketPill').innerHTML = s.market_open
    ? `<span class="dot" style="background:var(--good)"></span>MARKET OPEN`
    : `MARKET CLOSED`;
  $('marketPill').className = 'pill '+(s.market_open?'ok':'warnp');
  $('clock').textContent = s.now;

  if(s.account && s.account.paper){
    $('daemonPill').className='pill paper';
    $('daemonPill').textContent = d.running ? `PAPER · pid ${d.pid} · ${d.age_s}s ago` : 'PAPER — DAEMON NOT RUNNING';
  }else if(!d.running){
    $('daemonPill').className='pill bad';
    $('daemonPill').textContent='DAEMON NOT RUNNING';
  }else{
    $('daemonPill').className='pill '+(d.fresh?'ok':'warnp');
    $('daemonPill').innerHTML=`<span class="dot" style="background:${d.fresh?'var(--good)':'var(--warn)'}"></span>${d.fresh?'LIVE':'STALE'} · pid ${d.pid} · ${d.mode} · ${d.age_s}s ago`;
  }

  let al='';
  // No embedded clock here — the header clock already ticks every second on its own, and
  // splicing a live-changing number into the middle of this sentence (rebuilt in full on
  // every SSE push) shifted where the sentence wrapped from one second to the next, which
  // read as the whole banner jittering.
  if(!d.running && !s.market_open)
    al+=`<div class="stale">Daemon is not running, and the market is CLOSED.
         <b>start</b> will launch it and it will exit immediately — that is expected, not a broken button.
         To run anyway: <code>vigil monitor --force</code>.</div>`;
  else if(!d.running) al+=`<div class="banner">Daemon is not running — no SL management and no auto square-off.</div>`;
  else if(!d.fresh) al+=`<div class="stale">Snapshot is ${d.age_s}s old — treat these numbers as stale.</div>`;
  const naked=s.positions.filter(p=>p.protected===false);
  for(const p of naked)
    al+=`<div class="banner">${esc(p.symbol)} UNPROTECTED — ${p.qty} ${p.direction}, SL order ${esc(p.sl_order_status)}.</div>`;
  if(s.kill_switch) al+=`<div class="banner">KILL SWITCH — day at ${n(s.realized_r_today)}R. No new entries.</div>`;
  $('alerts').innerHTML=al;

  const nb=$('nb_pos');
  if(naked.length){ nb.style.display='inline-flex'; nb.textContent='!'; } else nb.style.display='none';

  // ---- hero row ----
  // Each card's top edge is colored by its own state (heroAcc), not left flat grey —
  // three identical boxes distinguished only by text color inside them read as one
  // undifferentiated block at a glance, which was part of what made the whole page feel
  // monotone.
  const heroAcc=(el,cls)=>{ const h=el.closest('.hero'); if(h) h.className='hero '+cls; };
  const pnl=s.realized_pnl_today, rr=s.realized_r_today;
  $('heroPnl').textContent=money(pnl); $('heroPnl').className='value '+sign(pnl);
  $('heroR').textContent=(rr>0?'+':'')+n(rr)+'R'; $('heroR').className='sub '+sign(rr);
  heroAcc($('heroPnl'), pnl>0?'acc-good':pnl<0?'acc-bad':'acc-accent');
  const a=s.account||{};
  $('heroAvail').textContent = a.paper ? 'PAPER' : money(a.available);
  const usedTot=Number(a.used||0), totTot=usedTot+Number(a.available||0);
  const pctTot = totTot ? Math.round(usedTot/totTot*100) : 0;
  $('heroUsed').textContent = a.paper ? 'no real money' : `used ${money(a.used)} · ${pctTot}% deployed`;
  heroAcc($('heroAvail'), 'acc-accent');

  const ring=$('protRing'), protTxt=$('heroProt'), protSub=$('heroProtSub');
  if(!s.positions.length){
    ring.className='ring'; protTxt.textContent='—'; protSub.textContent='no open positions';
    protSub.style.color=''; heroAcc(ring,'protection acc-accent');
  }else if(naked.length===0){
    ring.className='ring'; protTxt.textContent=`${s.positions.length} / ${s.positions.length} protected`;
    protSub.textContent='every open position has a resting stop'; protSub.style.color='var(--good)';
    heroAcc(ring,'protection acc-good');
  }else if(naked.length===s.positions.length){
    ring.className='ring bad'; protTxt.textContent=`0 / ${s.positions.length} protected`;
    protSub.textContent='no position has a resting stop'; protSub.style.color='var(--bad)';
    heroAcc(ring,'protection acc-bad');
  }else{
    ring.className='ring warn'; protTxt.textContent=`${s.positions.length-naked.length} / ${s.positions.length} protected`;
    protSub.textContent=`${naked.length} without a resting stop`; protSub.style.color='var(--warn)';
    heroAcc(ring,'protection acc-warn');
  }

  // ---- overview: positions, triggers, events ----
  $('pos').innerHTML=posTable(s);
  $('pos_count').textContent = s.positions.length ? `(${s.positions.length})` : '';
  const ageTxt = d.running ? `as of ${d.age_s}s ago${d.fresh?'':' — STALE'}` : '';
  $('pos_age').innerHTML = ageTxt ? `<span class="dot" style="width:5px;height:5px;border-radius:50%;background:${d.fresh?'var(--good)':'var(--warn)'};animation:${d.fresh?'pulse 1.8s ease-in-out infinite':'none'}"></span>${ageTxt}` : '';
  $('pos_age').style.color = d.fresh ? 'var(--good)' : 'var(--warn)';
  $('trig').innerHTML=trigTable(s);
  $('trig_count').textContent = s.triggers.length ? `(${s.triggers.length})` : '';
  $('events_s').innerHTML=evList(s.events,40);
  $('events_full').innerHTML=evList(s.events,200);
  $('closed_today').innerHTML=closedTable(s);

  // ---- daemon pane ----
  const cad=s.cadence||{};
  $('daemonStatus').innerHTML=`
    <dt>State</dt><dd><span class="pill ${d.running?(d.fresh?'ok':'warnp'):'bad'}" style="margin-right:8px">
      <span class="dot" style="background:currentColor"></span>${d.running?(d.fresh?'RUNNING':'STALE'):'STOPPED'}</span>${d.pid?`pid ${d.pid}`:''}</dd>
    <dt>Mode</dt><dd>${esc(d.mode)} · ${esc(d.broker)}</dd>
    <dt>Snapshot</dt><dd class="${d.fresh?'up':'down'}">${d.age_s!=null?d.age_s+'s ago':'—'}${d.fresh===false?' (STALE)':d.running?' (fresh)':''}</dd>
    <dt>Cadence</dt><dd>reconcile ~${cad.reconcile_s||'—'}s · qty-verify ~${cad.qty_verify_s||'—'}s · loop ${cad.loop_s||'—'}s</dd>
    <dt>Cycles run</dt><dd>${d.cycles_run!=null?Number(d.cycles_run).toLocaleString('en-IN'):'—'}</dd>
    <dt>Market</dt><dd><span class="pill ${s.market_open?'ok':'warnp'}" style="padding:2px 8px">${s.market_open?'MARKET OPEN':'MARKET CLOSED'}</span></dd>
    <dt>Kill switch</dt><dd class="${s.kill_switch?'down':'up'}">${s.kill_switch?'ON':'off'}</dd>`;

  if(!$('log_src').options.length){
    const opts=s.log_sources.map(x=>`<option>${x}</option>`).join('');
    $('log_src').innerHTML=opts;
    $('live_src').innerHTML=opts;
    $('live_src').value=localStorage.getItem('liveSrc')||'algo.log';
    if(localStorage.getItem('liveLeft')==='1') $('live').classList.add('left');
    if(localStorage.getItem('liveOpen')==='1') liveToggle(true);
  }
  $('raildot').style.background = d.running && d.fresh ? 'var(--good)' : 'var(--text-faint)';
  $('modes').innerHTML = s.skill_modes.map(m=>`<button class="mode-btn" onclick="askMode('${m}')">/${m}</button>`).join('');
  $('mode').textContent = s.claude.cli ? `CLI: ${s.claude.cli}`
    : 'No claude CLI — questions queue for a Claude session';
  $('qa').innerHTML = s.claude.recent.length ? s.claude.recent.map(r=>`<div class="qa">
     <div class="q">${esc(r.question)}</div>
     <div class="a">${r.status==='pending'?'<span class="dim">queued — waiting for a Claude session</span>':esc(r.answer||'')}</div>
     </div>`).join('') : '<div class="empty-hint">Nothing queued yet.</div>';

  syncEventsHeight();
}

// Recent events gets whichever is TALLER: the height of Positions+Armed triggers beside
// it, or whatever's left of the visible screen below the sticky hero row — not just
// Positions+Armed triggers' height on its own. Matching only the tables meant an account
// with few (or zero) open positions left most of the screen blank underneath a short
// events box, even though there was plenty of room and plenty more history to show.
// Measured against the viewport (not just the sibling column) for the same reason the
// previous CSS align-items:stretch approach was replaced with JS in the first place:
// there's no way to express "at least this tall, but grow to fill the screen" in pure
// CSS grid without reintroducing the circular sizing bug that broke last time.
function syncEventsHeight(){
  const ovMain=$('ovMain'), cap=$('events_s'), mainEl=document.querySelector('main');
  if(!ovMain || !cap || !mainEl) return;
  if(window.innerWidth <= 980){ cap.style.maxHeight=''; return; }
  // Measured from #events_s's own top, not .ov-grid's — the grid's top edge doesn't
  // account for "Recent events"'s own section head and panel border sitting between the
  // grid and events_s, which was ~29px unaccounted for and is where the residual scroll
  // after the first fix (measuring from the grid) came from.
  const mainRect=mainEl.getBoundingClientRect(), capRect=cap.getBoundingClientRect();
  // +mainEl.scrollTop cancels out the current scroll position — capRect.top is relative
  // to the viewport, so it shrinks (even goes negative) as the page scrolls down past
  // events_s; without correcting for that, "available space" grew a little more on every
  // ~1s SSE re-render while scrolled into the list, which read as unbounded growth. Adding
  // scrollTop back converts that live, scroll-position-relative gap into the fixed offset
  // from main's own content top — the same number you'd get freshly loading the page.
  const trueOffset=(capRect.top-mainRect.top)+mainEl.scrollTop;
  // main's real bottom padding, not a guessed buffer.
  const mainPadBottom=parseFloat(getComputedStyle(mainEl).paddingBottom)||0;
  const availableScreen=mainEl.clientHeight-trueOffset-mainPadBottom;
  const h=Math.max(ovMain.offsetHeight, availableScreen);
  if(h>0) cap.style.maxHeight=h+'px';
}
window.addEventListener('resize', syncEventsHeight);

/* ---------- live log dock ----------
   A narrow rail when collapsed, and when open it only auto-scrolls if you are already
   parked at the bottom. Scroll up to read something and the feed stops yanking you away;
   a "↓ live" button re-pins. */
const PIN_PX = 48;
let livePinned = true, liveText = '', liveMatches = [], liveIdx = 0;

function liveToggle(open){
  const el=$('live');
  el.classList.toggle('open', open); el.classList.toggle('collapsed', !open);
  $('railbtn').style.display = open ? 'none' : 'flex';
  for(const id of ['livehead','livewrap','livefoot'])
    $(id).style.display = open ? (id==='livewrap'?'flex':'block') : 'none';
  localStorage.setItem('liveOpen', open?'1':'0');
  if(open){ livePinned=true; liveLoad(); }
}
function liveSide(){
  const el=$('live'), left=el.classList.toggle('left');
  localStorage.setItem('liveLeft', left?'1':'0');
}
function liveSwitch(){
  localStorage.setItem('liveSrc', $('live_src').value);
  livePinned=true; liveLoad();
}
function livePin(){
  const b=$('livebody'); b.scrollTop=b.scrollHeight;
  livePinned=true; $('jump').style.display='none';
}
$('livebody')?.addEventListener('scroll',()=>{
  const b=$('livebody');
  const dist=b.scrollHeight-b.scrollTop-b.clientHeight;
  livePinned = dist < PIN_PX;
  $('jump').style.display = livePinned ? 'none' : 'block';
});

function liveRender(){
  const b=$('livebody');
  const q=v('livesearch');
  if(!q){
    b.textContent=liveText; liveMatches=[]; $('livematch').textContent='';
  }else{
    const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi');
    let i=0;
    b.innerHTML=esc(liveText).replace(new RegExp(rx.source,'gi'),
      m=>`<mark data-i="${i++}">${m}</mark>`);
    liveMatches=[...b.querySelectorAll('mark')];
    if(liveIdx>=liveMatches.length) liveIdx=0;
    $('livematch').textContent=liveMatches.length?`${liveIdx+1}/${liveMatches.length}`:'0';
    liveMatches.forEach((m,n)=>m.classList.toggle('cur',n===liveIdx));
  }
  if(livePinned){ b.scrollTop=b.scrollHeight; $('jump').style.display='none'; }
}
function liveFind(){ liveIdx=0; liveRender();
  if(liveMatches.length){ livePinned=false; liveMatches[0].scrollIntoView({block:'center'}); } }
function liveStep(d){
  if(!liveMatches.length) return;
  liveIdx=(liveIdx+d+liveMatches.length)%liveMatches.length;
  livePinned=false; liveRender();
  liveMatches[liveIdx]?.scrollIntoView({block:'center',behavior:'smooth'});
}
async function liveLoad(){
  if(!$('live').classList.contains('open')) return;
  const src=$('live_src').value; if(!src) return;
  try{
    const j=await (await fetch(`/api/logs?src=${encodeURIComponent(src)}&lines=400`)).json();
    if(j.text!==liveText){ liveText=j.text; liveRender(); }
    $('livefoot').textContent=(j.path||'').split('/').slice(-2).join('/')+(j.lines?` · ${j.lines} lines`:'');
  }catch(e){ $('livefoot').textContent='live log unavailable: '+e; }
}

/* ---------- fast hover tooltip for truncated event data ----------
   Not the native `title` attribute — browsers hold that back ~1-1.5s before showing it,
   which reads as "hover does nothing" for something meant to be the primary way to read a
   truncated line. This shows in ~100ms and follows the hovered element, reading the full
   text from data-full (set in evList()) rather than relying on the browser's own tooltip
   timing at all. */
const evTip=document.createElement('div');
evTip.id='evTip';
document.body.appendChild(evTip);
let evTipTimer=null;
document.addEventListener('mouseover',e=>{
  const el=e.target.closest('.ev .d[data-full]');
  if(!el) return;
  clearTimeout(evTipTimer);
  evTipTimer=setTimeout(()=>{
    evTip.textContent=el.dataset.full;
    const r=el.getBoundingClientRect();
    evTip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-420))+'px';
    evTip.style.top=(r.bottom+6)+'px';
    evTip.classList.add('show');
  },100);
});
document.addEventListener('mouseout',e=>{
  if(!e.target.closest('.ev .d[data-full]')) return;
  clearTimeout(evTipTimer);
  evTip.classList.remove('show');
});

/* ---------- live state: SSE, not a 3s poll ----------
   /api/stream holds one connection open and pushes a fresh snapshot every ~1s — render()
   runs on every push instead of waiting on a fixed browser-side interval. EventSource
   reconnects on a drop by itself; there's no manual retry/backoff to write. tick() still
   exists, on its own slower interval below, but only for the housekeeping that used to
   piggyback on the same timer as the state fetch — the raw-logs pane and the live-log
   dock, neither of which needs push freshness. */
function startLiveStream(){
  if(!window.EventSource){
    // Only for a browser old enough to lack EventSource entirely — not the reconnect
    // path, which EventSource already handles on its own.
    tick(); setInterval(tick, 3000);
    return;
  }
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    try{ render(JSON.parse(e.data)); }
    catch(err){ /* one malformed push is not worth surfacing — the next one fixes it */ }
  };
  es.onerror = () => {
    if(es.readyState === EventSource.CONNECTING){
      $('daemonPill').className='pill warnp'; $('daemonPill').textContent='Reconnecting…';
    }else if(es.readyState === EventSource.CLOSED){
      $('daemonPill').className='pill bad'; $('daemonPill').textContent='UI lost the server';
    }
  };
}

async function tick(){
  if($('p_logs').classList.contains('on') && c('log_auto')) loadLog();
  liveLoad();
}
startLiveStream();
tick(); setInterval(tick, 3000);

$('ask').onclick=async()=>{
  const q=v('q'); if(!q) return;
  $('ask').disabled=true;
  try{ await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({question:q})}); $('q').value=''; await tick(); }
  finally{ $('ask').disabled=false; }
};
