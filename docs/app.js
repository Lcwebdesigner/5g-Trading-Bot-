const $ = id => document.getElementById(id);
let socket = null, prices = [], lastPrice = null, recent = [];
const savedAppId = localStorage.getItem("5g_deriv_app_id");
if (savedAppId) $("appid").value = savedAppId;
function setConnection(ok, text) {
  $("connection").classList.toggle("connected", ok);
  $("connection").innerHTML = `<i></i> ${text}`;
}
function ema(values, period) {
  if (values.length < period) return null;
  let v = values.slice(0, period).reduce((a,b)=>a+b,0)/period;
  const alpha = 2/(period+1);
  for (const p of values.slice(period)) v = alpha*p+(1-alpha)*v;
  return v;
}
function emaSeries(values, period) {
  if (values.length < period) return [];
  let v=values.slice(0,period).reduce((a,b)=>a+b,0)/period, out=[v], alpha=2/(period+1);
  for (const p of values.slice(period)) { v=alpha*p+(1-alpha)*v; out.push(v); }
  return out;
}
function rsi(values, period=14) {
  if(values.length<period+1)return null;
  const changes=values.slice(1).map((v,i)=>v-values[i]);
  const first=changes.slice(0,period);
  let gain=first.reduce((s,x)=>s+Math.max(x,0),0)/period;
  let loss=first.reduce((s,x)=>s+Math.max(-x,0),0)/period;
  for(const c of changes.slice(period)){gain=(gain*(period-1)+Math.max(c,0))/period;loss=(loss*(period-1)+Math.max(-c,0))/period;}
  if(loss===0)return gain>0?100:50;
  if(gain===0)return 0;
  return 100-100/(1+gain/loss);
}
function macd(values, fast=12, slow=26, signal=9) {
  if(values.length<slow+signal-1)return [null,null];
  const f=emaSeries(values,fast).slice(slow-fast), s=emaSeries(values,slow);
  const line=f.map((v,i)=>v-s[i]), sig=emaSeries(line,signal);
  return sig.length?[line[line.length-1],sig[sig.length-1]]:[null,null];
}
function fmt(v){return v===null||!Number.isFinite(v)?"—":v.toFixed(4)}
function render(price, epoch) {
  const fast=ema(prices,9), slow=ema(prices,21), rv=rsi(prices,14), [ml,ms]=macd(prices);
  $("price").textContent=price.toFixed(2);
  $("tickCount").textContent=`${prices.length} / 100 ticks`;
  $("progress").style.width=`${Math.min(100,prices.length)}%`;
  $("ema9").textContent=fmt(fast); $("ema21").textContent=fmt(slow);
  $("macd").textContent=ml===null?"—":`${ml.toFixed(4)} / ${ms.toFixed(4)}`;
  $("rsi").textContent=fmt(rv);
  let signal="WAIT", explanation="Collecting at least 100 ticks before evaluating the indicators.";
  if(prices.length>=100 && [fast,slow,rv,ml,ms].every(v=>v!==null)){
    if(fast>slow && ml>ms && rv>=50 && rv<70){signal="HIGHER";explanation=`EMA bullish; MACD above signal; RSI ${rv.toFixed(1)}. Research signal only.`;}
    else if(fast<slow && ml<ms && rv>30 && rv<=50){signal="LOWER";explanation=`EMA bearish; MACD below signal; RSI ${rv.toFixed(1)}. Research signal only.`;}
    else explanation=`Indicators do not fully agree (RSI ${rv.toFixed(1)}). No signal.`;
  }
  $("signal").textContent=signal; $("signal").className="signal "+(signal==="HIGHER"?"higher":signal==="LOWER"?"lower":"wait");
  $("explanation").textContent=explanation;
  const time=new Date(epoch*1000).toLocaleTimeString();
  recent.unshift({time,price,change:lastPrice===null?null:price-lastPrice});
  recent=recent.slice(0,8);
  $("ticks").innerHTML=recent.map(t=>`<tr><td>${t.time}</td><td>${t.price.toFixed(2)}</td><td class="${t.change===null?"":t.change>=0?"up":"down"}">${t.change===null?"—":`${t.change>=0?"+":""}${t.change.toFixed(2)}`}</td></tr>`).join("");
  lastPrice=price;
}
$("connectBtn").addEventListener("click",()=>{
  const appId=$("appid").value.trim();
  if(!/^\d+$/.test(appId)){alert("Enter the numeric Deriv App ID from your application settings.");return;}
  localStorage.setItem("5g_deriv_app_id",appId);
  if(socket){try{socket.close();}catch(e){}}
  prices=[];recent=[];lastPrice=null;
  $("ticks").innerHTML='<tr><td colspan="3" class="muted">Connecting to market feed…</td></tr>';
  setConnection(false,"Connecting…");
  try {
    socket=new WebSocket(`wss://ws.derivws.com/websockets/v3?app_id=${encodeURIComponent(appId)}`);
    socket.onopen=()=>{setConnection(true,"Live feed connected");socket.send(JSON.stringify({ticks:"R_100",subscribe:1}));};
    socket.onmessage=(event)=>{
      const msg=JSON.parse(event.data);
      if(msg.error){setConnection(false,"Feed error");$("explanation").textContent=msg.error.message||"Unable to connect. Check the App ID.";return;}
      if(msg.tick){const p=Number(msg.tick.quote);if(Number.isFinite(p)){prices.push(p);if(prices.length>500)prices.shift();render(p,msg.tick.epoch);}}
    };
    socket.onerror=()=>setConnection(false,"Connection error");
    socket.onclose=()=>setConnection(false,"Disconnected");
  }catch(e){setConnection(false,"Connection error");}
});
window.addEventListener("beforeunload",()=>{if(socket)socket.close();});