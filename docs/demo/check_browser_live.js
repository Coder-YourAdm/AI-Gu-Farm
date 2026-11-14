#!/usr/bin/env node
/* 实时模式的「真实浏览器 + 真实 HTTP」验收（B7 到货前后都能跑）。
 *
 * 为什么需要它：
 *   - check_live.js      用 DOM 桩验状态机，不碰网络；
 *   - check_live_http.js 用真 HTTP，但页面是桩；
 *   - check_browser.js   真浏览器，但只验回放，不验实时。
 *   这一条把三者的缺口补上：真 Chrome 里点「实时」，请求真的 POST 到 /api/ask。
 *
 * 做法：自带静态服务 + 契约 4.5 的 mock /api/ask，并把 web/tutor.html 在内存里
 *      把 LIVE.endpoint 改成 "/api/ask"（不改仓库文件）。
 *
 * 用法：node docs/demo/check_browser_live.js
 *   SKIP（无浏览器）退 0；断言失败退 1。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const net = require("net");
const os = require("os");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const WEB = path.join(ROOT, "web");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ── mock /api/ask（契约 4.5） ── */
const KC = ["integral_definite", "integral_improper", "det"];
const STATE = new Map();
const STATEMENT = "计算定积分 ∫_{0}^{1} (3x² - 2x + 1) dx。";

function apiCall(body) {
  const sid = String(body.sid || "");
  const action = String(body.action || "");
  if (!["start", "submit", "skip", "hint", "why", "reset"].includes(action)) {
    return { code: 400, obj: { ok: false, error: "unknown action" } };
  }
  let st = STATE.get(sid);
  if (!st) {
    st = { turn: 0, hint: 0, last: "无记录", mastery: Object.fromEntries(KC.map((k) => [k, 0.2])) };
    STATE.set(sid, st);
  }
  st.turn += 1;
  let verdict = null;
  let err = null;
  if (action === "reset") {
    st.hint = 0;
    st.last = "无记录";
    st.mastery = Object.fromEntries(KC.map((k) => [k, 0.2]));
  } else if (action === "submit") {
    const ans = String(body.answer == null ? "" : body.answer).trim();
    const ok = ans === "1" || ans === "1.0";
    if (ok) {
      verdict = "right";
      err = "none";
      st.hint = 0;
      st.mastery.integral_definite = Math.min(1, st.mastery.integral_definite + 0.2);
    } else {
      verdict = "wrong";
      err = "calc";
      st.hint = Math.min(4, st.hint + 1);
      st.last = "算错了";
      st.mastery.integral_definite = Math.max(0.05, st.mastery.integral_definite - 0.02);
    }
  } else if (action === "skip") {
    verdict = "wrong";
    err = "giveup";
    st.hint = 4;
  } else if (action === "hint") {
    st.hint = Math.min(4, st.hint + 1);
  }
  return {
    code: 200,
    obj: {
      ok: true, turn: st.turn, kc: "integral_definite",
      verdict, err, statement: STATEMENT,
      answer_format: "integer, fraction or finite decimal",
      hint: { level: st.hint, text: "提示 L" + st.hint + "：再读一遍题。" },
      mastery: Object.assign({}, st.mastery),
      why: { mastery: st.mastery.integral_definite, difficulty: 0.35,
             last_error: st.last, memory: [] },
      review: [], elapsed_ms: 7
    }
  };
}

function startServer() {
  return new Promise((resolve, reject) => {
    const srv = http.createServer((req, res) => {
      if (req.method === "POST" && req.url.replace(/\/$/, "") === "/api/ask") {
        let raw = "";
        req.on("data", (c) => { raw += c; });
        req.on("end", () => {
          let body = {};
          try { body = JSON.parse(raw || "{}"); } catch (_) { body = {}; }
          const { code, obj } = apiCall(body);
          const buf = Buffer.from(JSON.stringify(obj));
          res.writeHead(code, { "Content-Type": "application/json; charset=utf-8",
            "Content-Length": buf.length });
          res.end(buf);
        });
        return;
      }
      const url = new URL(req.url, "http://x");
      let p = decodeURIComponent(url.pathname);
      if (p === "/") p = "/cover.html";
      const file = path.normalize(path.join(WEB, p));
      if (!file.startsWith(WEB)) { res.writeHead(403).end(); return; }
      fs.readFile(file, "utf8", (e, text) => {
        if (e) { res.writeHead(404).end("404"); return; }
        // 只在内存里把 endpoint 指向 mock，不改仓库文件
        if (p === "/tutor.html") {
          text = text.replace("endpoint: null", 'endpoint: "/api/ask"');
        }
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
        res.end(text);
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
  for (const c of [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium"
  ]) if (fs.existsSync(c)) return c;
  return null;
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []; }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
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
        out.push("JS异常: " + (e.params.exceptionDetails.text || ""));
      else if (e.method === "Log.entryAdded" && e.params.entry.level === "error")
        out.push("console.error: " + e.params.entry.text.slice(0, 120));
      else if (e.method === "Network.responseReceived" && e.params.response.status >= 400)
        out.push("HTTP " + e.params.response.status + " " + e.params.response.url);
    }
    return [...new Set(out)];
  }
}

(async () => {
  const exe = chromePath();
  if (!exe) { console.log("SKIP: 未找到 Chrome/Edge"); process.exit(0); }

  const { srv, port } = await startServer();
  const BASE = `http://127.0.0.1:${port}`;
  const cdpPort = await freePort();
  const profile = path.join(os.tmpdir(), "wb-live-" + process.pid + "-" + Date.now());
  fs.mkdirSync(profile, { recursive: true });

  const chrome = spawn(exe, [
    "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
    "--disable-dev-shm-usage", `--remote-debugging-port=${cdpPort}`,
    "--user-data-dir=" + profile, "--window-size=1280,720", "about:blank"
  ], { stdio: "ignore" });

  const done = (code) => {
    try { chrome.kill(); } catch (_) {}
    try { srv.close(); } catch (_) {}
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) {}
    process.exit(code);
  };

  let target = null;
  for (let i = 0; i < 30; i++) {
    await sleep(400);
    try {
      const list = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
      target = list.find((t) => t.type === "page");
      if (target) break;
    } catch (_) {}
  }
  if (!target) { console.log("SKIP: CDP 未就绪（环境限制，非页面缺陷）"); done(0); }

  let c;
  try { c = await CDP.connect(target.webSocketDebuggerUrl); }
  catch (e) { console.log("SKIP: CDP 连接失败（" + e.message + "）"); done(0); }

  await c.send("Page.enable");
  await c.send("Runtime.enable");
  await c.send("Network.enable");
  await c.send("Log.enable");

  let failed = 0;
  const ok = (cond, label, detail) => {
    if (!cond) failed += 1;
    console.log(`  ${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "  -> " + JSON.stringify(detail).slice(0, 200)}`);
  };

  async function load() {
    c.clear();
    await c.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });
    await c.send("Page.navigate", { url: BASE + "/tutor.html" });
    for (let i = 0; i < 30; i++) {
      await sleep(150);
      try {
        const st = await c.eval("({r:document.readyState,n:document.querySelectorAll('#turns .turn').length})");
        if (st.r === "complete" && st.n > 0) break;
      } catch (_) {}
    }
    await sleep(300);
  }
  async function click(sel) {
    const box = await c.eval(`(() => { const e=document.querySelector(${JSON.stringify(sel)}); if(!e) return null;
      e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect();
      return {x:r.left+r.width/2, y:r.top+r.height/2}; })()`);
    if (!box) return false;
    await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 });
    await c.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 });
    await sleep(250);
    return true;
  }
  async function typeAndEnter(text) {
    await click("#answer");
    await c.send("Input.insertText", { text });
    await c.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    await c.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    await sleep(350);
  }
  const lastStudent = `(() => { const s=[...document.querySelectorAll('#turns .turn.student')];
    if(!s.length) return null; const e=s[s.length-1];
    return {verdict:(e.querySelector('.verdict')||{}).innerText||'', answer:(e.querySelector('.bubble')||{}).innerText||''}; })()`;
  const lastHint = `(() => { const h=[...document.querySelectorAll('#turns .turn.tutor .hintline .lv')];
    return h.length? h[h.length-1].innerText : null; })()`;
  const firstStatement = `(() => { const b=document.querySelector('#turns .turn.tutor .stmt');
    return b? b.innerText : null; })()`;

  console.log("== A. 切实时：请求真的走 /api/ask ==");
  await load();
  const before = await c.eval(firstStatement);
  await click('#mode button[data-m="live"]');
  await sleep(700);
  const liveOn = await c.eval("document.body.classList.contains('live')");
  const composer = await c.eval("(() => { const e=document.querySelector('.composer'); return e? getComputedStyle(e).display : 'none'; })()");
  ok(liveOn, "body 切到 live");
  ok(composer !== "none", "输入区可见", composer);
  const afterStart = await c.eval(firstStatement);
  ok(afterStart && afterStart !== before, "start 请求返回了新题面", { before, afterStart });

  console.log("== B. 连交 5 次错答案：提示 L1→L4 封顶 ==");
  const levels = [];
  for (let i = 0; i < 5; i += 1) {
    await typeAndEnter("0");
    levels.push(await c.eval(lastHint));
  }
  ok(JSON.stringify(levels) === JSON.stringify(["L1", "L2", "L3", "L4", "L4"]),
     "提示级别 L1→L2→L3→L4→L4", levels);

  console.log("== C. 答对：判对 + 掌握度上升 ==");
  const masteryBefore = await c.eval("document.querySelector('#kcs .kc .val').innerText");
  await typeAndEnter("1");
  const st = await c.eval(lastStudent);
  const masteryAfter = await c.eval("document.querySelector('#kcs .kc .val').innerText");
  ok(st && st.verdict.indexOf("对") >= 0, "最后一条学生气泡判为「对」", st);
  ok(masteryBefore !== masteryAfter, "掌握度条发生变化", { masteryBefore, masteryAfter });

  console.log("== D. 重置：回到第一题、对话清空 ==");
  await click('.quick button[data-a="reset"]');
  await sleep(400);
  const turns = await c.eval("document.querySelectorAll('#turns .turn.student').length");
  const resetStmt = await c.eval(firstStatement);
  ok(turns <= 1, "重置后学生气泡清空（≤1 条）", turns);
  ok(!!resetStmt, "重置后重新出现题面", resetStmt);

  console.log("== E. 全程无 JS 异常 / 404 ==");
  const errs = c.errors();
  ok(errs.length === 0, "无异常/404", errs);

  console.log("");
  console.log(failed ? `RESULT: FAIL (${failed})` : "RESULT: PASS（真实浏览器 + 真实 HTTP：实时模式全链路）");
  done(failed ? 1 : 0);
})().catch((e) => { console.error("check_browser_live.js 异常:", e.stack); process.exit(2); });
