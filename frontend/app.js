/* ── State ─────────────────────────────────────────────────────────────────── */
let oauthPopup     = null;
let searchMultiMode = false;
let chatMultiMode   = false;
const _multiSelections = {};

/* ── Helpers ───────────────────────────────────────────────────────────────── */
function getApiBase() { return window.location.origin + "/api"; }
function bust(url)    { return url + (url.includes("?") ? "&" : "?") + "_ts=" + Date.now(); }

function setStatus(msg, type) {
  const s = document.getElementById("status");
  const p = document.getElementById("pill");
  s.textContent  = msg;
  s.className    = "status-bar" + (type ? " " + type : "");
  p.textContent  = msg.length > 24 ? msg.slice(0, 24) + "…" : msg;
  p.className    = "pill" + (type ? " " + type : "");
}

function esc(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function stripSlack(t) {
  return (t || "")
    .replace(/<https?:\/\/[^|>]+\|([^>]+)>/g, "$1")
    .replace(/<https?:\/\/[^>]+>/g, "[link]")
    .replace(/[*_`]/g, "");
}

async function safeJson(r) {
  const text = await r.text();
  try { return JSON.parse(text); } catch { return { ok: false, raw: text, status: r.status }; }
}

async function fetchJson(url, options) {
  const res = await fetch(bust(url), Object.assign({ cache: "no-store", credentials: "include" }, options || {}));
  return safeJson(res);
}

/* ── Output renderer ───────────────────────────────────────────────────────── */
function show(obj) {
  const el = document.getElementById("out");
  if (!obj) { el.innerHTML = ""; return; }

  function tag(el, cls, html)     { return `<${el} class="${cls}">${html}</${el}>`; }
  function badge(txt, color)      { return `<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:600;font-family:'DM Mono',monospace;background:${color}22;color:${color};border:1px solid ${color}44;">${esc(txt)}</span>`; }

  function msgCard(m, idx) {
    const name = esc(m.username || m.user_id || "unknown");
    const time = esc(m.timestamp_human || m.ts || m.sk || "");
    const ch   = m.channel_id ? `<span style="color:var(--accent);font-size:10px;">#${esc(m.channel_id)}</span>` : "";
    const text = esc(stripSlack(m.text || m.snippet || "")).replace(/\n/g, "<br>");
    const num  = idx != null ? `<span style="color:var(--text-muted);font-size:10px;font-family:'DM Mono',monospace;flex-shrink:0;">[${idx + 1}]</span>` : "";
    return `
      <div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;background:var(--surface);">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
          ${num}
          <span style="font-size:12px;font-weight:600;color:var(--text);">${name}</span>
          ${ch}
          <span style="font-size:11px;color:var(--text-muted);margin-left:auto;">${time}</span>
        </div>
        <div style="font-size:13px;color:var(--text-soft);line-height:1.55;">${text || '<em style="color:var(--text-muted);">no text</em>'}</div>
      </div>`;
  }

  function statRow(label, val) {
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:12px;color:var(--text-muted);">${esc(label)}</span>
      <span style="font-size:12px;font-weight:600;color:var(--text);font-family:'DM Mono',monospace;">${esc(String(val))}</span>
    </div>`;
  }

  function section(title, content) {
    return `<div style="margin-bottom:16px;">
      <div style="font-size:10px;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;">${title}</div>
      ${content}
    </div>`;
  }

  /* error */
  if (obj.ok === false || obj.error) {
    el.innerHTML = `<div style="padding:12px;border-radius:8px;background:var(--danger-bg);border:1px solid rgba(224,122,138,0.3);">
      <div style="font-size:12px;font-weight:700;color:var(--danger);margin-bottom:4px;">Error</div>
      <div style="font-size:12px;color:var(--text-soft);font-family:'DM Mono',monospace;">${esc(obj.error || obj.slack_error?.error || JSON.stringify(obj))}</div>
    </div>`;
    return;
  }

  let html = "";

  /* DB messages */
  if (obj.items && Array.isArray(obj.items)) {
    const items = obj.items.filter(m => !(m.subtype === "channel_join" || m.subtype === "channel_leave"));
    const joins = obj.items.length - items.length;
    html += section("Messages from DB",
      `<div style="font-size:11px;color:var(--text-muted);margin-bottom:10px;">Showing <strong>${items.length}</strong> messages`
      + (joins ? ` <span style="color:var(--text-muted);">(${joins} join/leave events hidden)</span>` : "")
      + `</div>`
      + (items.length ? items.map((m, i) => msgCard(m, i)).join("") : `<div style="color:var(--text-muted);font-size:13px;">No messages found.</div>`)
    );
  }
  /* search results */
  else if (obj.messages && Array.isArray(obj.messages)) {
    const filters = obj.filters || {};
    const filterBadges = [
      filters.username && badge("@" + filters.username, "#0d9488"),
      filters.from     && badge("from " + filters.from, "#2563eb"),
      filters.to       && badge("to " + filters.to, "#2563eb"),
      obj.query        && badge('"' + obj.query + '"', "#5b4fcf"),
    ].filter(Boolean).join(" ");

    html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
      <span style="font-size:13px;font-weight:600;color:var(--text);">${obj.count || 0} result${obj.count === 1 ? "" : "s"}</span>
      ${obj.channels_searched > 1 ? badge(obj.channels_searched + " channels", "#9b7fe8") : ""}
      ${filterBadges}
    </div>`;
    html += obj.messages.length
      ? obj.messages.map((m, i) => msgCard(m, i)).join("")
      : `<div style="text-align:center;padding:24px;color:var(--text-muted);font-size:13px;">No messages matched your search.</div>`;
  }
  /* backfill / join */
  else if (obj.backfill_all || obj.join_all || obj.backfill || obj.join) {
    if (obj.join_all) {
      html += section("Join All Channels",
        statRow("Joined", obj.join_all.joined_count ?? "—") +
        statRow("Failed", obj.join_all.failed_count ?? "—")
      );
    }
    if (obj.backfill_all) {
      const res = obj.backfill_all.results || [];
      const ok  = res.filter(r => r.ok);
      html += section("Backfill Summary",
        statRow("Total stored", obj.backfill_all.total_stored ?? "—") +
        statRow("Channels processed", res.length) +
        statRow("Successful", ok.length)
      );
      if (ok.length) {
        html += section("Per-channel",
          ok.map(r => `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px;">
            <span style="color:var(--text-soft);font-family:'DM Mono',monospace;">${esc(r.channel)}</span>
            <span style="color:var(--success);font-weight:600;">${r.stored} stored</span>
          </div>`).join("")
        );
      }
    }
    if (obj.join && !obj.join_all) html += section("Channel Joined", statRow("Channel", obj.join.channel_id || "✓"));
    if (obj.backfill && !obj.backfill_all) {
      html += section("Backfill Complete",
        statRow("Stored", obj.backfill.stored ?? "—") +
        statRow("Has more", obj.backfill.has_more ? "Yes (paginate)" : "No")
      );
    }
  }
  /* workspaces */
  else if (obj.workspaces) {
    html += section("Connected Workspaces",
      obj.workspaces.length
        ? obj.workspaces.map(w => `<div style="display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:6px;background:var(--surface2);margin-bottom:6px;">
            <span style="font-size:18px;">💼</span>
            <div>
              <div style="font-size:13px;font-weight:600;color:var(--text);">${esc(w.team_name || "Unnamed")}</div>
              <div style="font-size:10px;color:var(--text-muted);font-family:'DM Mono',monospace;">${esc(w.team_id)}</div>
            </div>
          </div>`).join("")
        : `<div style="color:var(--text-muted);font-size:13px;">No workspaces connected yet.</div>`
    );
  }
  /* disconnect */
  else if (obj.ok && obj.team_id && obj.revoked !== undefined) {
    html += `<div style="padding:12px;border-radius:8px;background:rgba(109,191,160,0.08);border:1px solid rgba(109,191,160,0.3);">
      <div style="font-size:13px;font-weight:600;color:var(--success);">✓ Workspace disconnected</div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:4px;font-family:'DM Mono',monospace;">${esc(obj.team_id)}</div>
    </div>`;
  }
  /* chat answer */
  else if (obj.ok && obj.question) {
    html += _renderChatOutput(obj);
  }
  /* generic ok */
  else if (obj.ok) {
    html += `<div style="padding:10px 12px;border-radius:8px;background:rgba(109,191,160,0.08);border:1px solid rgba(109,191,160,0.3);font-size:13px;color:var(--success);">✓ Done</div>`;
  }

  el.innerHTML = html || `<div style="color:var(--text-muted);font-size:12px;">No output.</div>`;
}

/* ── Chat output builder ────────────────────────────────────────────────────── */
function _parseSection(text, label) {
  const lines       = text.split("\n");
  const allLabels   = ["answer", "key points", "action items", "citations"];
  let collecting    = false;
  const result      = [];
  for (const line of lines) {
    const ll = line.toLowerCase();
    if (ll.startsWith(label.toLowerCase() + ":")) {
      collecting = true;
      const rest = line.substring(line.indexOf(":") + 1).trim();
      if (rest) result.push(rest);
    } else if (collecting && allLabels.some(l => ll.startsWith(l + ":"))) {
      break;
    } else if (collecting) {
      result.push(line);
    }
  }
  return result.join("\n").trim();
}

function _renderChatOutput(obj) {
  let html = "";
  const raw        = obj.answer || "";
  const answerText = _parseSection(raw, "Answer") || raw;
  const keyPoints  = _parseSection(raw, "Key points");
  const actionItems= _parseSection(raw, "Action items");
  const isWarning  = raw.startsWith("⚠️");

  html += `<div style="padding:10px 12px;border-radius:8px;background:rgba(155,127,232,0.06);border:1px solid rgba(155,127,232,0.15);margin-bottom:14px;">
    <div style="font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;">Query Info</div>
    <div style="font-size:12px;color:var(--text-soft);margin-bottom:4px;">${esc(obj.question)}</div>
    <div style="display:flex;gap:12px;margin-top:8px;font-size:11px;color:var(--text-muted);">
      <span>📨 ${obj.retrieved || 0} messages retrieved</span>
      ${obj.channels_searched > 1 ? `<span>🌐 ${obj.channels_searched} channels</span>` : ""}
    </div>
  </div>`;

  if (obj.resolved_username) {
    html += `<div style="display:inline-flex;align-items:center;gap:5px;font-size:11px;color:#0d9488;padding:4px 10px;background:rgba(13,148,136,0.07);border-radius:4px;border:1px solid rgba(13,148,136,0.2);margin-bottom:8px;">
      👤 Filtering messages from <strong>@${esc(obj.resolved_username)}</strong></div>`;
  }
  if (obj.channels_searched > 1) {
    html += `<div style="font-size:11px;color:var(--accent);padding:4px 8px;background:rgba(155,127,232,0.08);border-radius:4px;border:1px solid rgba(155,127,232,0.2);margin-bottom:8px;">
      🌐 Answer synthesized from <strong>${obj.channels_searched} channels</strong> · ${obj.retrieved || 0} messages reviewed</div>`;
  }

  html += `<div style="padding:12px 14px;border-radius:8px;background:${isWarning ? "var(--danger-bg)" : "var(--surface2)"};border:1px solid ${isWarning ? "rgba(224,122,138,0.3)" : "var(--border)"};margin-bottom:12px;">
    <div style="font-size:11px;font-weight:600;color:${isWarning ? "var(--danger)" : "var(--text-muted)"};text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;">Answer</div>
    <div style="font-size:13px;color:var(--text);line-height:1.7;">${esc(answerText).split("\n").join("<br>")}</div>
  </div>`;

  if (keyPoints && keyPoints.toLowerCase() !== "none" && keyPoints.trim()) {
    const bullets = keyPoints.split("\n").map(b => b.replace(/^[\*\-]\s*/, "").trim()).filter(b => b.length);
    if (bullets.length) {
      html += `<div style="padding:12px 14px;border-radius:8px;background:var(--surface2);border:1px solid var(--border);margin-bottom:12px;">
        <div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;">Key Points</div>`;
      bullets.forEach(b => {
        html += `<div style="display:flex;gap:8px;margin-bottom:6px;font-size:13px;color:var(--text-soft);">
          <span style="color:var(--accent);flex-shrink:0;margin-top:1px;">·</span>
          <span>${esc(b)}</span>
        </div>`;
      });
      html += `</div>`;
    }
  }

  if (actionItems && actionItems.toLowerCase() !== "none" && actionItems.trim()) {
    html += `<div style="padding:12px 14px;border-radius:8px;background:rgba(155,127,232,0.07);border:1px solid rgba(155,127,232,0.18);margin-bottom:12px;">
      <div style="font-size:11px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;">Action Items</div>
      <div style="font-size:13px;color:var(--text-soft);line-height:1.65;">${esc(actionItems).split("\n").join("<br>")}</div>
    </div>`;
  }

  if (obj.citations && obj.citations.length) {
    html += `<div style="border-top:1px solid var(--border);padding-top:10px;">
      <div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;">Sources (${obj.citations.length})</div>`;
    obj.citations.forEach((c, i) => {
      let snippet = esc(stripSlack(c.snippet || c.text || ""));
      if (snippet.length > 220) snippet = snippet.slice(0, 220) + "…";
      html += `<div style="border-left:2px solid var(--accent);padding:7px 10px;margin-bottom:8px;border-radius:0 6px 6px 0;background:var(--surface2);">
        <div style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-muted);margin-bottom:4px;">
          [${i + 1}] ${esc(c.timestamp_human || "")} · ${esc(c.username || c.user_id || "unknown")}${c.channel_id ? ` · <span style="color:var(--accent);">#${esc(c.channel_id)}</span>` : ""}
        </div>
        <div style="font-size:12px;color:var(--text-soft);line-height:1.5;">${snippet}</div>
      </div>`;
    });
    html += `</div>`;
  }
  return html;
}

/* ── Workspaces & Channels ──────────────────────────────────────────────────── */
async function loadWorkspaces(preferredTeamId = "") {
  try {
    setStatus("Loading workspaces…");
    const data = await fetchJson(getApiBase() + "/workspaces");
    show(data);
    const sel        = document.getElementById("workspaceSelect");
    const channelSel = document.getElementById("channelSelect");
    sel.innerHTML    = "";
    channelSel.innerHTML = '<option value="">— Select workspace, then Load Channels —</option>';
    if (!data.ok) { sel.innerHTML = '<option value="">— failed to load workspaces —</option>'; setStatus("Workspaces load failed", "err"); return; }
    const list = data.workspaces || [];
    if (!list.length) { sel.innerHTML = '<option value="">— none connected yet —</option>'; setStatus("No workspaces connected", "warn"); return; }
    let chosen = false;
    list.forEach((w, idx) => {
      const opt = document.createElement("option");
      opt.value       = w.team_id;
      opt.textContent = (w.team_name ? w.team_name : w.team_id) + (w.team_name ? "  (" + w.team_id + ")" : "");
      if ((preferredTeamId && w.team_id === preferredTeamId) || (!preferredTeamId && idx === 0)) { opt.selected = true; chosen = true; }
      sel.appendChild(opt);
    });
    if (!chosen && sel.options.length > 0) sel.selectedIndex = 0;
    setStatus(list.length + " workspace" + (list.length > 1 ? "s" : "") + " loaded — click Load Channels", "ok");
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Workspaces load failed", "err"); }
}

function connectSlack() {
  setStatus("Opening Slack OAuth…");
  oauthPopup = window.open(getApiBase() + "/install", "slack_oauth_popup", "width=700,height=800,menubar=no,toolbar=no,location=yes,resizable=yes,scrollbars=yes,status=no");
  if (!oauthPopup) setStatus("Popup blocked", "err");
}

window.addEventListener("message", async (event) => {
  const data = event.data || {};
  if (data.type === "slack_oauth_success") {
    setStatus("Workspace connected — refreshing…", "ok");
    await new Promise(res => setTimeout(res, 600));
    await loadWorkspaces(data.team_id || "");
  } else if (data.type === "slack_oauth_error") {
    show({ ok: false, oauth_error: data.error });
    setStatus("Slack connect failed", "err");
  }
});

async function loadChannels() {
  const team_id = document.getElementById("workspaceSelect").value;
  const sel     = document.getElementById("channelSelect");
  if (!team_id) { sel.innerHTML = '<option value="">— Load channels first —</option>'; return; }
  setStatus("Loading channels…");
  const data = await fetchJson(getApiBase() + "/channels?team_id=" + encodeURIComponent(team_id));
  show(data);
  sel.innerHTML = "";
  if (!data.ok) { sel.innerHTML = '<option value="">— failed to load channels —</option>'; setStatus("Channels load failed", "err"); return; }
  const channels = data.channels || [];
  if (!channels.length) { sel.innerHTML = '<option value="">— no channels returned —</option>'; setStatus("No channels", "warn"); return; }
  channels.forEach(c => { const opt = document.createElement("option"); opt.value = c.id; opt.textContent = "#" + c.name; sel.appendChild(opt); });
  setStatus("Channels loaded", "ok");
  if (searchMultiMode) populateMultiChannelList("multiSearchChannelList");
  if (chatMultiMode)   populateMultiChannelList("multiChatChannelList");
}

/* ── Backfill / Join ────────────────────────────────────────────────────────── */
async function backfillOneChannel(team_id, channel_id) {
  let cursor = "", totalStored = 0;
  while (true) {
    const url  = getApiBase() + "/backfill-channel?team_id=" + encodeURIComponent(team_id) + "&channel_id=" + encodeURIComponent(channel_id) + "&limit=200" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
    const data = await fetchJson(url, { method: "POST" });
    if (!data.ok) return { ok: false, error: data };
    totalStored += (data.stored_new || 0);
    if (!data.has_more) break;
    cursor = data.next_cursor || "";
    await new Promise(res => setTimeout(res, 350));
  }
  return { ok: true, stored: totalStored };
}

async function joinSelectedAndBackfill() {
  try {
    const team_id    = document.getElementById("workspaceSelect").value;
    const channel_id = document.getElementById("channelSelect").value;
    if (!team_id || !channel_id) { setStatus("Select workspace + channel first", "warn"); return; }
    setStatus("Joining channel…");
    const j = await fetchJson(getApiBase() + "/join-channel?team_id=" + encodeURIComponent(team_id) + "&channel_id=" + encodeURIComponent(channel_id), { method: "POST" });
    if (!j.ok) { show(j); setStatus("Join failed", "err"); return; }
    setStatus("Backfilling…");
    const bf = await backfillOneChannel(team_id, channel_id);
    show({ join: j, backfill: bf });
    setStatus(bf.ok ? ("Done — " + bf.stored + " stored") : "Backfill failed", bf.ok ? "ok" : "err");
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Failed", "err"); }
}

async function joinAllPublicOnly() {
  try {
    const team_id = document.getElementById("workspaceSelect").value;
    if (!team_id) { setStatus("Select a workspace first", "warn"); return; }
    setStatus("Joining all public channels…");
    const j = await fetchJson(getApiBase() + "/join-all-public?team_id=" + encodeURIComponent(team_id), { method: "POST" });
    show(j);
    setStatus(j.ok ? ("Joined " + (j.joined_count || 0) + " public channels") : "Join failed", j.ok ? "ok" : "err");
    if (j.ok) await loadChannels();
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Failed", "err"); }
}

async function backfillAllPublic() {
  try {
    const team_id = document.getElementById("workspaceSelect").value;
    if (!team_id) { setStatus("Select a workspace first", "warn"); return; }
    setStatus("Backfilling all public channels…");
    const data = await fetchJson(getApiBase() + "/backfill-all-public?team_id=" + encodeURIComponent(team_id), { method: "POST" });
    show({ backfill_all: data });
    setStatus(data.ok ? ("Done — " + (data.total_stored || 0) + " stored") : "Backfill failed", data.ok ? "ok" : "err");
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Failed", "err"); }
}

async function backfillAllPrivate() {
  try {
    const team_id = document.getElementById("workspaceSelect").value;
    if (!team_id) { setStatus("Select a workspace first", "warn"); return; }
    setStatus("Backfilling all private channels…");
    const data = await fetchJson(getApiBase() + "/backfill-all-private?team_id=" + encodeURIComponent(team_id), { method: "POST" });
    show({ backfill_all: data });
    setStatus(data.ok ? ("Done — " + (data.total_stored || 0) + " stored") : "Backfill failed", data.ok ? "ok" : "err");
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Failed", "err"); }
}

async function loadMessagesFromDB() {
  try {
    const team_id    = document.getElementById("workspaceSelect").value;
    const channel_id = document.getElementById("channelSelect").value;
    if (!team_id || !channel_id) { setStatus("Select workspace + channel first", "warn"); return; }
    setStatus("Loading from DB…");
    const data = await fetchJson(getApiBase() + "/db-messages?team_id=" + encodeURIComponent(team_id) + "&channel_id=" + encodeURIComponent(channel_id) + "&limit=50");
    show(data);
    setStatus(data.ok ? "Loaded from DB" : "DB load failed", data.ok ? "ok" : "err");
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("DB load failed", "err"); }
}

async function disconnectWorkspace() {
  try {
    const team_id    = document.getElementById("workspaceSelect").value;
    const workspaceSel = document.getElementById("workspaceSelect");
    const channelSel   = document.getElementById("channelSelect");
    if (!team_id) { setStatus("Select a workspace first", "warn"); return; }
    if (!confirm("Disconnect this workspace? This will revoke the token and delete the secret.")) return;
    setStatus("Disconnecting…");
    const data = await fetchJson(getApiBase() + "/workspaces/" + encodeURIComponent(team_id), { method: "DELETE" });
    show(data);
    if (!data.ok) { setStatus("Disconnect failed", "err"); return; }
    workspaceSel.innerHTML = '<option value="">— none connected yet —</option>';
    channelSel.innerHTML   = '<option value="">— Load channels first —</option>';
    setStatus("Disconnected", "ok");
    await loadWorkspaces();
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Disconnect failed", "err"); }
}

/* ── Multi-channel mode ─────────────────────────────────────────────────────── */
function toggleSearchMode() {
  searchMultiMode = !searchMultiMode;
  const btn    = document.getElementById("searchModeToggle");
  const picker = document.getElementById("multiSearchChannelPicker");
  const info   = document.getElementById("searchSingleChannelInfo");
  if (searchMultiMode) {
    btn.textContent = "✕ Single Channel";
    btn.style.cssText += ";background:rgba(155,127,232,0.12);border-color:rgba(155,127,232,0.4);color:var(--accent)";
    picker.style.display = "block";
    info.style.display   = "none";
    populateMultiChannelList("multiSearchChannelList");
  } else {
    btn.textContent = "+ Multi-Channel";
    btn.style.background = btn.style.borderColor = btn.style.color = "";
    picker.style.display = "none";
    info.style.display   = "none";
  }
}

function toggleChatMode() {
  chatMultiMode = !chatMultiMode;
  const btn    = document.getElementById("chatModeToggle");
  const picker = document.getElementById("multiChatChannelPicker");
  const info   = document.getElementById("chatSingleChannelInfo");
  if (chatMultiMode) {
    btn.textContent = "✕ Single Channel";
    btn.style.cssText += ";background:rgba(155,127,232,0.12);border-color:rgba(155,127,232,0.4);color:var(--accent)";
    picker.style.display = "block";
    info.style.display   = "none";
    populateMultiChannelList("multiChatChannelList");
  } else {
    btn.textContent = "+ Multi-Channel";
    btn.style.background = btn.style.borderColor = btn.style.color = "";
    picker.style.display = "none";
    info.style.display   = "none";
  }
}

function populateMultiChannelList(containerId) {
  const container  = document.getElementById(containerId);
  const channelSel = document.getElementById("channelSelect");
  const opts       = Array.from(channelSel.options).filter(o => o.value);
  if (!opts.length) {
    container.innerHTML = '<span style="font-size:12px;color:var(--text-muted);">Load channels first (Step 02)</span>';
    return;
  }
  if (!_multiSelections[containerId]) {
    _multiSelections[containerId] = new Set(opts.map(o => o.value));
  }
  const sel = _multiSelections[containerId];
  container.innerHTML = "";
  opts.forEach(o => {
    const uid = "chk_" + containerId + "_" + o.value.replace(/[^a-zA-Z0-9]/g, "_");
    const cb  = document.createElement("input");
    cb.type    = "checkbox"; cb.id = uid; cb.value = o.value; cb.checked = sel.has(o.value);
    cb.addEventListener("change", () => { if (cb.checked) sel.add(o.value); else sel.delete(o.value); });
    const lbl = document.createElement("label");
    lbl.htmlFor   = uid;
    lbl.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:5px;cursor:pointer;width:100%;box-sizing:border-box;text-transform:none;letter-spacing:0;font-size:13px;color:var(--text-soft);font-weight:400;margin:0;";
    lbl.onmouseover = () => lbl.style.background = "rgba(155,127,232,0.08)";
    lbl.onmouseout  = () => lbl.style.background = "transparent";
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(o.textContent));
    container.appendChild(lbl);
  });
}

function getCheckedChannels(containerId) {
  return Array.from(document.getElementById(containerId).querySelectorAll("input[type=checkbox]:checked")).map(cb => cb.value);
}

function selectAllSearchChannels() { document.getElementById("multiSearchChannelList").querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = true;  if (_multiSelections["multiSearchChannelList"]) _multiSelections["multiSearchChannelList"].add(cb.value); }); }
function clearAllSearchChannels()  { document.getElementById("multiSearchChannelList").querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = false; if (_multiSelections["multiSearchChannelList"]) _multiSelections["multiSearchChannelList"].delete(cb.value); }); }
function selectAllChatChannels()   { document.getElementById("multiChatChannelList").querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = true;  if (_multiSelections["multiChatChannelList"]) _multiSelections["multiChatChannelList"].add(cb.value); }); }
function clearAllChatChannels()    { document.getElementById("multiChatChannelList").querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = false; if (_multiSelections["multiChatChannelList"]) _multiSelections["multiChatChannelList"].delete(cb.value); }); }

/* ── Search ─────────────────────────────────────────────────────────────────── */
async function searchMessages() {
  try {
    const team_id = document.getElementById("workspaceSelect").value;
    if (!team_id) { setStatus("Select workspace first", "warn"); return; }
    const q       = document.getElementById("keyword").value.trim();
    const from    = document.getElementById("fromDate").value;
    const to      = document.getElementById("toDate").value;
    const user_id = document.getElementById("searchUserId").value.trim();

    if (searchMultiMode) {
      const channelIds = getCheckedChannels("multiSearchChannelList");
      if (!channelIds.length) { setStatus("Select at least one channel", "warn"); return; }
      let url = getApiBase() + "/search/multi?team_id=" + encodeURIComponent(team_id) + "&channel_ids=" + encodeURIComponent(channelIds.join(","));
      if (q)       url += "&q="        + encodeURIComponent(q);
      if (from)    url += "&from="     + encodeURIComponent(from);
      if (to)      url += "&to="       + encodeURIComponent(to);
      if (user_id) url += "&username=" + encodeURIComponent(user_id);
      setStatus("Searching " + channelIds.length + " channels…");
      const data = await fetchJson(url);
      show(data);
      const n = data.count || 0, ch = data.channels_searched || channelIds.length;
      setStatus(data.ok ? (n > 0 ? `Found ${n} result${n === 1 ? "" : "s"} across ${ch} channels` : "No results found") : "Search failed", data.ok ? (n > 0 ? "ok" : "warn") : "err");
    } else {
      const channel_id = document.getElementById("channelSelect").value;
      if (!channel_id) { setStatus("Select workspace + channel first", "warn"); return; }
      let url = getApiBase() + "/search?team_id=" + encodeURIComponent(team_id) + "&channel_id=" + encodeURIComponent(channel_id);
      if (q)       url += "&q="        + encodeURIComponent(q);
      if (from)    url += "&from="     + encodeURIComponent(from);
      if (to)      url += "&to="       + encodeURIComponent(to);
      if (user_id) url += "&username=" + encodeURIComponent(user_id);
      setStatus("Searching…");
      const data = await fetchJson(url);
      show(data);
      const n = data.count || 0;
      setStatus(data.ok ? (n > 0 ? `Found ${n} message${n === 1 ? "" : "s"}` : "No results found") : "Search failed", data.ok ? (n > 0 ? "ok" : "warn") : "err");
    }
  } catch (e) { show({ ok: false, error: String(e) }); setStatus("Search error", "err"); }
}

function clearSearch() {
  ["keyword","fromDate","toDate","searchUserId","chatFromDate","chatToDate"].forEach(id => document.getElementById(id).value = "");
  show({});
  setStatus("Cleared");
}

/* ── Ask AI ─────────────────────────────────────────────────────────────────── */
async function askChat() {
  try {
    const team_id  = document.getElementById("workspaceSelect").value;
    const question = document.getElementById("chatQuestion").value.trim();
    const fromDate = document.getElementById("chatFromDate").value || null;
    const toDate   = document.getElementById("chatToDate").value   || null;
    if (!team_id)                                         { setStatus("Select a workspace first", "warn"); return; }
    if (!question)                                        { setStatus("Enter a question first", "warn"); return; }
    if (question.length > 500)                            { setStatus("Question too long (max 500 chars)", "warn"); return; }
    if (fromDate && toDate && fromDate > toDate)          { setStatus("'From' date must be before 'To' date", "warn"); return; }

    const btn = document.getElementById("askBtn");
    btn.disabled = true; btn.textContent = "Asking…";
    document.getElementById("chatAnswer").style.display = "none";

    let requestBody, endpoint;

    if (chatMultiMode) {
      const channelIds = getCheckedChannels("multiChatChannelList");
      if (!channelIds.length) { setStatus("Select at least one channel", "warn"); btn.disabled = false; btn.textContent = "Ask"; return; }
      endpoint = getApiBase() + "/chat/multi";
      const payload = { team_id, channel_ids: channelIds, question };
      if (fromDate) payload.from_date = fromDate;
      if (toDate)   payload.to_date   = toDate;
      requestBody = JSON.stringify(payload);
      setStatus("Thinking across " + channelIds.length + " channels" + (fromDate ? ` · ${fromDate}${toDate ? " → " + toDate : "+"}` : "") + "…");
    } else {
      const channel_id = document.getElementById("channelSelect").value;
      if (!channel_id) { setStatus("Select workspace + channel first", "warn"); btn.disabled = false; btn.textContent = "Ask"; return; }
      endpoint = getApiBase() + "/chat";
      const payload = { team_id, channel_id, question };
      if (fromDate) payload.from_date = fromDate;
      if (toDate)   payload.to_date   = toDate;
      requestBody = JSON.stringify(payload);
      setStatus("Thinking" + (fromDate ? ` · ${fromDate}${toDate ? " → " + toDate : "+"}` : "") + "…");
    }

    const r    = await fetch(endpoint, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: requestBody });
    const data = await safeJson(r);
    show(data);
    if (r.status === 403) { setStatus("Access denied", "err"); return; }

    if (data.ok && data.answer) {
      /* inline answer panel */
      const raw        = data.answer || "";
      const answerText = _parseSection(raw, "Answer") || raw;
      const keyPoints  = _parseSection(raw, "Key points");
      const actionItems= _parseSection(raw, "Action items");
      let html = `<div style="font-size:13px;color:var(--text);line-height:1.7;">${esc(answerText).split("\n").join("<br>")}</div>`;

      if (keyPoints && keyPoints.toLowerCase() !== "none" && keyPoints.trim()) {
        const bullets = keyPoints.split("\n").map(b => b.replace(/^[\*\-]\s*/, "").trim()).filter(b => b.length);
        html += `<div style="margin-top:12px;"><div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px;">Key Points</div>`;
        bullets.forEach(b => { html += `<div style="display:flex;gap:6px;margin-bottom:4px;font-size:13px;color:var(--text-soft);"><span style="color:var(--accent);flex-shrink:0;">·</span><span>${esc(b)}</span></div>`; });
        html += `</div>`;
      }
      if (actionItems && actionItems.toLowerCase() !== "none" && actionItems.trim()) {
        html += `<div style="margin-top:12px;padding:8px 12px;border-radius:6px;background:rgba(155,127,232,0.07);border:1px solid rgba(155,127,232,0.15);">
          <div style="font-size:11px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px;">Action Items</div>
          <div style="font-size:13px;color:var(--text-soft);">${esc(actionItems).split("\n").join("<br>")}</div></div>`;
      }
      if (data.channels_searched > 1) {
        html = `<div style="font-size:11px;color:var(--accent);margin-bottom:8px;padding:4px 8px;background:rgba(155,127,232,0.08);border-radius:4px;border:1px solid rgba(155,127,232,0.2);">🌐 Answer synthesized from <strong>${data.channels_searched} channels</strong> · ${data.retrieved_count || 0} messages reviewed</div>` + html;
      }
      if (data.resolved_username) {
        html = `<div style="font-size:11px;color:#0d9488;margin-bottom:8px;padding:4px 10px;background:rgba(13,148,136,0.07);border-radius:4px;border:1px solid rgba(13,148,136,0.2);display:inline-flex;align-items:center;gap:5px;">👤 Filtering messages from <strong>@${data.resolved_username}</strong></div><br>` + html;
      }
      const chatFrom = document.getElementById("chatFromDate").value;
      const chatTo   = document.getElementById("chatToDate").value;
      if (chatFrom || chatTo) {
        const rangeLabel = chatFrom && chatTo ? chatFrom + " → " + chatTo : chatFrom ? "From " + chatFrom : "Until " + chatTo;
        html = `<div style="font-size:11px;color:#2563eb;margin-bottom:8px;padding:4px 10px;background:rgba(37,99,235,0.07);border-radius:4px;border:1px solid rgba(37,99,235,0.2);display:inline-flex;align-items:center;gap:5px;">📅 Date range: <strong>${rangeLabel}</strong></div><br>` + html;
      }

      document.getElementById("chatAnswerText").innerHTML = html;

      const citBox = document.getElementById("chatCitations");
      if (data.citations && data.citations.length) {
        citBox.innerHTML = `<div style="margin-top:14px;border-top:1px solid var(--border);padding-top:10px;">
          <div style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;">Sources (${data.citations.length})</div>
          ${data.citations.map((c, i) => `
            <div style="border-left:2px solid var(--accent);padding:6px 10px;margin-bottom:8px;border-radius:0 6px 6px 0;background:var(--surface2);">
              <div style="font-family:'DM Mono',monospace;font-size:11px;color:var(--text-muted);margin-bottom:4px;">[${i + 1}] ${c.timestamp_human || ""} · ${c.username || c.user_id || "unknown"}${c.channel_id ? ` · <span style="color:var(--accent);">#${c.channel_id}</span>` : ""}</div>
              <div style="font-size:12px;color:var(--text-soft);line-height:1.5;">${stripSlack(c.snippet || c.text || "").slice(0, 200)}${(c.snippet || "").length > 200 ? "…" : ""}</div>
            </div>`).join("")}
        </div>`;
      } else { citBox.innerHTML = ""; }

      document.getElementById("chatAnswer").style.display = "block";
      setStatus("Answer ready" + (data.channels_searched > 1 ? ` (${data.channels_searched} channels)` : ""), "ok");
      show({ ok: true, question: data.question, answer: data.answer, citations: data.citations, retrieved: data.retrieved_count, channels_searched: data.channels_searched, resolved_username: data.resolved_username });
    } else {
      setStatus("Chat failed", "err");
    }
  } catch (e) {
    show({ ok: false, error: String(e) });
    setStatus("Chat error", "err");
  } finally {
    const btn = document.getElementById("askBtn");
    btn.disabled = false; btn.textContent = "Ask";
  }
}

/* ── Session ─────────────────────────────────────────────────────────────────── */
async function initSession() {
  try {
    const r = await fetchJson(getApiBase() + "/session");
    if (r.ok && r.session_id) {
      document.getElementById("sessionId").textContent    = r.session_id.slice(0, 8) + "…";
      document.getElementById("sessionBadge").style.display = "flex";
      document.getElementById("logoutBtn").style.display    = "inline-block";
    }
  } catch (e) { /* non-fatal */ }
  await loadWorkspaces();
}

async function logout() {
  if (!confirm("Sign out? Your session will be cleared.")) return;
  try { await fetch(getApiBase() + "/logout", { method: "POST", credentials: "include" }); } catch (e) {}
  document.getElementById("workspaceSelect").innerHTML = '<option value="">— none connected yet —</option>';
  document.getElementById("channelSelect").innerHTML   = '<option value="">— Load channels first —</option>';
  document.getElementById("sessionBadge").style.display = "none";
  document.getElementById("logoutBtn").style.display    = "none";
  show({});
  setStatus("Signed out", "warn");
}

/* ── Boot ───────────────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  const ta = document.getElementById("chatQuestion");
  if (ta) ta.addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") askChat(); });
  initSession();
});