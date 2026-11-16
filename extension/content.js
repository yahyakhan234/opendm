// "Download this video" button, shown over videos when you hover them.
// Runs in every page and frame. The list of qualities comes from the background
// script, which watches what the page loads.
(() => {
  if (window.__opendmVideoButton) return;
  window.__opendmVideoButton = true;

  const B = globalThis.browser ?? globalThis.chrome;
  const MIN_W = 200;
  const MIN_H = 110;
  let enabled = true;
  let current = null; // the <video> the button is attached to
  let hideTimer = 0;
  let menuOpen = false;

  B.storage.local.get({ showVideoButton: true }).then((s) => (enabled = s.showVideoButton));
  B.storage.onChanged.addListener((c) => {
    if (c.showVideoButton) {
      enabled = c.showVideoButton.newValue;
      if (!enabled) hide(true);
    }
  });

  // ---- UI (inside a shadow root so page CSS can't touch it) -------------------
  const host = document.createElement("opendm-video-button");
  host.style.cssText = "all:initial;position:fixed;z-index:2147483647;top:0;left:0;display:none;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }
      .btn { display:flex; align-items:center; gap:7px; background:#1b1e25f2; color:#fff; border:1px solid #3b82f6;
             border-radius:8px; padding:6px 11px 6px 8px; font-size:13px; font-weight:600; cursor:pointer;
             box-shadow:0 4px 14px #0007; user-select:none; white-space:nowrap; }
      .btn:hover { background:#3b82f6; }
      .btn svg { flex:none; }
      .menu { position:absolute; right:0; top:calc(100% + 6px); min-width:220px; max-height:320px; overflow:auto;
              background:#1b1e25; border:1px solid #2e3340; border-radius:10px; padding:6px; display:none;
              box-shadow:0 10px 30px #000a; }
      .menu.open { display:block; }
      .item { display:flex; justify-content:space-between; align-items:center; gap:14px; width:100%;
              background:none; border:0; color:#e7e9ee; padding:8px 10px; border-radius:7px; cursor:pointer;
              font-size:13px; text-align:left; }
      .item:hover:not([disabled]) { background:#3b82f6; }
      .item[disabled] { cursor:default; color:#8b93a3; }
      .item b { font-weight:600; }
      .item span { color:#aab2c0; font-size:12px; }
      .item:hover:not([disabled]) span { color:#e8eefc; }
      .msg { color:#8b93a3; font-size:12px; padding:8px 10px; line-height:1.4; }
      .msg.ok { color:#22c55e; } .msg.err { color:#ef4444; }
    </style>
    <div class="btn" part="button" title="Download this video with OpenDM">
      <svg width="18" height="18" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#3b82f6"/>
        <path d="M12 5v9m0 0l-4-4m4 4l4-4M7 18h10" stroke="#fff" stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Download this video
    </div>
    <div class="menu"></div>`;
  const btn = root.querySelector(".btn");
  const menu = root.querySelector(".menu");

  function mount() {
    const parent = document.fullscreenElement && document.fullscreenElement.tagName !== "VIDEO"
      ? document.fullscreenElement
      : document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }

  // The area to treat as "the video". Some players (YouTube before you press play) park
  // the <video> element off-screen and show a thumbnail, so fall back to the player box.
  function areaOf(v) {
    const r = v.getBoundingClientRect();
    const visibleH = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    if (r.width >= MIN_W && visibleH >= Math.min(r.height, MIN_H)) return r;
    const box = v.closest(".html5-video-player") || v.parentElement?.parentElement || v.parentElement;
    return box ? box.getBoundingClientRect() : r;
  }

  function place() {
    if (!current) return;
    const r = areaOf(current);
    if (r.width < MIN_W || r.height < MIN_H || r.bottom < 0 || r.top > innerHeight) return hide(true);
    mount();
    host.style.display = "block";
    const w = host.offsetWidth || 190;
    host.style.left = `${Math.max(4, Math.min(innerWidth - w - 4, r.right - w - 10))}px`;
    host.style.top = `${Math.max(4, r.top + 10)}px`;
  }

  function hide(force) {
    if (menuOpen && !force) return;
    host.style.display = "none";
    menu.classList.remove("open");
    menuOpen = false;
    current = null;
  }

  function videoAt(x, y) {
    for (const v of document.querySelectorAll("video")) {
      const r = areaOf(v);
      if (r.width >= MIN_W && r.height >= MIN_H && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return v;
    }
    return null;
  }

  // Players put overlays on top of <video>, so hover is tracked by position.
  document.addEventListener(
    "pointermove",
    (e) => {
      if (!enabled) return;
      if (e.composedPath().includes(host)) {
        clearTimeout(hideTimer);
        return;
      }
      const v = videoAt(e.clientX, e.clientY);
      if (v) {
        if (v !== current) {
          current = v;
          menu.classList.remove("open");
          menuOpen = false;
        }
        place();
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => hide(false), 2500);
      } else if (!menuOpen) {
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => hide(false), 400);
      }
    },
    { passive: true, capture: true }
  );
  addEventListener("scroll", () => current && place(), { passive: true, capture: true });
  addEventListener("resize", () => current && place(), { passive: true });
  document.addEventListener("fullscreenchange", () => current && place());
  document.addEventListener(
    "pointerdown",
    (e) => {
      if (menuOpen && !e.composedPath().includes(host)) hide(true);
    },
    true
  );

  function srcOf(v) {
    const s = v && (v.currentSrc || v.src);
    return s && /^https?:/.test(s) ? s : null;
  }

  function message(text, cls = "") {
    menu.innerHTML = "";
    const d = document.createElement("div");
    d.className = `msg ${cls}`;
    d.textContent = text;
    menu.appendChild(d);
  }

  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (menuOpen) {
      menu.classList.remove("open");
      menuOpen = false;
      return;
    }
    menuOpen = true;
    clearTimeout(hideTimer);
    menu.classList.add("open");
    const youtube = /(^|\.)(youtube\.com|youtube-nocookie\.com)$/i.test(location.hostname);
    message(youtube ? "Asking yt-dlp for the available qualities…" : "Looking for videos…");
    const fallbackSrc = srcOf(current);
    const pageUrl = location.href;
    let options = [];
    let note = null;
    let isError = false;
    try {
      ({ options, message: note, error: isError } = await B.runtime.sendMessage({ type: "media:options", fallbackSrc, pageUrl }));
    } catch {}
    if (!options || !options.length) {
      message(note || "No downloadable video found yet. Start playing the video, then try again.", isError ? "err" : "");
      return;
    }
    menu.innerHTML = "";
    for (const o of options) {
      const item = document.createElement("button");
      item.className = "item";
      item.disabled = !!o.disabled;
      item.innerHTML = "<b></b><span></span>";
      item.querySelector("b").textContent = o.label;
      item.querySelector("span").textContent = o.sub || "";
      item.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        message("Sending to OpenDM…");
        const r = await B.runtime.sendMessage({ type: "media:download", optionId: o.id, fallbackSrc, pageUrl });
        if (r && r.ok) {
          message("✓ Sent to OpenDM", "ok");
          setTimeout(() => hide(true), 1500);
        } else {
          message(/reach|fetch|timed? ?out|NetworkError|replied/i.test(r?.error || "")
            ? "OpenDM isn't running. Open the app and try again."
            : r?.error || "Something went wrong.", "err");
        }
      });
      menu.appendChild(item);
    }
  });
})();
