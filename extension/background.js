// OpenDM browser integration.
// Catches downloads the browser starts and hands them to the OpenDM desktop app
// (with the page's cookies and referrer so logged-in downloads keep working).
// If the app isn't running, the browser just downloads the file normally.

// Firefox exposes `browser`, Chromium exposes `chrome`; both are promise-based in MV3.
const B = globalThis.browser ?? globalThis.chrome;

// The Chrome Web Store doesn't allow YouTube downloaders, so the Chrome store build
// (tools/package_extension.py) switches this off. Firefox and unpacked builds keep it.
const YOUTUBE_SUPPORT = true;

const DEFAULTS = { enabled: true, port: 6801, minSizeKB: 0, skipExtensions: "" };
const HEADER = { "X-OpenDM-Client": "extension" };

// Cached so the download hook can act instantly (a fast file can finish
// before an async storage read returns).
let cached = { ...DEFAULTS };
const ready = B.storage.local.get(DEFAULTS).then((s) => (cached = s));
B.storage.onChanged.addListener((changes) => {
  for (const [k, { newValue }] of Object.entries(changes)) cached[k] = newValue;
});

async function settings() {
  await ready;
  return cached;
}

async function api(path, body, timeoutMs = 2000) {
  const { port } = await settings();
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: body ? "POST" : "GET",
    headers: body ? { ...HEADER, "Content-Type": "application/json" } : HEADER,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`OpenDM replied ${res.status}`);
  return res.json();
}

async function appRunning() {
  try {
    await api("/ping");
    return true;
  } catch {
    return false;
  }
}

async function cookieHeader(url) {
  let cookies = [];
  try {
    cookies = await B.cookies.getAll({ url });
  } catch {
    // Firefox with first-party isolation needs firstPartyDomain; Chromium rejects that key.
    try {
      cookies = await B.cookies.getAll({ url, firstPartyDomain: null });
    } catch {}
  }
  return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
}

async function sendToApp(url, extra = {}) {
  return api("/add", {
    url,
    referrer: extra.referrer || "",
    filename: extra.filename || "",
    size: extra.size || 0,
    cookies: await cookieHeader(url),
    useragent: navigator.userAgent,
    ...(extra.kind ? { kind: extra.kind } : {}),
    ...(extra.quality ? { quality: extra.quality } : {}),
  });
}

function baseName(path) {
  return (path || "").split(/[\\/]/).pop();
}

// Downloads we deliberately let the browser handle (e.g. app not running).
const passThrough = new Set();
// URLs we are re-starting in the browser ourselves (Firefox fallback, see release()).
const passUrls = new Set();

B.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url;
  if (passUrls.delete(url)) return;
  if (!cached.enabled || passThrough.has(item.id) || !/^https?:/i.test(url)) return;
  if (item.state !== "in_progress") return;
  if (item.incognito) return; // keep private-window downloads out of the app's history

  // Hold the browser's copy first, then decide.
  try {
    await B.downloads.pause(item.id);
  } catch {
    return; // already finished or failed - the browser keeps it
  }
  // Give the download back to the browser.
  const release = async () => {
    passThrough.add(item.id);
    try {
      await B.downloads.resume(item.id);
      return;
    } catch {}
    // Firefox can't resume a download that was paused before any data arrived,
    // so cancel that copy and start it again as a normal browser download.
    try {
      await B.downloads.cancel(item.id);
      await B.downloads.erase({ id: item.id });
    } catch {}
    passUrls.add(url);
    try {
      await B.downloads.download({ url });
    } catch {
      passUrls.delete(url);
    }
  };

  const s = await settings();
  const ext = (baseName(item.filename) || new URL(url).pathname).split(".").pop().toLowerCase();
  const skip = s.skipExtensions.split(/[\s,]+/).filter(Boolean).map((e) => e.replace(/^\./, "").toLowerCase());
  if (!s.enabled || skip.includes(ext)) return release();
  if (s.minSizeKB > 0 && item.totalBytes > 0 && item.totalBytes < s.minSizeKB * 1024) return release();

  let handed = false;
  try {
    await sendToApp(url, {
      referrer: item.referrer,
      filename: baseName(item.filename),
      size: item.totalBytes > 0 ? item.totalBytes : 0, // Firefox reports -1 when unknown
    });
    handed = true;
  } catch {
    handed = false;
  }

  if (handed) {
    try {
      await B.downloads.cancel(item.id);
      await B.downloads.erase({ id: item.id });
    } catch {}
  } else {
    await release();
  }
});

// ---- Right-click menu --------------------------------------------------------
function buildMenus() {
  B.contextMenus.removeAll(() => {
    B.contextMenus.create({
      id: "opendm-link",
      title: "Download with OpenDM",
      contexts: ["link", "image", "video", "audio"],
    });
    B.contextMenus.create({
      id: "opendm-all-links",
      title: "Download all links on this page with OpenDM…",
      contexts: ["page"],
    });
  });
}
B.runtime.onInstalled.addListener(async () => {
  buildMenus();
  // Firefox can install MV3 extensions without host access; ask for it up front.
  if (!(await B.permissions.contains({ origins: ["<all_urls>"] }))) {
    B.tabs.create({ url: B.runtime.getURL("popup.html?setup=1") });
  }
});
B.runtime.onStartup.addListener(buildMenus);

B.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "opendm-link") {
    const url = info.linkUrl || info.srcUrl;
    try {
      await sendToApp(url, { referrer: info.pageUrl });
    } catch {
      notifyOffline(tab);
    }
  } else if (info.menuItemId === "opendm-all-links" && tab?.id) {
    try {
      await grabLinks(tab);
    } catch {
      notifyOffline(tab);
    }
  }
});

// Collect links to files on the page and send them as one batch
// (the app opens a single "Add download" window listing them all).
const FILE_EXT = /\.(zip|rar|7z|tar|gz|bz2|xz|iso|exe|msi|apk|dmg|deb|rpm|appimage|mp4|mkv|avi|mov|webm|flv|wmv|m4v|mp3|flac|wav|aac|ogg|m4a|opus|pdf|epub|docx?|xlsx?|pptx?|csv|torrent|bin|img)$/i;

async function grabLinks(tab) {
  const [{ result }] = await B.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => [...document.querySelectorAll("a[href]")].map((a) => a.href),
  });
  const urls = [...new Set(result || [])].filter((u) => {
    try {
      const p = new URL(u);
      return /^https?:$/.test(p.protocol) && FILE_EXT.test(p.pathname);
    } catch {
      return false;
    }
  });
  if (!urls.length) return { ok: true, count: 0 };
  await api("/add", { urls, referrer: tab.url, cookies: await cookieHeader(urls[0]), useragent: navigator.userAgent });
  return { ok: true, count: urls.length };
}

function notifyOffline(tab) {
  B.action.setBadgeBackgroundColor({ color: "#ef4444" });
  B.action.setBadgeText({ text: "off" });
  setTimeout(() => B.action.setBadgeText({ text: "" }), 4000);
}

// Popup and the in-page video button talk to us here.
B.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === "status") reply({ running: await appRunning() });
    else if (msg.type === "grab") {
      try {
        const [tab] = await B.tabs.query({ active: true, currentWindow: true });
        reply(await grabLinks(tab));
      } catch (e) {
        reply({ ok: false, error: String(e) });
      }
    } else if (msg.type === "media:options") {
      const tabId = msg.tabId ?? sender.tab?.id;
      const pageUrl = msg.pageUrl || sender.tab?.url || "";
      if (isYouTube(pageUrl)) {
        reply(YOUTUBE_SUPPORT ? await ytOptions(pageUrl) : {
          options: [],
          message: "YouTube isn't supported in the Chrome Web Store version. Paste the link into OpenDM's Add URL instead.",
        });
      }
      else reply({ options: await mediaOptions(tabId, msg.fallbackSrc) });
    } else if (msg.type === "media:download") {
      const tabId = msg.tabId ?? sender.tab?.id;
      try {
        if (msg.optionId.startsWith("yt:")) await ytDownload(tabId, msg.pageUrl, msg.optionId.slice(3));
        else await downloadOption(tabId, msg.optionId, msg.fallbackSrc);
        reply({ ok: true });
      } catch (e) {
        reply({ ok: false, error: String(e.message || e) });
      }
    } else if (msg.type === "settings") {
      reply(await settings());
    }
  })();
  return true;
});

// =============================================================================
// Video detection ("Download this video")
// =============================================================================
// We watch what each tab loads. HLS playlists (.m3u8) are fetched and parsed so we
// can offer each quality; plain video files are offered directly. DRM-protected and
// live streams are listed but can't be downloaded. YouTube is not supported.

const MIN_VIDEO_BYTES = 512 * 1024;
const MAX_ITEMS = 40;
const tabs = new Map(); // tabId -> { items: [], children: [] } (mirrored to storage.session)

async function tabState(tabId) {
  if (!tabs.has(tabId)) {
    let saved = null;
    try {
      saved = (await B.storage.session.get(`media:${tabId}`))[`media:${tabId}`];
    } catch {}
    if (!tabs.has(tabId)) tabs.set(tabId, saved || { items: [], children: [] });
  }
  return tabs.get(tabId);
}

function quiet(p) {
  if (p && typeof p.catch === "function") p.catch(() => {});
}

function persist(tabId) {
  const st = tabs.get(tabId);
  const count = st ? st.items.filter((i) => !i.drm && !i.live).length : 0;
  quiet(B.action.setBadgeBackgroundColor({ color: "#3b82f6", tabId }));
  quiet(B.action.setBadgeText({ text: count ? String(count) : "", tabId }));
  try {
    quiet(st ? B.storage.session.set({ [`media:${tabId}`]: st }) : B.storage.session.remove(`media:${tabId}`));
  } catch {}
}

function clearTab(tabId) {
  tabs.set(tabId, { items: [], children: [] });
  for (const k of [...seenPlaylists]) if (k.startsWith(`${tabId} `)) seenPlaylists.delete(k);
  persist(tabId);
}

B.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) clearTab(tabId);
});
B.tabs.onRemoved.addListener((tabId) => {
  tabs.delete(tabId);
  try {
    quiet(B.storage.session.remove(`media:${tabId}`));
  } catch {}
});

function header(details, name) {
  const h = (details.responseHeaders || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value || "" : "";
}

function unsupportedSite(url) {
  // YouTube serves video as signed, scrambled chunks - not something we can grab.
  return /(^|\.)(googlevideo\.com|youtube\.com)$/i.test(new URL(url).hostname);
}

B.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0 || d.statusCode >= 400 || !/^https?:/.test(d.url)) return;
    let path;
    try {
      if (unsupportedSite(d.url)) return;
      path = new URL(d.url).pathname.toLowerCase();
    } catch {
      return;
    }
    const type = header(d, "content-type").toLowerCase();
    if (type.includes("mpegurl") || path.endsWith(".m3u8")) {
      onPlaylist(d.tabId, d.url);
      return;
    }
    const isVideoType = /^(video|audio)\//.test(type) && !/mp2t|iso\.segment|mpegurl/.test(type);
    const isVideoExt = /\.(mp4|webm|mkv|mov|m4v|flv|ogv|mp3|m4a)$/.test(path);
    if (!isVideoType && !isVideoExt) return;
    if (/\.(ts|m4s|aac)$/.test(path) || /[?&](range|bytestart|byterange)=/i.test(d.url)) return; // stream chunks
    let size = 0;
    const cr = header(d, "content-range").match(/\/(\d+)$/);
    if (cr) size = +cr[1];
    else size = +header(d, "content-length") || 0;
    if (size && size < MIN_VIDEO_BYTES) return; // thumbnails, previews, ad pings
    addItem(d.tabId, { kind: "file", url: d.url, size, type });
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object", "sub_frame"] },
  ["responseHeaders"]
);

async function addItem(tabId, item) {
  const st = await tabState(tabId);
  if (st.children.includes(item.url)) return; // a quality of a playlist we already list
  const existing = st.items.find((i) => i.url === item.url);
  if (existing) {
    if (item.size && !existing.size) existing.size = item.size;
  } else {
    item.id = Math.random().toString(36).slice(2, 10);
    st.items.push(item);
    if (st.items.length > MAX_ITEMS) st.items.shift();
  }
  persist(tabId);
}

function attrs(s) {
  const o = {};
  for (const m of s.matchAll(/([A-Z0-9-]+)=("[^"]*"|[^,]*)/g)) o[m[1]] = m[2].replace(/^"|"$/g, "");
  return o;
}

function isDrmKey(a) {
  const method = (a.METHOD || "NONE").toUpperCase();
  const fmt = (a.KEYFORMAT || "identity").toLowerCase();
  return method !== "NONE" && !(method === "AES-128" && fmt === "identity");
}

function parsePlaylist(text, base) {
  if (!text.trimStart().startsWith("#EXTM3U")) return null;
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (text.includes("#EXT-X-STREAM-INF")) {
    const variants = [];
    const children = [];
    let drm = false;
    lines.forEach((l, i) => {
      if (l.startsWith("#EXT-X-SESSION-KEY:") && isDrmKey(attrs(l.slice(19)))) drm = true;
      if (l.startsWith("#EXT-X-MEDIA:")) {
        const a = attrs(l.slice(13));
        if (a.URI) children.push(new URL(a.URI, base).href);
      }
      if (l.startsWith("#EXT-X-STREAM-INF:")) {
        const a = attrs(l.slice(18));
        const uri = lines.slice(i + 1).find((x) => !x.startsWith("#"));
        if (!uri) return;
        children.push(new URL(uri, base).href);
        variants.push({
          height: +((a.RESOLUTION || "").split("x")[1] || 0),
          bandwidth: +(a["AVERAGE-BANDWIDTH"] || a.BANDWIDTH || 0),
        });
      }
    });
    // One entry per height, best bitrate first.
    const byHeight = new Map();
    for (const v of variants.sort((a, b) => b.bandwidth - a.bandwidth)) {
      if (!byHeight.has(v.height)) byHeight.set(v.height, v);
    }
    return { master: true, drm, children, variants: [...byHeight.values()].sort((a, b) => b.height - a.height) };
  }
  let drm = false;
  let duration = 0;
  for (const l of lines) {
    if (l.startsWith("#EXT-X-KEY:") && isDrmKey(attrs(l.slice(11)))) drm = true;
    if (l.startsWith("#EXTINF:")) duration += parseFloat(l.slice(8)) || 0;
  }
  const ended = text.includes("#EXT-X-ENDLIST") || /#EXT-X-PLAYLIST-TYPE:\s*VOD/.test(text);
  return { master: false, drm, duration, live: !ended };
}

const seenPlaylists = new Set();

async function onPlaylist(tabId, url) {
  const key = `${tabId} ${url}`;
  if (seenPlaylists.has(key)) return;
  seenPlaylists.add(key);
  const st = await tabState(tabId);
  if (st.children.includes(url)) return;
  let info;
  try {
    const res = await fetch(url, { credentials: "include" });
    info = parsePlaylist(await res.text(), url);
  } catch {
    return;
  }
  if (!info) return;
  if (info.master) {
    st.children.push(...info.children);
    st.items = st.items.filter((i) => !info.children.includes(i.url)); // seen before the master was parsed
    await addItem(tabId, { kind: "hls", url, variants: info.variants, drm: info.drm });
  } else {
    await addItem(tabId, { kind: "hls", url, variants: [], duration: info.duration, drm: info.drm, live: info.live });
  }
}

function fmtSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

function fmtDuration(s) {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

function fileExt(item) {
  const m = new URL(item.url).pathname.match(/\.([a-z0-9]{2,4})$/i);
  if (m) return m[1].toLowerCase();
  const t = ((item.type || "").split("/")[1] || "mp4").split(";")[0];
  return t.replace("x-matroska", "mkv").replace("quicktime", "mov");
}

// Flat list of choices for the menu: [{id, label, sub, disabled}]
async function mediaOptions(tabId, fallbackSrc) {
  const st = tabId != null ? await tabState(tabId) : { items: [] };
  const out = [];
  const hls = st.items.filter((i) => i.kind === "hls");
  const files = st.items.filter((i) => i.kind === "file").sort((a, b) => (b.size || 0) - (a.size || 0));
  for (const it of hls) {
    if (it.drm) {
      out.push({ id: it.id, label: "DRM-protected video", sub: "can't be downloaded", disabled: true });
    } else if (it.live) {
      out.push({ id: it.id, label: "Live stream", sub: "can't be downloaded", disabled: true });
    } else if (it.variants && it.variants.some((v) => v.height)) {
      for (const v of it.variants) {
        if (!v.height) continue;
        const parts = [v.height >= 720 ? "HD" : "", v.bandwidth ? `${(v.bandwidth / 1e6).toFixed(1)} Mbps` : ""];
        out.push({ id: `${it.id}:${v.height}`, label: `${v.height}p`, sub: parts.filter(Boolean).join(" · ") || "stream" });
      }
    } else {
      out.push({ id: it.id, label: "Video", sub: ["stream", fmtDuration(it.duration)].filter(Boolean).join(" · ") });
    }
  }
  for (const it of files) {
    out.push({ id: it.id, label: fileExt(it).toUpperCase(), sub: fmtSize(it.size) || "video file" });
  }
  if (!out.length && fallbackSrc && /^https?:/.test(fallbackSrc)) {
    out.push({ id: "src", label: "This video", sub: "direct file" });
  }
  return out;
}

async function downloadOption(tabId, optionId, fallbackSrc) {
  const tab = await B.tabs.get(tabId);
  const title = (tab.title || "video").replace(/\s+/g, " ").trim().slice(0, 120);
  if (optionId === "src") {
    return sendToApp(fallbackSrc, { referrer: tab.url, filename: `${title}.${fileExt({ url: fallbackSrc })}` });
  }
  const [itemId, height] = optionId.split(":");
  const st = await tabState(tabId);
  const it = st.items.find((i) => i.id === itemId);
  if (!it) throw new Error("That video is no longer available - reload the page");
  if (it.drm || it.live) throw new Error("This video can't be downloaded");
  if (it.kind === "hls") {
    return sendToApp(it.url, { referrer: tab.url, filename: title, kind: "hls", quality: height ? +height : 0 });
  }
  return sendToApp(it.url, { referrer: tab.url, filename: `${title}.${fileExt(it)}`, size: it.size });
}

// =============================================================================
// YouTube via yt-dlp
// =============================================================================
// The app runs yt-dlp to list real qualities (with sizes) and to do the download.

const YT_HOSTS = /(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$/i;
const ytTitles = new Map();

function isYouTube(url) {
  try {
    return YT_HOSTS.test(new URL(url).hostname);
  } catch {
    return false;
  }
}

// Normalise watch / shorts / embed / youtu.be links to one watch URL (drops playlist params).
function ytVideoUrl(pageUrl) {
  try {
    const u = new URL(pageUrl);
    if (u.hostname === "youtu.be") return `https://www.youtube.com/watch?v=${u.pathname.slice(1)}`;
    const v = u.searchParams.get("v");
    if (u.pathname === "/watch" && v) return `https://www.youtube.com/watch?v=${v}`;
    const m = u.pathname.match(/^\/(shorts|embed|live|v)\/([\w-]{6,})/);
    if (m) return `https://www.youtube.com/watch?v=${m[2]}`;
  } catch {}
  return null;
}

async function ytOptions(pageUrl) {
  const url = ytVideoUrl(pageUrl);
  if (!url) return { options: [], message: "Open a video first, then click the button." };
  let r;
  try {
    r = await api("/probe", { url }, 90000);
  } catch {
    return { options: [], message: "OpenDM isn't running. Open the app and try again.", error: true };
  }
  if (!r.ok) return { options: [], message: r.error, error: true };
  ytTitles.set(url, r.title);
  const options = r.options.map((o) => ({
    id: `yt:${o.height}`,
    label: `${o.height}p${o.fps > 30 ? Math.round(o.fps) : ""}`,
    sub: [o.height >= 2160 ? "4K" : o.height >= 720 ? "HD" : "", fmtSize(o.size)].filter(Boolean).join(" · ") || "video",
  }));
  options.push({ id: "yt:audio", label: "Audio only", sub: ["M4A", fmtSize(r.audio_size)].filter(Boolean).join(" · ") });
  return { options };
}

async function ytDownload(tabId, pageUrl, quality) {
  if (!YOUTUBE_SUPPORT) throw new Error("YouTube isn't supported in this version.");
  if (!pageUrl && tabId != null) pageUrl = (await B.tabs.get(tabId)).url;
  const url = ytVideoUrl(pageUrl || "");
  if (!url) throw new Error("Open a video first, then click the button.");
  const title = ytTitles.get(url) || "";
  return api("/add", {
    url,
    kind: "ytdlp",
    quality: quality === "audio" ? "audio" : +quality,
    filename: title,
    referrer: pageUrl,
  });
}
