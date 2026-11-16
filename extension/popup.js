// Firefox exposes `browser`, Chromium exposes `chrome`; both are promise-based in MV3.
const B = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = { enabled: true, port: 6801, minSizeKB: 0, skipExtensions: "", showVideoButton: true };
const $ = (id) => document.getElementById(id);

async function refreshStatus() {
  const { running } = await B.runtime.sendMessage({ type: "status" });
  $("dot").className = "dot " + (running ? "on" : "off");
  $("statusText").textContent = running ? "Connected to the app" : "App not running - browser will download normally";
}

const ALL = { origins: ["<all_urls>"] };

async function checkPermission() {
  const ok = await B.permissions.contains(ALL);
  $("perm").classList.toggle("show", !ok);
  return ok;
}

async function init() {
  if (new URLSearchParams(location.search).has("setup")) document.body.classList.add("tab");
  $("grant").addEventListener("click", async () => {
    // Must run directly from the click (user gesture) - no awaits before it.
    const granted = await B.permissions.request(ALL);
    if (granted) {
      await checkPermission();
      refreshStatus();
    }
  });
  await checkPermission();

  const s = await B.storage.local.get(DEFAULTS);
  $("enabled").checked = s.enabled;
  $("showVideoButton").checked = s.showVideoButton;
  $("showVideoButton").addEventListener("change", (e) => B.storage.local.set({ showVideoButton: e.target.checked }));
  $("minSizeKB").value = s.minSizeKB;
  $("skipExtensions").value = s.skipExtensions;
  $("port").value = s.port;

  $("enabled").addEventListener("change", (e) => B.storage.local.set({ enabled: e.target.checked }));
  $("minSizeKB").addEventListener("change", (e) => B.storage.local.set({ minSizeKB: Math.max(0, +e.target.value || 0) }));
  $("skipExtensions").addEventListener("change", (e) => B.storage.local.set({ skipExtensions: e.target.value }));
  $("port").addEventListener("change", async (e) => {
    await B.storage.local.set({ port: +e.target.value || 6801 });
    refreshStatus();
  });

  $("grab").addEventListener("click", async () => {
    $("msg").textContent = "Looking for file links…";
    const r = await B.runtime.sendMessage({ type: "grab" });
    if (!r.ok) $("msg").textContent = "Couldn't reach OpenDM. Is the app open?";
    else if (!r.count) $("msg").textContent = "No file links found on this page.";
    else $("msg").textContent = `Sent ${r.count} link${r.count > 1 ? "s" : ""} to OpenDM.`;
  });

  refreshStatus();
  loadVideos();
}

async function loadVideos() {
  const [tab] = await B.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  const pageUrl = tab.url || "";
  const list = $("videoList");
  if (/^https?:\/\/([^/]+\.)?(youtube\.com|youtu\.be)\//i.test(pageUrl)) {
    $("videos").hidden = false;
    list.textContent = "Asking yt-dlp for qualities…";
  }
  const { options = [], message } = await B.runtime.sendMessage({ type: "media:options", tabId: tab.id, pageUrl });
  $("videos").hidden = !options.length && !message;
  list.innerHTML = "";
  if (!options.length && message) list.textContent = message;
  for (const o of options) {
    const b = document.createElement("button");
    b.className = "vid";
    b.disabled = !!o.disabled;
    b.innerHTML = "<b></b><span></span>";
    b.querySelector("b").textContent = o.label;
    b.querySelector("span").textContent = o.sub || "";
    b.addEventListener("click", async () => {
      const r = await B.runtime.sendMessage({ type: "media:download", tabId: tab.id, optionId: o.id, pageUrl });
      $("msg").textContent = r.ok ? `Sent ${o.label} to OpenDM.` : r.error || "Couldn't reach OpenDM. Is the app open?";
    });
    list.appendChild(b);
  }
}

init();
