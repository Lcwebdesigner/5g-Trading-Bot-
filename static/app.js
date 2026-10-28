"use strict";
/* 5G Trading Bot front end. It only DISPLAYS what the server says and sends the user's requests.
   Every limit and safety rule is enforced on the server, never here. Server text is always inserted with
   textContent (never innerHTML). */
(function () {
  const $ = (id) => document.getElementById(id);
  let last = null;            // latest state from the server
  let activeSlider = null;    // slider the user is touching right now (do not overwrite it)
  let timer = null;
  let toastTimer = null;

  // ------------------------------------------------------------------ helpers
  function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
  function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  function toast(text) {
    const t = $("toast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
  }

  async function api(path, body) {
    try {
      const res = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers: body === undefined ? {} : { "Content-Type": "application/json", "X-Requested-With": "5g" },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin", cache: "no-store",
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* non-JSON reply */ }
      return { ok: res.ok, status: res.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { message: "Cannot reach the server. Check your internet connection." } };
    }
  }

  async function act(path, body, quiet) {
    const r = await api(path, body);
    if (r.status === 401) { await refresh(); return r; }
    if (!r.ok || !quiet) toast(r.data.message || (r.ok ? "Done." : "That did not work."));
    await refresh();
    return r;
  }

  const money = (v, cur) => (v === null || v === undefined || v === "" || isNaN(Number(v))) ? "—"
    : Number(v).toFixed(2) + (cur ? " " + cur : "");
  const signed = (v) => (Number(v) > 0 ? "+" : "") + Number(v).toFixed(2);
  function secs(n) { return n < 60 ? n + "s" : Math.floor(n / 60) + "m" + (n % 60 ? " " + (n % 60) + "s" : ""); }
  function clock(n) { const m = Math.floor(n / 60), s = n % 60; return m + ":" + String(s).padStart(2, "0"); }

  // ------------------------------------------------------------------ render
  const STATE_TEXT = { READY: "READY", STOPPED: "STOPPED", RUNNING: "RUNNING", IN_TRADE: "IN TRADE",
    PAUSED: "PAUSED", LIMIT: "LIMIT REACHED", EXPIRED: "SIGN IN AGAIN" };
  const FEED_TEXT = { CONNECTED: "Connected", CONNECTING: "Connecting…", RETRYING: "Reconnecting…",
    FAILED: "Feed failed", DISCONNECTED: "Offline" };

  function render(s) {
    last = s;
    const signedIn = !!s.authenticated;
    $("login").hidden = signedIn;
    $("main").hidden = !signedIn;
    if (!signedIn) {
      const note = $("loginNote");
      note.hidden = s.configured !== false;
      if (!note.hidden) note.textContent = "This server is not fully set up yet, so sign-in is unavailable.";
      $("ackModal").hidden = true;
      return;
    }
    const real = s.mode === "real";
    $("balanceText").textContent = s.balance !== null && s.balance !== undefined
      ? money(s.balance, s.currency) : (s.account_status === "VERIFIED" ? "Loading…" : "—");
    $("modeTag").textContent = real ? "REAL" : "DEMO";
    const banner = $("banner");
    banner.className = "banner " + (real ? "real" : "demo");
    banner.textContent = real ? "REAL MONEY ACCOUNT — you can lose your stake" : "DEMO ACCOUNT — virtual money";

    const m = s.market || {};
    $("liveDot").className = "dot" + (m.streaming ? " live" : "");
    $("feedText").textContent = m.streaming ? "Live" : (FEED_TEXT[m.feed_state] || "Offline");
    $("priceText").textContent = m.price === null || m.price === undefined ? "—" : Number(m.price).toFixed(2);
    $("acctText").textContent = "Account: " + (s.account_id || "—") + " (" + (real ? "real" : "demo") + ") · "
      + s.account_status.toLowerCase();

    const b = s.bot;
    const st = $("stateTag");
    st.textContent = STATE_TEXT[b.state] || b.state;
    st.className = "state " + b.state;
    const play = $("playBtn");
    play.classList.toggle("running", b.running);
    play.disabled = b.state === "EXPIRED" || b.state === "LIMIT";
    $("playIcon").innerHTML = b.running ? '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>'
      : '<path d="M8 5v14l11-7z" fill="currentColor"/>';
    play.setAttribute("aria-label", b.running ? "Stop the bot" : "Start the bot");

    // settings sliders (leave the one being dragged alone)
    const cap = Math.min(b.stake_cap, 20);
    setSlider("freq", b.frequency, null, null, b.running);
    setSlider("amount", b.stake, b.min_stake, cap, b.running);
    setSlider("risk", b.risk_level, null, null, b.running);
    $("freqOut").textContent = secs(Number($("freq").value));
    $("amountOut").textContent = "$" + Number($("amount").value).toFixed(2);
    $("riskOut").textContent = $("risk").value + "/5";
    $("amountHint").textContent = "The same amount every trade, never raised after a loss. Largest allowed in "
      + (real ? "real" : "demo") + " mode: " + b.stake_cap + ".";
    updateRiskHint(s);

    // signals
    const sig = $("signalText");
    const map = { HIGHER: ["RISE ↑", "rise"], LOWER: ["FALL ↓", "fall"], WAIT: ["WAIT", "wait"] };
    const mm = map[m.signal] || map.WAIT;
    sig.textContent = mm[0]; sig.className = "signal " + mm[1];
    $("explainText").textContent = m.explanation || "";
    $("meterFill").style.width = Math.min(100, Math.round((m.ticks || 0) / (m.min_ticks || 100) * 100)) + "%";
    $("ticksText").textContent = (m.ticks || 0) + " / " + (m.min_ticks || 100) + " ticks collected";

    // status line + log
    const msgs = s.messages || [];
    $("statusLine").textContent = s.expired
      ? "Your sign-in expired. Open the menu and choose “Sign in again”."
      : (msgs.length ? msgs[msgs.length - 1].text : "Ready.");
    const log = $("logList");
    log.textContent = "";
    msgs.slice().reverse().forEach((x) => {
      const li = document.createElement("li");
      const t = document.createElement("time"); t.textContent = x.t;
      li.appendChild(t); li.appendChild(document.createTextNode(x.text));
      log.appendChild(li);
    });

    // limits
    const L = s.limits;
    $("limitMode").textContent = real ? "REAL" : "DEMO";
    $("limTrades").textContent = L.trades_today + "/" + L.max_trades;
    $("limLosses").textContent = L.losses + "/" + L.max_losses;
    const pnl = $("limPnl");
    pnl.textContent = signed(L.pnl_today);
    pnl.className = "big " + (L.pnl_today < 0 ? "neg" : L.pnl_today > 0 ? "pos" : "");
    $("limitNote").textContent = (b.halted ? "A safety limit stopped trading for today. It resets at midnight (Nairobi time). " : "")
      + (L.loss_stop ? "Trading also stops if today's net loss reaches " + L.loss_stop.toFixed(2) + ". " : "")
      + "These limits are enforced on the server.";

    // recent trades
    const list = $("recentList");
    list.textContent = "";
    if (!s.recent.length) {
      const li = document.createElement("li"); li.className = "empty"; li.textContent = "No trades yet in this mode.";
      list.appendChild(li);
    }
    s.recent.forEach((r) => {
      const li = document.createElement("li");
      const cls = r.result === "WIN" ? "win" : r.result === "LOSS" ? "loss" : "tie";
      li.className = cls;
      const ico = document.createElement("span"); ico.className = "ico";
      ico.textContent = cls === "win" ? "✓" : cls === "loss" ? "✕" : "–";
      const txt = document.createElement("span");
      const word = cls === "win" ? "won" : cls === "loss" ? "lost" : "tied";
      txt.textContent = "Auto trade " + word + ": " + signed(r.profit || 0) + " (stake " + money(r.stake) + ")";
      const when = document.createElement("span"); when.className = "when"; when.textContent = r.time || "";
      li.appendChild(ico); li.appendChild(txt); li.appendChild(when);
      list.appendChild(li);
    });
    const S = s.summary;
    $("summaryText").textContent = S.n ? ("Since sign-up on this server: " + S.n + " trades · W " + S.wins + " / L " + S.losses
      + " · net " + signed(S.net) + (S.win_rate === null ? "" : " · win rate " + S.win_rate + "%")) : "";

    $("footText").textContent = s.expired ? "Signed out by expiry — sign in again to continue."
      : "Your sign-in lasts about an hour. Time left: " + clock(s.expires_in) + ". The bot stops when it ends.";

    // account sheet
    $("sheetAccount").textContent = "Signed in · " + (s.account_id || "connecting…");
    const segD = $("segDemo"), segR = $("segReal");
    segD.classList.toggle("active", !real);
    segR.classList.toggle("active", real);
    segR.classList.add("real");
    segD.disabled = !s.has_demo;
    segR.disabled = !s.has_real || !s.allow_real;
    $("segNote").textContent = !s.allow_real ? "Real-money trading is switched off on this server."
      : !s.has_real ? "You have no real Deriv account yet. Open one at deriv.com."
      : b.in_trade ? "Wait for the open trade to finish before switching." : "";
    $("logoutBtn").textContent = s.expired ? "Sign in again" : "Sign out";

    $("ackModal").hidden = !!s.acknowledged;
  }

  function setSlider(id, value, min, max, disabled) {
    const el = $(id);
    if (min !== null) el.min = String(min);
    if (max !== null) el.max = String(max);
    el.disabled = disabled;
    if (activeSlider !== id) el.value = String(value);
  }

  function updateRiskHint(s) {
    const level = $("risk").value;
    const row = (s.risk_table || {})[level];
    $("riskHint").textContent = row ? ("Level " + level + ": at most " + row[0] + " trades a day, and trading stops for the day after "
      + row[1] + " loss" + (row[1] > 1 ? "es" : "") + " in a row. Higher levels allow more trades, not bigger ones.") : "";
  }

  // ------------------------------------------------------------------ polling
  async function refresh() {
    const r = await api("/api/state");
    if (r.status === 0) { toast(r.data.message); return; }
    if (r.data && typeof r.data === "object") render(r.data);
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => { await refresh(); schedule(); }, document.hidden ? 5000 : 1000);
  }

  // ------------------------------------------------------------------ events
  function sendConfig() {
    activeSlider = null;
    act("/api/config", { frequency: Number($("freq").value), stake: Number($("amount").value),
      risk_level: Number($("risk").value) }, true);
  }
  ["freq", "amount", "risk"].forEach((id) => {
    const el = $(id);
    el.addEventListener("pointerdown", () => { activeSlider = id; });
    el.addEventListener("focus", () => { activeSlider = id; });
    el.addEventListener("blur", () => { if (activeSlider === id) activeSlider = null; });
    el.addEventListener("input", () => {
      $("freqOut").textContent = secs(Number($("freq").value));
      $("amountOut").textContent = "$" + Number($("amount").value).toFixed(2);
      $("riskOut").textContent = $("risk").value + "/5";
      if (last) updateRiskHint(last);
    });
    el.addEventListener("change", sendConfig);
  });

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    ["config", "strategies", "signals"].forEach((n) => { $("panel-" + n).hidden = n !== tab.dataset.tab; });
  }));

  $("playBtn").addEventListener("click", () => {
    if (!last) return;
    if (last.bot.running) { act("/api/bot", { action: "stop" }); return; }
    if (last.mode === "real") {
      const b = last.bot, L = last.limits;
      $("startText").textContent = "The bot will place REAL-money trades automatically. Amount per trade: "
        + Number(b.stake).toFixed(2) + " " + last.currency + ". Limits: " + L.max_trades + " trades a day, stop after "
        + L.max_losses + " loss(es) in a row" + (L.loss_stop ? ", stop at a net loss of " + L.loss_stop.toFixed(2) : "")
        + ". You can lose this money.";
      $("startModal").hidden = false;
      return;
    }
    act("/api/bot", { action: "start" });
  });
  $("startCancel").addEventListener("click", () => { $("startModal").hidden = true; });
  $("startOk").addEventListener("click", async () => {
    $("startModal").hidden = true;
    await act("/api/bot", { action: "start", confirm_real: true });
  });

  function openSheet() { $("sheet").hidden = false; }
  $("menuBtn").addEventListener("click", openSheet);
  $("balancePill").addEventListener("click", openSheet);
  $("sheetClose").addEventListener("click", () => { $("sheet").hidden = true; });
  $("sheet").addEventListener("click", (e) => { if (e.target === $("sheet")) $("sheet").hidden = true; });
  $("historyBtn").addEventListener("click", () => $("recentCard").scrollIntoView({ behavior: "smooth", block: "start" }));

  $("segDemo").addEventListener("click", async () => {
    if (last && last.mode === "demo") return;
    const r = await act("/api/mode", { mode: "demo" });
    if (r.ok) $("sheet").hidden = true;
  });
  $("segReal").addEventListener("click", () => {
    if (!last || last.mode === "real") return;
    const L = last.risk_table[String(last.bot.risk_level)] || [0, 0];
    $("realLimits").textContent = "Real mode: amount up to " + last.bot.stake_cap + " per trade, at your risk level "
      + last.bot.risk_level + " (" + L[0] + " trades a day, stop after " + L[1] + " loss(es) in a row).";
    $("realInput").value = ""; $("realErr").hidden = true;
    $("sheet").hidden = true; $("realModal").hidden = false;
    $("realInput").focus();
  });
  $("realCancel").addEventListener("click", () => { $("realModal").hidden = true; });
  $("realOk").addEventListener("click", async () => {
    const r = await api("/api/mode", { mode: "real", confirm: $("realInput").value });
    if (r.ok) { $("realModal").hidden = true; toast(r.data.message); await refresh(); }
    else { $("realErr").textContent = r.data.message || "That did not work."; $("realErr").hidden = false; }
  });

  $("reconnectBtn").addEventListener("click", () => act("/api/reconnect", {}));
  $("logoutBtn").addEventListener("click", async () => {
    const wasExpired = last && last.expired;
    await api("/api/logout", {});
    $("sheet").hidden = true;
    if (wasExpired) { window.location.href = "/login"; return; }
    await refresh();
  });

  $("ackBox").addEventListener("change", () => { $("ackBtn").disabled = !$("ackBox").checked; });
  $("ackBtn").addEventListener("click", async () => {
    const r = await act("/api/ack", { accept: $("ackBox").checked }, true);
    if (r.ok) { $("ackBox").checked = false; $("ackBtn").disabled = true; }
  });

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  }
  applyTheme(load("5g-theme"));
  $("themeBtn").addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark"
      || (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    applyTheme(next); store("5g-theme", next);
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); schedule(); } });
  refresh().then(schedule);
})();
