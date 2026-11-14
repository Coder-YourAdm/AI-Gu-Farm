#!/usr/bin/env node
/* 真实 Chrome/Edge 浏览器验收（C2 三处交互 / C6 互跳 / C9 整条路径 / C13 六格布局）
 *
 * 为什么需要它：docs/demo/check_live*.js 用的是轻量 DOM 桩，全绿只能证明「脚本层不塌」。
 * 本脚本通过 CDP 驱动真实浏览器渲染，发真实鼠标事件，并抓 JS 异常 / console.error / HTTP 4xx。
 * 早先用 --screenshot / --dump-dom 在沙箱里不落盘，改走 CDP 的 Page.captureScreenshot
 * （base64 回传内存）后可用。零 npm 包、零外网。
 *
 * 用法：
 *   node docs/demo/check_browser.js            # 只验，不写文件（已接入 check_all.ps1）
 *   node docs/demo/check_browser.js --shots    # 额外把 7 张截图写到 docs/demo/shots/
 *   node docs/demo/check_browser.js --strict   # CDP 起不来时按失败退 1（CI / 发布前用）
 *   CHROME_PATH=... node docs/demo/check_browser.js   # 指定浏览器路径
 *
 * 脚本自带临时静态服务（随机端口），不依赖外部 8123；退出时关闭浏览器与服务。
 * 边界：CDP 派发的是真实鼠标事件、真实渲染，但仍不是「人眼看投影可读性」，
 * 也不能替代 C12 彩排计时。未找到浏览器、或 CDP 起不来时打印 SKIP 并退出 0
 * （环境问题不阻塞其它自检）；页面断言失败始终退出 1。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const os = require("os");
const http = require("http");
const net = require("net");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const WEB = path.join(ROOT, "web");
const SHOTS = path.join(ROOT, "docs", "demo", "shots");
const WANT_SHOTS = process.argv.includes("--shots");
const PAGES = ["cover.html", "tutor.html", "compare.html"];
const SIZES = [[1280, 720], [1920, 1080]];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const STRICT = process.argv.includes("--strict");

/* CDP 起不来是环境问题（无头浏览器被拦、调试端口不通、沙箱限制），
   不该把整套自检变红：打印 SKIP 并退 0；加 --strict 时仍按失败退 1。 */
function skip(reason) {
  console.log("SKIP: " + reason);
  console.log("      浏览器层（C2 三处点击 / C6 互跳 / C9 整条路径 / C13 六格）本轮未验证，");
  console.log("      属环境限制，不是页面缺陷；请在能起浏览器的机器上补跑，或加 --strict 强制失败。");
  if (STRICT) {
    console.log("      --strict 已开启：按失败处理。");
    process.exit(1);
  }
  process.exit(0);
}

const MIME = { ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".ico": "image/x-icon", ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml" };

function startServer() {
  return new Promise((resolve, reject) => {
    const srv = http.createServer((req, res) => {
      let p;
      try { p = decodeURIComponent(new URL(req.url, "http://x").pathname); }
      catch (_) { res.writeHead(400).end(); return; }
      if (p === "/") p = "/index.html";
      const f = path.normalize(path.join(WEB, p));
      if (!f.startsWith(WEB)) { res.writeHead(403).end(); return; }
      fs.readFile(f, (e, b) => {
        if (e) { res.writeHead(404, { "Content-Type": "text/plain" }).end("404"); return; }
        res.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream",
          "Cache-Control": "no-store" });
        res.end(b);
      });
    });
    srv.listen(0, "127.0.0.1", () => resolve({ srv, port: srv.address().port }));
    srv.on("error", reject);
  });
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
    s.on("error", reject);
  });
}

function chromePath() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const cands = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  ];
  for (const c of cands) if (fs.existsSync(c)) return c;
  return null;
}

function profileDir() {
  // 每次运行用独立目录：连续两次跑时，上一个 Chrome 可能还没退干净，
  // 复用同一个 user-data-dir 会被判为「配置正在使用」而拒绝启动。
  const d = "D:/WorkBuddy-temp/outputs/headless/wb-profile-" + process.pid + "-" + Date.now();
  try { fs.mkdirSync(d, { recursive: true }); return d; }
  catch (_) { return path.join(os.tmpdir(), "wb-cdp-profile-" + process.pid); }
}
function cleanupProfile(d) {
  try { fs.rmSync(d, { recursive: true, force: true }); } catch (_) {}
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []; }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) {
        const p = c.pending.get(m.id); c.pending.delete(m.id);
        m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
      } else if (m.method) c.events.push(m);
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("JS: " + (r.exceptionDetails.text || ""));
    return r.result.value;
  }
  clear() { this.events.length = 0; }
  errors() {
    const out = [];
    for (const e of this.events) {
      if (e.method === "Runtime.exceptionThrown")
        out.push("JS异常: " + (e.params.exceptionDetails.text || "") + " " +
          (e.params.exceptionDetails.exception?.description || "").slice(0, 140));
      else if (e.method === "Log.entryAdded" && e.params.entry.level === "error")
        out.push("console.error: " + e.params.entry.text.slice(0, 180));
      else if (e.method === "Network.responseReceived" && e.params.response.status >= 400)
        out.push("HTTP " + e.params.response.status + " " + e.params.response.url);
    }
    return [...new Set(out)];
  }
}

const LAYOUT_JS = `(() => {
  const de = document.documentElement, vw = innerWidth, vh = innerHeight;
  const bad = [...document.querySelectorAll('body *')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (r.right > vw + 1 || r.left < -1);
  }).slice(0, 6).map(e => ({ tag:e.tagName, id:e.id, right:Math.round(e.getBoundingClientRect().right) }));
  const text = document.body ? document.body.innerText : '';
  return { vw, vh, sw:de.scrollWidth, sh:de.scrollHeight,
    overflowX: de.scrollWidth > vw + 1, overflowY: de.scrollHeight > vh + 1,
    textLen: text.length,
    hasFixtureNotice: text.indexOf('内联示例') >= 0 || text.indexOf('这是示例数据') >= 0,
    hasReference: (text + ' ' + document.title).toLowerCase().indexOf('reference') >= 0,
    bad };
})()`;

(async () => {
  const exe = chromePath();
  if (!exe) {
    console.log("SKIP: 未找到 Chrome/Edge（可用 CHROME_PATH 指定）。真人浏览器验收需在另一台机器补做。");
    process.exit(0);
  }
  if (WANT_SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  const { srv, port: httpPort } = await startServer();
  const BASE = `http://127.0.0.1:${httpPort}`;
  const cdpPort = await freePort();
  const profile = profileDir();
  const chrome = spawn(exe, [
    "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
    "--disable-dev-shm-usage", `--remote-debugging-port=${cdpPort}`,
    "--user-data-dir=" + profile, "--window-size=1280,720", "about:blank"
  ], { stdio: "ignore" });
  let spawnError = null;
  chrome.on("error", e => { spawnError = e; });

  let target = null;
  for (let i = 0; i < 40; i++) {
    await sleep(350);
    try {
      const list = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
      target = list.find(t => t.type === "page");
      if (target) break;
    } catch (_) {}
  }
  if (!target) {
    chrome.kill(); srv.close(); cleanupProfile(profile);
    skip(spawnError ? "浏览器启动失败（" + spawnError.message + "）"
                    : "CDP 未就绪（浏览器无法启动，或调试端口被拦）");
  }

  let c;
  try {
    c = await CDP.connect(target.webSocketDebuggerUrl);
  } catch (e) {
    chrome.kill(); srv.close(); cleanupProfile(profile);
    skip("CDP 连接失败（" + (e && e.message ? e.message : e) + "）");
  }
  await c.send("Page.enable"); await c.send("Runtime.enable");
  await c.send("Network.enable"); await c.send("Log.enable");
  let failed = 0;
  const ok = (cond, label, detail) => {
    if (!cond) failed++;
    console.log(`  ${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "  -> " + JSON.stringify(detail).slice(0, 180)}`);
  };

  async function load(url, w, h) {
    c.clear();
    await c.send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    await c.send("Page.navigate", { url });
    for (let i = 0; i < 30; i++) {
      await sleep(150);
      try {
        const st = await c.eval("({r:document.readyState,t:(document.body?document.body.innerText.length:0)})");
        if (st.r === "complete" && st.t > 100) break;
      } catch (_) {}
    }
    await sleep(2000);
  }
  async function shot(name) {
    if (!WANT_SHOTS) return;
    const s = await c.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(SHOTS, name), Buffer.from(s.data, "base64"));
  }
  async function clickSel(sel) {
    const box = await c.eval(`(() => { const e=document.querySelector(${JSON.stringify(sel)}); if(!e) return null; e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; })()`);
    if (!box) return false;
    await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 });
    await c.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 });
    await sleep(200);
    return true;
  }

  console.log("== C13：三页 × 1280/1920 六格（真实渲染，无横向溢出 / 无黄条 / 无 reference / 零错误）==");
  for (const [w, h] of SIZES) for (const page of PAGES) {
    await load(`${BASE}/${page}`, w, h);
    const L = await c.eval(LAYOUT_JS);
    const errs = c.errors();
    await shot(`${page.replace(".html", "")}_${w}.png`);
    ok(L.vw === w && L.vh === h && !L.overflowX && !L.overflowY && L.bad.length === 0 &&
       !L.hasFixtureNotice && !L.hasReference && L.textLen > 100 && errs.length === 0,
      `${page} @ ${w}x${h}  scroll=${L.sw}x${L.sh}/${L.vw}x${L.vh} 越界=${L.bad.length} 文本=${L.textLen} 错误=${errs.length}`,
      { L, errs });
  }

  console.log("== C2：真实鼠标点击三处 ==");
  await load(`${BASE}/tutor.html`, 1280, 720);
  c.clear();
  const before = await c.eval("document.querySelector('#turn-count').textContent");
  const c1 = await clickSel("#kcs .kc[data-kc]");
  const after = await c.eval("document.querySelector('#turn-count').textContent");
  ok(c1 && after !== before && after.indexOf("过滤") >= 0, "点 KC → 对话流按知识点过滤", { before, after });
  await clickSel("#kcs .kc[data-kc]");
  const c2 = await clickSel("#turns .turn.tutor .bubble[data-n]");
  const why = await c.eval("document.querySelector('#why-body').innerText");
  ok(c2 && /掌握度/.test(why) && /难度/.test(why) && /0\.\d\d/.test(why), "点题面 → 理由面板出三个数字", why.slice(0, 140));
  await shot("tutor_why_1280.png");
  const c3 = await clickSel("#memory-timeline .mem");
  const mem = await c.eval("(() => { const e=document.querySelector('#memory-timeline .mem'); return e?{open:e.classList.contains('open'),src:e.querySelector('.src')?e.querySelector('.src').innerText:''}:null; })()");
  ok(c3 && mem && mem.open && /evt_/.test(mem.src), "点记忆条目 → 展开并显示 src 事件 id", mem);
  const clickErrs = c.errors();
  ok(clickErrs.length === 0, "三处点击期间无 JS 异常 / 404 / console.error", clickErrs);

  console.log("== C9 / C6：整条路径与互跳 ==");
  for (const page of PAGES) {
    await load(`${BASE}/${page}`, 1280, 720);
    const L = await c.eval(LAYOUT_JS);
    const errs = c.errors();
    ok(L.textLen > 100 && !L.hasFixtureNotice && !L.hasReference && errs.length === 0,
      `${page} 无白屏 / 无 404 / 无 reference / 无黄条`, { L, errs });
  }
  await load(`${BASE}/cover.html`, 1280, 720);
  c.clear();
  const jumped = await clickSel('a[href="tutor.html"]');
  await sleep(900);
  const loc = await c.eval("location.pathname");
  ok(jumped && loc.indexOf("tutor.html") >= 0, "封面点「诊断页」跳到 tutor.html", loc);

  if (WANT_SHOTS) console.log(`截图已写入 ${SHOTS}`);
  console.log(failed ? `RESULT: FAIL (${failed})` : "RESULT: PASS（真实 Chrome CDP：六格 + 三处点击 + 整条路径 + 互跳）");
  try { c.ws.close(); } catch (_) {}
  chrome.kill(); srv.close(); cleanupProfile(profile);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error("check_browser.js 异常:", e.stack); process.exit(2); });
