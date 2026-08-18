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

document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('nav a').forEach(x=>x.classList.remove('on'));
  a.classList.add('on');
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
  $('p_'+a.dataset.pane).classList.add('on');
  if(a.dataset.pane==='logs') loadLog();
});

async function run(cmd,params={},confirm=null){
  $('out').textContent='running '+cmd+'…'; $('out').className='dim';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd,params,confirm})});
    const j=await r.json();
    $('out').textContent=(j.ok?'✓ ':'✗ ')+(j.argv||cmd)+'\n\n'+j.output;
    $('out').className=j.ok?'':'down';
    // The output panel used to sit below the fold, so a command that ran fine but
    // reported a refusal looked like a dead button. Always bring it into view.
    $('out').scrollIntoView({behavior:'smooth', block:'nearest'});
    if(cmd==='start'){
      // `start` returns as soon as the child is spawned; the child may exit immediately
      // (market closed). Re-check after a beat and say so plainly.
      setTimeout(async()=>{
        const s=await (await fetch('/api/state')).json();
        if(!s.daemon.running){
          $('out').textContent += '\n\n⚠ The daemon exited straight after starting'
            + (s.market_open ? '.' : ' — the market is CLOSED (now '+s.now+').')
            + '\nUse `vigil monitor --force` in a terminal to run outside market hours.';
          $('out').className='down';
        }
      }, 1500);
    }
  }catch(e){ $('out').textContent='request failed: '+e; $('out').className='down'; }
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

function posTable(s){
  return (s.positions.length ? `<table><tr>
    <th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>LTP</th><th>R</th><th>P&L</th><th>Phase</th><th>Stop</th></tr>`+
    s.positions.map(p=>`<tr><td>${p.symbol}</td><td class="dim">${p.direction}</td><td>${p.qty}</td>
      <td>${n(p.entry)}</td><td>${n(p.ltp)}</td><td class="${sign(p.profit_r)}">${n(p.profit_r)}</td>
      <td class="${sign(p.unrealized_pnl)}">${n(p.unrealized_pnl)}</td><td>P${p.phase}</td>
      <td class="${p.protected===false?'down':''}">${p.protected===false?'NO STOP':n(p.sl_price)}</td></tr>`).join('')
    +`</table>` : '<span class="dim">No open positions.</span>')
    + (s.closed_today.length?`<table style="margin-top:10px"><tr><th>Closed</th><th>Reason</th><th>Exit</th><th>R</th><th>P&L</th></tr>`+
       s.closed_today.map(x=>`<tr><td>${x.symbol}</td><td class="dim">${x.exit_reason}</td>
       <td>${n(x.exit_price)}</td><td class="${sign(x.realized_r)}">${n(x.realized_r)}</td>
       <td class="${sign(x.realized_pnl)}">${n(x.realized_pnl)}</td></tr>`).join('')+`</table>`:'');
}
function trigTable(s){
  return s.triggers.length ? `<table><tr>
    <th>Symbol</th><th>Dir</th><th>Side</th><th>Level</th><th>Qty</th><th>SL%</th><th>Auto</th><th>Status</th></tr>`+
    s.triggers.map(t=>`<tr><td>${t.symbol}</td><td class="dim">${t.direction}</td><td>${t.side}</td>
      <td>${n(t.level)}</td><td>${t.qty}</td><td>${n(t.sl_pct*100)}%</td>
      <td>${t.auto?'<span class="pill warnp">AUTO</span>':'alert'}</td><td class="dim">${t.status}</td></tr>`).join('')
    +`</table>` : '<span class="dim">Nothing armed.</span>';
}
function evList(s,limit){
  const e=s.events.slice(0,limit);
  return e.length ? e.map(x=>`<div class="ev"><span class="dim">${x.ts}</span> <b>${x.type}</b> ${x.symbol||''}
       <span class="dim">${esc(x.data)}</span></div>`).join('') : '<span class="dim">No events yet.</span>';
}

const money=x=>x==null?'—':'₹'+Number(x).toLocaleString('en-IN',{maximumFractionDigits:0});

function renderAccount(a){
  if(!a || !a.ok){
    $('acct').innerHTML=`<span class="pill bad">KITE TOKEN</span> <span class="dim">${a?esc(a.error||'unavailable'):'…'}</span>`;
    $('acct_full').innerHTML=`<span class="down">Cannot read the account: ${a?esc(a.error||''):'…'}</span>
      <div class="hint" style="margin-top:6px">The daemon token dies around 06:00 daily.
      Re-auth with <code>vigil login</code>.</div>`;
    $('acct_funds').innerHTML='<span class="dim">—</span>';
    return;
  }
  // Header: the two numbers that decide whether you can trade at all.
  $('acct').innerHTML=
    `<b>${esc(a.client_id||'?')}</b> <span class="dim">${esc(a.name||'')}</span>`
    + ` · avail <b class="up">${money(a.available)}</b>`
    + ` · used <b>${money(a.used)}</b>`
    + (a.m2m_unrealised!=null
        ? ` · M2M <b class="${sign(a.m2m_unrealised)}">${money(a.m2m_unrealised)}</b>` : '');

  $('acct_full').innerHTML=`<table>
    <tr><td>Client ID</td><td><b>${esc(a.client_id||'—')}</b></td></tr>
    <tr><td>Name</td><td>${esc(a.name||'—')}</td></tr>
    <tr><td>Broker</td><td>${esc(a.broker||'—')}</td></tr>
    <tr><td>Email</td><td>${esc(a.email||'—')}</td></tr>
    <tr><td>Exchanges</td><td>${esc((a.exchanges||[]).join(', ')||'—')}</td></tr>
    <tr><td>Products</td><td>${esc((a.products||[]).join(', ')||'—')}</td></tr></table>`;

  const used=Number(a.used||0), net=Number(a.net||0), total=used+Number(a.available||0);
  const pct=total?Math.round(used/total*100):0;
  $('acct_funds').innerHTML=`<table>
    <tr><td>Opening balance</td><td>${money(a.opening)}</td></tr>
    <tr><td>Net</td><td>${money(a.net)}</td></tr>
    <tr><td>Available to trade</td><td class="up"><b>${money(a.available)}</b></td></tr>
    <tr><td>Used (margin blocked)</td><td>${money(a.used)} <span class="dim">${pct}% deployed</span></td></tr>
    <tr><td>M2M unrealised</td><td class="${sign(a.m2m_unrealised)}">${money(a.m2m_unrealised)}</td></tr>
    <tr><td>M2M realised</td><td class="${sign(a.m2m_realised)}">${money(a.m2m_realised)}</td></tr>
    </table>
    <div class="hint" style="margin-top:8px">Used is margin blocked, not money at risk —
    MIS is ~5x, so notional exposure is roughly 5x this figure. Refreshes every 20s.</div>`;
}

function render(s){
  $('ver').textContent='v'+s.ui_version;
  renderAccount(s.account);
  if(s.ui_version!==BUILT){
    const b=$('jserr'); b.style.display='block';
    b.textContent=`This page is v${BUILT} but the server is v${s.ui_version} — hard-reload (Cmd-Shift-R).`;
  }
  const d=s.daemon;
  const mkt = s.market_open
    ? `<span class="pill ok">MARKET OPEN</span>`
    : `<span class="pill warnp">MARKET CLOSED</span>`;
  $('daemon').innerHTML = (d.running
    ? `<span class="pill ${d.fresh?'ok':'warnp'}">${d.fresh?'LIVE':'STALE'}</span> pid ${d.pid} · ${d.mode} · ${d.age_s}s ago`
    : `<span class="pill bad">DAEMON NOT RUNNING</span>`)
    + ` · ${mkt} ${s.now} · day <b class="${sign(s.realized_pnl_today)}">₹${n(s.realized_pnl_today)}</b>`;

  let al='';
  if(!d.running && !s.market_open)
    al+=`<div class="stale">Daemon is not running, and the market is CLOSED (${s.now}).
         <b>start</b> will launch it and it will exit immediately — that is expected, not a broken button.
         To run anyway: <code>vigil monitor --force</code>.</div>`;
  else if(!d.running) al+=`<div class="banner">Daemon is not running — no SL management and no auto square-off.</div>`;
  else if(!d.fresh) al+=`<div class="stale">Snapshot is ${d.age_s}s old — treat these numbers as stale.</div>`;
  const naked=s.positions.filter(p=>p.protected===false);
  for(const p of naked)
    al+=`<div class="banner">${p.symbol} UNPROTECTED — ${p.qty} ${p.direction}, SL order ${p.sl_order_status}.</div>`;
  if(s.kill_switch) al+=`<div class="banner">KILL SWITCH — day at ${n(s.realized_r_today)}R. No new entries.</div>`;
  $('alerts').innerHTML=al;
  const nb=$('nb_pos');
  if(naked.length){ nb.style.display='inline'; nb.textContent='!' } else nb.style.display='none';

  $('pos').innerHTML=posTable(s); $('pos2').innerHTML=posTable(s);
  $('trig').innerHTML=trigTable(s); $('trig2').innerHTML=trigTable(s);
  $('events_s').innerHTML=evList(s,12); $('events_full').innerHTML=evList(s,200);

  if(!$('log_src').options.length){
    const opts=s.log_sources.map(x=>`<option>${x}</option>`).join('');
    $('log_src').innerHTML=opts;
    $('live_src').innerHTML=opts;
    $('live_src').value=localStorage.getItem('liveSrc')||'algo.log';
    if(localStorage.getItem('liveLeft')==='1') $('live').classList.add('left');
    if(localStorage.getItem('liveOpen')==='1') liveToggle(true);
  }
  // Pulse the rail dot only while the daemon is actually cycling.
  $('raildot').style.background = d.running && d.fresh ? 'var(--up)' : 'var(--dim)';
  $('modes').innerHTML = s.skill_modes.map(m=>`<button onclick="askMode('${m}')">/${m}</button>`).join('');
  $('mode').textContent = s.claude.cli ? `CLI: ${s.claude.cli}`
    : 'No claude CLI — questions queue for a Claude session';
  $('qa').innerHTML = s.claude.recent.map(r=>`<div class="qa">
     <div class="q">${esc(r.question)}</div>
     <div class="a">${r.status==='pending'?'<span class="dim">queued — waiting for a Claude session</span>':esc(r.answer||'')}</div>
     </div>`).join('');
}

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

async function tick(){
  try{
    const r=await fetch('/api/state');
    render(await r.json());
  }catch(e){
    $('daemon').innerHTML='<span class="pill bad">UI lost the server</span>';
  }
  if($('p_logs').classList.contains('on') && c('log_auto')) loadLog();
  liveLoad();
}
$('ask').onclick=async()=>{
  const q=v('q'); if(!q) return;
  $('ask').disabled=true;
  try{ await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({question:q})}); $('q').value=''; await tick(); }
  finally{ $('ask').disabled=false; }
};
tick(); setInterval(tick,3000);
