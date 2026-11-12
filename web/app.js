/* 云课助手 DSH - 前端逻辑 */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const api = async (url, opt = {}) => {
    const r = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...opt,
      body: opt.body && typeof opt.body !== "string" ? JSON.stringify(opt.body) : opt.body,
    });
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.detail || j.message || msg; } catch (e) {}
      throw new Error(msg);
    }
    return r.headers.get("content-type")?.includes("application/json") ? r.json() : r.text();
  };

  const state = {
    settings: null,
    videos: [],
    cur: null,
    cues: [],
    segments: [],
    cueEls: [],
    activeCue: -1,
    activeSeg: -1,
    follow: true,
    sel: { a: null, b: null },
    chatId: null,
    chatSel: null,
    jobTimer: null,
    curJob: null,
    lastPosSave: 0,
    mdTimer: null,
    autoTranscoded: false,
  };

  const fmtTime = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`
             : `${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`;
  };
  const fmtDur = (s) => {
    const m = Math.round((s || 0) / 60);
    return m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分钟`;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transform = "translateY(8px)"; }, 3600);
    setTimeout(() => el.remove(), 4000);
  }

  /* ------------------------------------------------ 课程库 */
  async function loadVideos() {
    const r = await api("/api/videos");
    state.videos = r.videos || [];
    renderLibrary();
  }

  function renderLibrary() {
    const grid = $("#libGrid");
    const kw = ($("#libSearch").value || "").trim().toLowerCase();
    const list = state.videos.filter((v) => !kw || v.title.toLowerCase().includes(kw));
    $("#libCount").textContent = state.videos.length ? `共 ${state.videos.length} 门课` : "";
    $("#libEmpty").classList.toggle("hidden", state.videos.length > 0);
    grid.innerHTML = "";
    const frag = document.createDocumentFragment();
    list.forEach((v) => {
      const meta = v.meta || {};
      const card = document.createElement("div");
      card.className = "card";
      const statusText = { ready: "已分析", analyzing: "分析中", failed: "失败", new: "待分析" }[v.status] || v.status;
      const cover = meta.cover ? `/api/videos/${v.id}/cover` : "";
      card.innerHTML = `
        <div class="card-cover" ${cover ? `style="background-image:url('${cover}')"` : ""}>
          ${cover ? "" : '<span class="ph">▶</span>'}
          <span class="dur">${fmtDur(v.duration)}</span>
          <button class="icon-btn sm card-del" title="删除"><svg class="ico"><use href="#i-trash"/></svg></button>
        </div>
        <div class="card-body">
          <h4>${esc(v.title)}</h4>
          <div class="card-meta">
            <span class="status ${v.status}">${statusText}</span>
            <span>${v.segment_count ? v.segment_count + " 段" : fmtTime(v.duration)}</span>
          </div>
          ${v.status === "analyzing" ? '<div class="card-progress"><i style="width:60%"></i></div>' : ""}
        </div>`;
      card.addEventListener("click", (e) => {
        if (e.target.closest(".card-del")) return;
        openPlayer(v.id);
      });
      card.querySelector(".card-del").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`删除《${v.title}》？字幕和笔记会一起删除（原视频文件不受影响）。`)) return;
        await api(`/api/videos/${v.id}`, { method: "DELETE" });
        toast("已删除", "ok");
        loadVideos();
      });
      frag.appendChild(card);
    });
    grid.appendChild(frag);
  }

  /* ------------------------------------------------ 播放器 */
  async function openPlayer(vid) {
    const d = await api(`/api/videos/${vid}`);
    state.cur = d.video;
    state.segments = d.segments || [];
    state.cues = d.cues || [];
    state.activeCue = -1;
    state.activeSeg = -1;
    state.sel = { a: null, b: null };
    state.chatSel = null;
    state.autoTranscoded = false;
    state.chatId = null;

    $("#view-library").classList.add("hidden");
    $("#view-player").classList.remove("hidden");
    $("#chatContext").classList.add("hidden");
    $("#pTitle").textContent = d.video.title;
    const meta = d.video.meta || {};
    $("#pMeta").textContent = [
      fmtDur(d.video.duration),
      meta.media_kind === "audio" ? "录音" : `${meta.width || "?"}×${meta.height || "?"}`,
      state.cues.length ? `${state.cues.length} 条字幕` : "尚未分析",
    ].filter(Boolean).join(" · ");

    setupMedia(d.video);
    renderSegments();
    renderTimeline();
    renderCueList();
    renderOverview(d.overview || meta.overview || {});
    renderSubtitle(-1);
    renderSelection();
    loadChats();
    applySubtitleStyle();

    const pos = Number(meta.position || 0);
    if (pos > 5) {
      const apply = () => {
        try { video.currentTime = Math.min(pos, (video.duration || pos + 1) - 1); } catch (e) {}
        toast(`已从 ${fmtTime(pos)} 继续播放`);
      };
      if (video.readyState >= 1) apply();
      else video.addEventListener("loadedmetadata", apply, { once: true });
    }
  }

  function setupMedia(v) {
    const meta = v.meta || {};
    const video = $("#video");
    const wrap = $("#videoWrap");
    const isAudio = meta.media_kind === "audio";
    $("#audioCover").classList.toggle("hidden", !isAudio);
    if (isAudio) {
      $("#audioCoverImg").src = `/api/videos/${v.id}/cover`;
      video.style.visibility = "hidden";
      wrap.style.background = "radial-gradient(circle at 50% 30%,#16233c,#080d16)";
    } else {
      video.style.visibility = "visible";
      wrap.style.background = "#000";
    }
    video.src = `/api/videos/${v.id}/media`;
    if (v.cover) video.poster = `/api/videos/${v.id}/cover`;
    else video.removeAttribute("poster");
    video.playbackRate = parseFloat($("#speedSel").value) || 1;
    video.onerror = onPlaybackError;
    video.load();
  }

  async function onPlaybackError() {
    if (state.autoTranscoded || !state.cur) return;
    state.autoTranscoded = true;
    toast("这个格式浏览器放不了，正在后台转成 MP4…");
    try {
      const r = await api(`/api/videos/${state.cur.id}/transcode`, { method: "POST" });
      const job = r.job;
      const timer = setInterval(async () => {
        const j = await api(`/api/jobs/${job.id}`);
        if (j.status === "done") {
          clearInterval(timer);
          toast("转码完成", "ok");
          setupMedia({ ...state.cur, meta: { ...(state.cur.meta || {}) } });
        } else if (j.status === "failed") {
          clearInterval(timer);
          toast("转码失败：" + j.message, "err");
        }
      }, 1200);
    } catch (e) { toast("转码失败：" + e.message, "err"); }
  }

  function renderSegments() {
    const box = $("#segList");
    box.innerHTML = "";
    $("#segCount").textContent = state.segments.length ? `${state.segments.length} 段` : "";
    if (!state.segments.length) {
      box.innerHTML = '<div class="chat-empty">还没有分段，点右上角「重新分析」生成字幕与摘要。</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    state.segments.forEach((s, i) => {
      const el = document.createElement("div");
      el.className = "seg-item";
      el.dataset.i = i;
      el.innerHTML = `<div class="st"><i>${String(i + 1).padStart(2, "0")}</i><span>${esc(s.title)}</span></div>
        <div class="sm-time">${fmtTime(s.start)} - ${fmtTime(s.end)}</div>`;
      el.addEventListener("click", () => seekTo(s.start + 0.05));
      frag.appendChild(el);
    });
    box.appendChild(frag);
  }

  function renderTimeline() {
    const box = $("#tlSegments");
    box.innerHTML = "";
    const dur = state.cur?.duration || 0;
    if (!dur) return;
    state.segments.forEach((s, i) => {
      const el = document.createElement("i");
      el.style.left = (s.start / dur * 100) + "%";
      el.style.width = ((s.end - s.start) / dur * 100) + "%";
      const hue = 205 + (i * 37) % 100;
      el.style.background = `hsla(${hue},55%,${28 + (i % 3) * 6}%,.55)`;
      el.title = s.title;
      box.appendChild(el);
    });
  }

  function renderOverview(ov) {
    const box = $("#overviewBox");
    const cards = $("#segCards");
    cards.innerHTML = "";
    if (!ov || (!ov.summary && !state.segments.length)) {
      box.innerHTML = '<span class="muted">还没有课程摘要。</span>';
      return;
    }
    let html = "";
    if (ov.summary) html += `<h4>课程概览</h4><div>${esc(ov.summary)}</div>`;
    if (ov.takeaways?.length) html += `<h4 style="margin-top:10px">重点结论</h4><ul>${ov.takeaways.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
    if (ov.terms?.length) html += `<div class="terms">${ov.terms.slice(0, 18).map((t) => `<span class="term">${esc(t.term)}${t.cn ? " · " + esc(t.cn) : ""}</span>`).join("")}</div>`;
    box.innerHTML = html;
    state.segments.forEach((s, i) => {
      const el = document.createElement("div");
      el.className = "seg-card";
      el.dataset.i = i;
      el.innerHTML = `<h5><span>${esc(s.title)}</span><span>${fmtTime(s.start)}</span></h5>
        <p>${esc(s.abstract)}</p>
        ${s.keypoints?.length ? `<ul>${s.keypoints.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}`;
      el.addEventListener("click", () => seekTo(s.start + 0.05));
      cards.appendChild(el);
    });
  }

  function renderCueList(filter = "") {
    const box = $("#cueList");
    box.innerHTML = "";
    state.cueEls = [];
    if (!state.cues.length) {
      box.innerHTML = '<div class="chat-empty">还没有字幕。点「重新分析」生成双语字幕。</div>';
      return;
    }
    const kw = filter.trim().toLowerCase();
    const frag = document.createDocumentFragment();
    state.cues.forEach((c, i) => {
      let text = esc(c.text);
      if (kw) {
        const idx = c.text.toLowerCase().indexOf(kw);
        if (idx >= 0) {
          text = esc(c.text.slice(0, idx)) + "<mark>" + esc(c.text.slice(idx, idx + kw.length)) + "</mark>" + esc(c.text.slice(idx + kw.length));
        }
      }
      const el = document.createElement("div");
      el.className = "cue";
      el.dataset.i = i;
      el.innerHTML = `<div class="t">${fmtTime(c.start)}</div>
        <div><div class="tx">${text}</div>${c.translation ? `<div class="tr">${esc(c.translation)}</div>` : ""}</div>`;
      el.addEventListener("click", () => seekTo(c.start + 0.02));
      frag.appendChild(el);
      state.cueEls.push(el);
    });
    box.appendChild(frag);
    if (kw) {
      const first = state.cues.findIndex((c) => c.text.toLowerCase().includes(kw));
      if (first >= 0) state.cueEls[first]?.scrollIntoView({ block: "center" });
    }
  }

  function cueIndexAt(t) {
    const cues = state.cues;
    let lo = 0, hi = cues.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].start <= t + 0.05) { ans = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    if (ans >= 0 && t > cues[ans].end + 0.6) return -1;
    return ans;
  }

  function segIndexAt(t) {
    return state.segments.findIndex((s) => t >= s.start && t < s.end);
  }

  function renderSubtitle(i) {
    const show = $("#subToggle").checked;
    const c = i >= 0 ? state.cues[i] : null;
    const box = $("#subtitle");
    box.style.display = show && c ? "block" : "none";
    if (!c) { $("#subText").textContent = ""; $("#subTr").textContent = ""; return; }
    $("#subText").textContent = c.text || "";
    $("#subTr").textContent = c.translation || "";
    $("#subTr").style.display = c.translation ? "block" : "none";
  }

  function applySubtitleStyle() {
    const size = Number(state.settings?.ui?.font_size || 26);
    $("#subText").style.fontSize = size + "px";
    $("#subTr").style.fontSize = Math.max(13, Math.round(size * 0.66)) + "px";
  }

  function onTime() {
    const video = $("#video");
    const t = video.currentTime || 0;
    const dur = video.duration || state.cur?.duration || 0;
    $("#timeNow").textContent = fmtTime(t);
    $("#timeTotal").textContent = fmtTime(dur);
    $("#tlPlayed").style.width = dur ? (t / dur * 100) + "%" : "0";
    $("#tlHead").style.left = dur ? (t / dur * 100) + "%" : "0";
    if (video.buffered.length && dur) {
      const end = video.buffered.end(video.buffered.length - 1);
      $("#tlBuffer").style.width = (end / dur * 100) + "%";
    }
    const ci = cueIndexAt(t);
    if (ci !== state.activeCue) {
      if (state.activeCue >= 0) state.cueEls[state.activeCue]?.classList.remove("active");
      state.activeCue = ci;
      if (ci >= 0) {
        const el = state.cueEls[ci];
        el?.classList.add("active");
        if (state.follow && !el?.matches(":hover")) el?.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      renderSubtitle(ci);
    }
    const si = segIndexAt(t);
    if (si !== state.activeSeg) {
      state.activeSeg = si;
      $$(".seg-item").forEach((el, k) => el.classList.toggle("active", k === si));
      $$(".seg-card").forEach((el, k) => el.classList.toggle("active", k === si));
      const segEl = $$(".seg-item")[si];
      if (segEl && state.follow) segEl.scrollIntoView({ block: "nearest" });
    }
    if (Date.now() - state.lastPosSave > 8000 && state.cur) {
      state.lastPosSave = Date.now();
      api(`/api/videos/${state.cur.id}/position`, { method: "POST", body: { position: t } }).catch(() => {});
    }
  }

  function seekTo(t) {
    const video = $("#video");
    if (!video.duration) return;
    video.currentTime = Math.max(0, Math.min(video.duration - 0.3, t));
    if (video.paused) video.play().catch(() => {});
  }

  function togglePlay() {
    const video = $("#video");
    if (video.paused) video.play().catch(() => {}); else video.pause();
  }

  function flashPlay(playing) {
    const el = $("#centerFlash");
    el.innerHTML = `<svg class="ico"><use href="#${playing ? "i-play" : "i-pause"}"/></svg>`;
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 420);
  }

  /* ------------------------------------------------ 选区 */
  function renderSelection() {
    const { a, b } = state.sel;
    const dur = state.cur?.duration || 0;
    const box = $("#tlSelection");
    const has = a != null && b != null && b > a;
    box.classList.toggle("hidden", !has);
    if (has && dur) {
      box.style.left = (a / dur * 100) + "%";
      box.style.width = ((b - a) / dur * 100) + "%";
    }
    $("#selLabel").textContent = has ? `${fmtTime(a)} → ${fmtTime(b)}（${fmtTime(b - a)}）`
      : (a != null ? `A ${fmtTime(a)}，再设 B` : "");
    $("#btnAskSel").classList.toggle("hidden", !has);
    $("#btnClearSel").classList.toggle("hidden", a == null && b == null);
    if (has) {
      state.chatSel = { start: a, end: b };
      $("#chatContext").classList.remove("hidden");
      $("#chatContextText").textContent = `已选中 ${fmtTime(a)} - ${fmtTime(b)}`;
    }
  }

  function mark(which) {
    const t = $("#video").currentTime || 0;
    if (which === "a") {
      state.sel.a = t;
      if (state.sel.b != null && state.sel.b <= t) state.sel.b = null;
    } else {
      state.sel.b = t;
      if (state.sel.a == null) state.sel.a = Math.max(0, t - 30);
      if (state.sel.b <= state.sel.a) { toast("B 点要在 A 点之后", "err"); state.sel.b = null; }
    }
    renderSelection();
  }

  /* ------------------------------------------------ 时间轴交互 */
  function initTimeline() {
    const tl = $("#timeline");
    let dragging = false;
    const seekFromEvent = (e) => {
      const rect = tl.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const dur = $("#video").duration || state.cur?.duration || 0;
      if (dur) seekTo(ratio * dur);
    };
    tl.addEventListener("pointerdown", (e) => {
      dragging = true;
      tl.setPointerCapture(e.pointerId);
      seekFromEvent(e);
    });
    tl.addEventListener("pointermove", (e) => {
      const rect = tl.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      const dur = $("#video").duration || state.cur?.duration || 0;
      const t = ratio * dur;
      const tip = $("#tlTip");
      const seg = state.segments[segIndexAt(t)];
      tip.style.display = "block";
      tip.style.left = (ratio * 100) + "%";
      tip.textContent = `${fmtTime(t)}${seg ? " · " + seg.title : ""}`;
      if (dragging) seekFromEvent(e);
    });
    tl.addEventListener("pointerup", () => { dragging = false; });
    tl.addEventListener("pointerleave", () => { $("#tlTip").style.display = "none"; });
  }

  /* ------------------------------------------------ 问答 */
  function modelOptions(role) {
    const s = state.settings;
    const out = [];
    (s.providers || []).forEach((p) => {
      const models = (role === "asr" ? (p.asr_models?.length ? p.asr_models : p.models) : p.models) || [];
      models.forEach((m) => out.push({ pid: p.id, pname: p.name, model: m, ready: p.id === "ollama" || !!p.api_key }));
    });
    return out;
  }

  function fillChatModels() {
    const sel = $("#chatModel");
    const cur = state.settings?.roles?.chat || {};
    const opts = modelOptions("chat");
    const groups = {};
    opts.forEach((o) => (groups[o.pname] = groups[o.pname] || []).push(o));
    sel.innerHTML = Object.entries(groups).map(([pname, list]) =>
      `<optgroup label="${esc(pname)}">${list.map((o) =>
        `<option value="${o.pid}||${o.model}">${esc(o.model)}${o.ready ? "" : "（未填 Key）"}</option>`).join("")}</optgroup>`).join("");
    const val = `${cur.provider}||${cur.model}`;
    if (opts.some((o) => `${o.pid}||${o.model}` === val)) sel.value = val;
    else if (opts.length) sel.selectedIndex = 0;
  }

  async function loadChats() {
    const box = $("#chatList");
    box.innerHTML = '<div class="chat-empty">选一个片段，或者直接问整节课的问题。<br>AI 会参考字幕与画面回答，并标出时间点。</div>';
    state.chatId = null;
    if (!state.cur) return;
    try {
      const r = await api(`/api/videos/${state.cur.id}/chats`);
      const chats = r.chats || [];
      if (chats.length) {
        const d = await api(`/api/chats/${chats[0].id}`);
        state.chatId = chats[0].id;
        if (d.messages?.length) {
          box.innerHTML = "";
          d.messages.forEach((m) => {
            if (m.role === "assistant") appendMsg("ai", m.content);
            else if (m.role === "user") appendMsg("user", m.content, false);
          });
        }
      }
    } catch (e) {}
  }

  function md(src) {
    let s = esc(src || "");
    s = s.replace(/```(?:\w*)\n([\s\S]*?)```/g, (m, c) => `<pre><code>${c}</code></pre>`);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/^### (.*)$/gm, "<h4>$1</h4>");
    s = s.replace(/^## (.*)$/gm, "<h3>$1</h3>");
    s = s.replace(/^&gt; ?(.*)$/gm, "<blockquote>$1</blockquote>");
    s = s.replace(/(?:^|\n)((?:- .*(?:\n|$))+)/g, (m, b) =>
      "<ul>" + b.trim().split("\n").map((l) => `<li>${l.replace(/^- /, "")}</li>`).join("") + "</ul>");
    s = s.replace(/(?:^|\n)((?:\d+\. .*(?:\n|$))+)/g, (m, b) =>
      "<ol>" + b.trim().split("\n").map((l) => `<li>${l.replace(/^\d+\. /, "")}</li>`).join("") + "</ol>");
    s = s.replace(/\n/g, "<br>");
    return s;
  }

  function appendMsg(who, content, streaming = false) {
    const box = $("#chatList");
    const empty = box.querySelector(".chat-empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "msg " + who;
    el.innerHTML = `<div class="who">${who === "ai" ? "助教" : "我"}</div>
      <div class="bubble">${who === "ai" ? md(content) : esc(content)}${streaming ? '<span class="cursor"></span>' : ""}</div>`;
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return el;
  }

  async function sendChat() {
    const input = $("#chatInput");
    const text = input.value.trim();
    if (!text || !state.cur) return;
    input.value = "";
    input.style.height = "auto";
    appendMsg("user", text, false);
    const aiEl = appendMsg("ai", "", true);
    const bubble = aiEl.querySelector(".bubble");
    let acc = "";
    let paint = 0;
    const [pid, model] = ($("#chatModel").value || "||").split("||");
    const body = {
      video_id: state.cur.id,
      chat_id: state.chatId,
      text,
      provider: pid,
      model,
      think: $("#thinkToggle").checked,
      include_frames: $("#frameToggle").checked,
      selection: state.chatSel,
      glossary: $("#uiGlossary").value || "",
    };
    try {
      const resp = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const cid = resp.headers.get("X-Chat-Id");
      if (cid) state.chatId = cid;
      if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += dec.decode(value, { stream: true });
        if (Date.now() - paint > 90) {
          paint = Date.now();
          bubble.innerHTML = md(acc) + '<span class="cursor"></span>';
          $("#chatList").scrollTop = $("#chatList").scrollHeight;
        }
      }
      bubble.innerHTML = md(acc);
    } catch (e) {
      bubble.innerHTML = md(acc + `\n\n> 出错了：${e.message}`);
    }
    $("#chatList").scrollTop = $("#chatList").scrollHeight;
  }

  /* ------------------------------------------------ 关键词 */
  async function searchKeywords() {
    if (!state.cur) return;
    const kw = $("#kwInput").value.trim();
    if (!kw) return toast("先输入关键词", "err");
    const box = $("#kwResults");
    box.innerHTML = '<div class="chat-empty">检索中…</div>';
    try {
      const r = await api(`/api/videos/${state.cur.id}/keywords`, {
        method: "POST", body: { keywords: kw, use_ai: $("#kwAi").checked },
      });
      if (!r.hits?.length) {
        box.innerHTML = '<div class="chat-empty">没有找到相关内容，换个关键词试试。</div>';
        return;
      }
      box.innerHTML = "";
      r.hits.forEach((h) => {
        const el = document.createElement("div");
        el.className = "kw-hit";
        el.innerHTML = `<div class="kh"><span>${fmtTime(h.start)}</span><span class="seg">${esc(h.segment || "")}</span></div>
          <p>${esc(h.text)}</p>${h.why ? `<div class="why">${esc(h.why)}</div>` : ""}`;
        el.addEventListener("click", () => seekTo(h.start + 0.02));
        box.appendChild(el);
      });
      toast(`找到 ${r.hits.length} 处${r.used_ai ? "（AI 精筛）" : ""}`, "ok");
    } catch (e) {
      box.innerHTML = `<div class="chat-empty">${esc(e.message)}</div>`;
    }
  }

  /* ------------------------------------------------ 分析 */
  function openAnalyze() {
    if (!state.cur) return;
    const ui = state.settings.ui;
    const meta = state.cur.meta || {};
    $("#anSub").value = meta.subtitle_path || "";
    $("#anTrLang").value = ui.translation_lang || "zh";
    $("#anTerm").value = ui.term_policy || "keep_en";
    $("#anSkip").checked = false;
    const roles = state.settings.roles;
    const pname = (pid) => state.settings.providers.find((p) => p.id === pid)?.name || pid;
    $("#anRoleNote").innerHTML = [
      `语音识别：${esc(pname(roles.asr.provider))} / ${esc(roles.asr.model)}`,
      `画面纠错：${esc(pname(roles.refine.provider))} / ${esc(roles.refine.model)}`,
      `分段摘要：${esc(pname(roles.text.provider))} / ${esc(roles.text.model)}`,
      `<span class="muted">分段由文本模型按教学内容自动判断。想换模型去「设置」。</span>`,
    ].join("<br>");
    $("#anProgress").classList.add("hidden");
    $("#btnStartAnalyze").disabled = false;
    showModal("modalAnalyze");
  }

  async function startAnalyze() {
    if (!state.cur) return;
    const btn = $("#btnStartAnalyze");
    btn.disabled = true;
    const body = {
      subtitle_path: $("#anSub").value.trim(),
      translation_lang: $("#anTrLang").value,
      term_policy: $("#anTerm").value,
      skip_refine: $("#anSkip").checked,
      glossary: $("#uiGlossary").value || "",
    };
    try {
      const r = await api(`/api/videos/${state.cur.id}/analyze`, { method: "POST", body });
      $("#anProgress").classList.remove("hidden");
      pollJob(r.job.id, "modal");
    } catch (e) {
      toast("启动失败：" + e.message, "err");
      btn.disabled = false;
    }
  }

  function pollJob(jid, where) {
    if (state.jobTimer) clearInterval(state.jobTimer);
    state.jobTimer = setInterval(async () => {
      let j;
      try { j = await api(`/api/jobs/${jid}`); } catch (e) { return; }
      const pct = Math.round(j.progress || 0);
      if (where === "modal") {
        $("#anProgressFill").style.width = pct + "%";
        $("#anPct").textContent = pct + "%";
        $("#anMsg").textContent = j.message || j.status;
        $("#anLog").innerHTML = (j.log || []).map((l) =>
          `<div>${new Date(l.t * 1000).toLocaleTimeString()} · ${esc(l.msg)}</div>`).join("");
        $("#anLog").scrollTop = $("#anLog").scrollHeight;
      }
      $("#jobBar").classList.remove("hidden");
      $("#jobFill").style.width = pct + "%";
      $("#jobText").textContent = `${j.message || ""} ${pct}%`;
      if (j.status === "done" || j.status === "failed" || j.status === "canceled") {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        btnEnable();
        $("#jobBar").classList.add("hidden");
        if (j.status === "done") {
          if (where === "modal") hideModal("modalAnalyze");
          toast("分析完成，字幕和摘要已生成", "ok");
          const id = state.cur.id;
          await openPlayer(id);
          loadVideos();
        } else if (j.status === "failed") {
          toast(j.message || "分析失败", "err");
        }
      }
    }, 900);
  }
  function btnEnable() { $("#btnStartAnalyze").disabled = false; }

  /* ------------------------------------------------ 设置 */
  function showModal(id) { $("#" + id).classList.remove("hidden"); }
  function hideModal(id) { $("#" + id).classList.add("hidden"); }

  function renderProviders() {
    const box = $("#providerList");
    box.innerHTML = "";
    state.settings.providers.forEach((p, i) => {
      const el = document.createElement("div");
      el.className = "provider" + (i === 0 ? " open" : "");
      el.dataset.i = i;
      const on = p.id === "ollama" || !!p.api_key;
      el.innerHTML = `
        <div class="provider-head">
          <span class="dot ${on ? "on" : ""}"></span>
          <span class="nm">${esc(p.name)}</span>
          <span class="muted small">${p.api_key ? "已配置" : (p.id === "ollama" ? "无需 Key" : "未配置")}</span>
        </div>
        <div class="provider-body">
          <label class="field"><span>名称</span><input id="name-${p.id}" data-k="name" value="${esc(p.name)}"></label>
          <label class="field"><span>Base URL</span><input id="base-${p.id}" data-k="base_url" value="${esc(p.base_url)}"></label>
          <label class="field"><span>API Key</span><input id="key-${p.id}" data-k="api_key" type="password" value="${esc(p.api_key)}" placeholder="${esc(p.name)} 的 Key" aria-label="${esc(p.name)} API Key"></label>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <label class="field"><span>对话 / 视觉模型（每行一个）</span><textarea id="models-${p.id}" data-k="models" rows="4">${esc((p.models || []).join("\n"))}</textarea></label>
            <label class="field"><span>语音识别模型</span><textarea id="asr-${p.id}" data-k="asr_models" rows="4">${esc((p.asr_models || []).join("\n"))}</textarea></label>
          </div>
          <div class="row between">
            <div class="prov-note">${p.notes ? esc(p.notes) : ""}${p.docs ? ` <a href="${esc(p.docs)}" target="_blank">获取 Key</a>` : ""}</div>
            <button class="btn ghost sm act-test">测试连接</button>
          </div>
        </div>`;
      el.querySelector(".provider-head").addEventListener("click", () => el.classList.toggle("open"));
      el.querySelectorAll("input,textarea").forEach((inp) => {
        inp.addEventListener("input", () => {
          const k = inp.dataset.k;
          if (k === "models" || k === "asr_models") p[k] = inp.value.split("\n").map((x) => x.trim()).filter(Boolean);
          else p[k] = inp.value;
        });
      });
      el.querySelector(".act-test").addEventListener("click", async (e) => {
        e.stopPropagation();
        await saveSettings(true);
        const model = (p.models || [])[0] || "";
        e.target.textContent = "测试中…";
        try {
          const r = await api("/api/settings/test", { method: "POST", body: { provider: p.id, model } });
          toast(r.ok ? `可用：${r.message}` : `不可用：${r.message}`, r.ok ? "ok" : "err");
          e.target.textContent = r.ok ? "连接正常" : "重试";
        } catch (err) {
          toast("测试失败：" + err.message, "err");
          e.target.textContent = "测试连接";
        }
      });
      box.appendChild(el);
    });
  }

  function fillRoleSelects() {
    const roles = state.settings.roles;
    ["asr", "refine", "text", "chat"].forEach((role) => {
      const sel = $("#role-" + role);
      const opts = modelOptions(role);
      const groups = {};
      opts.forEach((o) => (groups[o.pname] = groups[o.pname] || []).push(o));
      let html = Object.entries(groups).map(([pname, list]) =>
        `<optgroup label="${esc(pname)}">${list.map((o) =>
          `<option value="${o.pid}||${o.model}">${esc(o.model)}${o.ready ? "" : "（未填 Key）"}</option>`).join("")}</optgroup>`).join("");
      sel.innerHTML = html;
      const cur = `${roles[role]?.provider}||${roles[role]?.model}`;
      if (opts.some((o) => `${o.pid}||${o.model}` === cur)) sel.value = cur;
      else if (opts.length) {
        sel.selectedIndex = 0;
        const [pid, model] = sel.value.split("||");
        roles[role] = { provider: pid, model };
      }
      sel.onchange = () => {
        const [pid, model] = sel.value.split("||");
        roles[role] = { provider: pid, model };
        if (role === "chat") fillChatModels();
      };
    });
  }

  function fillUiFields() {
    const ui = state.settings.ui;
    $("#uiSubLang").value = ui.subtitle_lang || "zh";
    $("#uiTrLang").value = ui.translation_lang || "en";
    $("#uiTerm").value = ui.term_policy || "keep_en";
    $("#uiFrames").value = ui.frames_per_chunk ?? 4;
    $("#uiFont").value = ui.font_size || 26;
    $("#uiGlossary").value = ui.glossary || "";
  }

  async function saveSettings(silent = false) {
    const ui = state.settings.ui;
    ui.subtitle_lang = $("#uiSubLang").value;
    ui.translation_lang = $("#uiTrLang").value;
    ui.term_policy = $("#uiTerm").value;
    ui.frames_per_chunk = Number($("#uiFrames").value) || 0;
    ui.font_size = Number($("#uiFont").value) || 26;
    ui.glossary = $("#uiGlossary").value;
    try {
      const r = await api("/api/settings", { method: "POST", body: { providers: state.settings.providers, roles: state.settings.roles, ui } });
      state.settings.roles = r.settings.roles;
      state.settings.ui = r.settings.ui;
      applySubtitleStyle();
      fillRoleSelects();
      fillChatModels();
      if (!silent) toast("设置已保存", "ok");
      $("#saveHint").textContent = "已保存 " + new Date().toLocaleTimeString();
    } catch (e) { toast("保存失败：" + e.message, "err"); }
  }

  /* ------------------------------------------------ 导入 */
  async function importViaDialog() {
    try {
      const r = await api("/api/import/dialog", { method: "POST", body: { kind: "video" } });
      if (r.videos?.length) {
        toast("已导入 " + r.videos.length + " 个文件", "ok");
        await loadVideos();
        const v = r.videos.find((x) => x.ok)?.video;
        if (v) {
          state.cur = v;
          openAnalyze();
        }
      } else if (r.paths) {
        /* 选择文字稿 */
      } else {
        toast("没有导入文件", "err");
      }
    } catch (e) { toast("导入失败：" + e.message, "err"); }
  }

  /* ------------------------------------------------ 事件绑定 */
  function bind() {
    const video = $("#video");
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("durationchange", () => {
      $("#timeTotal").textContent = fmtTime(video.duration);
    });
    video.addEventListener("play", () => {
      $("#btnPlay").innerHTML = '<svg class="ico"><use href="#i-pause"/></svg>';
      flashPlay(true);
    });
    video.addEventListener("pause", () => {
      $("#btnPlay").innerHTML = '<svg class="ico"><use href="#i-play"/></svg>';
      flashPlay(false);
    });
    video.addEventListener("click", (e) => e.stopPropagation());
    $("#videoWrap").addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      togglePlay();
    });

    $("#btnPlay").addEventListener("click", togglePlay);
    $("#btnBack10").addEventListener("click", () => seekTo(video.currentTime - 10));
    $("#btnFwd10").addEventListener("click", () => seekTo(video.currentTime + 10));
    $("#speedSel").addEventListener("change", () => { video.playbackRate = parseFloat($("#speedSel").value); });
    $("#subToggle").addEventListener("change", () => renderSubtitle(state.activeCue));
    $("#btnFull").addEventListener("click", () => {
      const w = $("#videoWrap");
      if (document.fullscreenElement) document.exitFullscreen();
      else w.requestFullscreen?.();
    });
    $("#btnMarkA").addEventListener("click", () => mark("a"));
    $("#btnMarkB").addEventListener("click", () => mark("b"));
    $("#btnClearSel").addEventListener("click", () => {
      state.sel = { a: null, b: null }; state.chatSel = null;
      $("#chatContext").classList.add("hidden");
      renderSelection();
    });
    $("#btnAskSel").addEventListener("click", () => {
      if (!state.chatSel) return;
      switchTab("chat");
      $("#chatContext").classList.remove("hidden");
      $("#chatContextText").textContent = `已选中 ${fmtTime(state.chatSel.start)} - ${fmtTime(state.chatSel.end)}`;
      $("#chatInput").focus();
    });
    $("#btnCtxClear").addEventListener("click", () => {
      state.chatSel = null;
      $("#chatContext").classList.add("hidden");
      state.sel = { a: null, b: null };
      renderSelection();
    });

    $("#btnBack").addEventListener("click", async () => {
      video.pause();
      video.removeAttribute("src");
      video.load();
      $("#view-player").classList.add("hidden");
      $("#view-library").classList.remove("hidden");
      await loadVideos();
    });
    $("#brandHome").addEventListener("click", () => $("#btnBack").click());
    $("#btnReanalyze").addEventListener("click", openAnalyze);
    $("#btnStartAnalyze").addEventListener("click", startAnalyze);
    $("#btnPickSub").addEventListener("click", async () => {
      const r = await api("/api/import/dialog", { method: "POST", body: { kind: "subtitle" } });
      if (r.paths?.[0]) $("#anSub").value = r.paths[0];
    });
    $("#btnExportMenu").addEventListener("click", (e) => {
      e.stopPropagation();
      $("#exportMenu").classList.toggle("open");
    });
    document.addEventListener("click", () => $("#exportMenu").classList.remove("open"));
    $$("#exportMenu button").forEach((b) => b.addEventListener("click", () => {
      if (state.cur) window.open(`/api/videos/${state.cur.id}/export?fmt=${b.dataset.fmt}`, "_blank");
    }));
    $("#btnExport").addEventListener("click", () => {
      if (state.cur) window.open(`/api/videos/${state.cur.id}/export?fmt=srt`, "_blank");
    });

    $("#btnSettings").addEventListener("click", () => {
      renderProviders(); fillRoleSelects(); fillUiFields();
      $("#saveHint").textContent = "";
      showModal("modalSettings");
    });
    $("#btnSaveSettings").addEventListener("click", () => saveSettings(false));
    $("#btnAddProvider").addEventListener("click", () => {
      state.settings.providers.push({
        id: "custom_" + Date.now().toString(36), name: "自定义服务商", base_url: "https://", api_key: "",
        models: [], asr_models: [], vision: true, notes: "", docs: "",
      });
      renderProviders();
      fillRoleSelects();
    });
    $$("[data-close]").forEach((b) => b.addEventListener("click", () => hideModal(b.dataset.close)));
    $$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); }));

    $("#btnImport").addEventListener("click", importViaDialog);
    $("#btnImport2").addEventListener("click", importViaDialog);
    $("#btnImport3").addEventListener("click", importViaDialog);
    $("#libSearch").addEventListener("input", renderLibrary);

    $("#findCue").addEventListener("input", (e) => {
      clearTimeout(window.__findT);
      window.__findT = setTimeout(() => renderCueList(e.target.value), 180);
    });
    $("#btnFollow").addEventListener("click", (e) => {
      state.follow = !state.follow;
      e.target.textContent = state.follow ? "跟随播放" : "已取消跟随";
    });

    $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
    $("#btnSend").addEventListener("click", sendChat);
    $("#chatInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
    });
    $("#chatInput").addEventListener("input", (e) => {
      e.target.style.height = "auto";
      e.target.style.height = Math.min(110, e.target.scrollHeight) + "px";
    });
    $("#chatModel").addEventListener("change", () => {
      const [pid, model] = $("#chatModel").value.split("||");
      state.settings.roles.chat = { provider: pid, model };
      api("/api/settings", { method: "POST", body: { roles: state.settings.roles } }).catch(() => {});
    });
    $("#btnKwSearch").addEventListener("click", searchKeywords);
    $("#kwInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.ctrlKey) searchKeywords(); });
    $("#btnKeywordsJump").addEventListener("click", () => switchTab("keywords"));
    $("#btnNewChat").addEventListener("click", () => {
      state.chatId = null;
      $("#chatList").innerHTML = '<div class="chat-empty">新对话已开始。<br>选一个片段，或直接问整节课的问题。</div>';
      $("#chatInput").focus();
    });

    initTimeline();

    document.addEventListener("keydown", (e) => {
      if ($("#view-player").classList.contains("hidden")) return;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
      if (e.code === "Space") { e.preventDefault(); togglePlay(); }
      else if (e.key === "ArrowLeft") seekTo(video.currentTime - 5);
      else if (e.key === "ArrowRight") seekTo(video.currentTime + 5);
      else if (e.key === "[") mark("a");
      else if (e.key === "]") mark("b");
    });
  }

  function switchTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".tab-body").forEach((b) => b.classList.add("hidden"));
    $("#tab-" + name).classList.remove("hidden");
    if (name === "chat") setTimeout(() => $("#chatList").scrollTop = $("#chatList").scrollHeight, 30);
  }

  /* ------------------------------------------------ 启动 */
  async function init() {
    bind();
    try {
      const s = await api("/api/settings");
      state.settings = s.settings;
      $("#envHint").textContent = s.ffmpeg
        ? `v${s.version} · 本地运行 · 数据只保存在本机`
        : "未检测到 ffmpeg，请先运行 setup.bat 安装";
      fillChatModels();
      applySubtitleStyle();
    } catch (e) {
      toast("无法连接本地服务：" + e.message, "err");
    }
    await loadVideos();
    const m = location.hash.match(/v=([a-z0-9]+)/i);
    if (m) openPlayer(m[1]).catch(() => {});
  }

  init();
})();
