/* C8 真实 HTTP 自检：起一个本地 /api/ask，把「关掉后端 → 自动回退回放」
 * 从 check_live.js 的 fetch 桩模拟升级为真实网络请求。
 *
 * 用途：B 的 /api/ask 还没落地（main 上 grep 不到），C8 验收动作
 *       「关掉后端再点，页面提示并自动回放」因此没有真后端可关。
 *       本脚本在本机 127.0.0.1 起一个符合契约 4.5 的 mock 服务，
 *       用真实 HTTP 走一遍：服务在 → 连答 5 题 → 关服务 → 自动切回放
 *       → 重启服务 → 后端 ok:false → 同样回退。
 *
 * 与 check_live.js 的分工：
 *   check_live.js      DOM 桩 + fetch 桩，验证状态机与响应形状（快）
 *   check_live_http.js DOM 桩 + 真实 HTTP，验证网络故障切换（本脚本）
 *   两者都替代不了真人在浏览器里点；完整验收仍需非沙箱浏览器。
 *
 * 运行：node docs/demo/check_live_http.js
 * 依赖：Node >= 18（用全局 fetch），零 npm 包，零外网。
 *
 * 重要：不修改 web/tutor.html。endpoint 只在运行时通过 __tutorDebug.state
 *       注入，源码里 LIVE.endpoint 保持 null —— B 的接口通了才改那一处。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const http = require("http");

const ROOT = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(ROOT, "web", "tutor.html"), "utf8");
const tutorJson = JSON.parse(fs.readFileSync(path.join(ROOT, "web", "tutor.json"), "utf8"));
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error("FAIL: tutor.html 里找不到 <script>"); process.exit(2); }

/* ── 契约 4.5 的 mock 后端 ─────────────────────────────────────
   六道题与页面 LIVE_POOL 同口径，答案全是有理数；判分由服务端算，
   页面只负责渲染。这样能验证「真接口形状」而不是页面自算的假 Tutor。 */
const POOL = [
  { kc: "integral_definite", statement: "计算定积分 ∫₀¹ (3x² - 2x + 1) dx。", answer: "1", difficulty: 0.35 },
  { kc: "integral_definite", statement: "计算定积分 ∫₀² (x + 1) dx。", answer: "4", difficulty: 0.45 },
  { kc: "integral_improper", statement: "计算反常积分 ∫₁^{+∞} 1/x² dx。", answer: "1", difficulty: 0.58 },
  { kc: "integral_improper", statement: "计算反常积分 ∫₀^{+∞} e^{-x} dx。", answer: "1", difficulty: 0.62 },
  { kc: "det", statement: "计算二阶行列式 det [[1, 2], [3, 4]]。", answer: "-2", difficulty: 0.40 }
];
const FMT = "integer, fraction or finite decimal";
const HINTS = ["先看被积函数的原函数", "别忘了代入上下限", "拆成两项分别积", "检查符号", "再核对一次常数项"];
let reqLog = [];
let forceError = false;

function makeServer() {
  const state = { i: 0, hint: 0, turn: 0, mastery: 0.20 };
  const srv = http.createServer((req, res) => {
    let body = "";
    req.on("data", c => { body += c; });
    req.on("end", () => {
      let q = {};
      try { q = JSON.parse(body || "{}"); } catch (_) { q = {}; }
      reqLog.push({ url: req.url, method: req.method, body: q });
      const send = (code, obj) => {
        res.writeHead(code, { "Content-Type": "application/json", "Cache-Control": "no-store" });
        res.end(JSON.stringify(obj));
      };
      if (req.url !== "/api/ask" || req.method !== "POST") return send(404, { ok: false, error: "not found" });
      if (forceError) return send(500, { ok: false, error: "Tutor: mock 后端故障" });

      const action = q.action || "start";
      if (action === "reset") { state.i = 0; state.hint = 0; state.turn = 0; state.mastery = 0.20; }
      const item = POOL[state.i % POOL.length];
      let verdict = null, err = "none";
      if (action === "submit") {
        const a = String(q.answer === null || q.answer === undefined ? "" : q.answer).trim();
        const norm = s => {
          // 有理数判分：支持 "1"、"1.0"、"2/2"，不比较浮点
          if (/^-?\d+\/-?\d+$/.test(s)) {
            const [n, d] = s.split("/").map(Number);
            return d === 0 ? NaN : n / d;
          }
          return /^-?\d+(\.\d+)?$/.test(s) ? Number(s) : NaN;
        };
        const av = norm(a), ev = norm(item.answer);
        const right = !Number.isNaN(av) && !Number.isNaN(ev) && Math.abs(av - ev) < 1e-9;
        verdict = right ? "right" : "wrong";
        err = right ? "none" : "calc";
        if (right) { state.mastery = Math.min(0.95, state.mastery + 0.12); state.i++; state.hint = 0; }
        else { state.hint = Math.min(4, state.hint + 1); }
      } else if (action === "skip") {
        verdict = "wrong"; err = "skipped"; state.hint = Math.min(4, state.hint + 1);
      } else if (action === "hint") {
        state.hint = Math.min(4, state.hint + 1);
      }
      state.turn++;
      const cur = POOL[state.i % POOL.length];
      send(200, {
        ok: true, turn: state.turn, kc: cur.kc,
        verdict: verdict, err: err,
        statement: cur.statement, answer_format: FMT,
        hint: { level: state.hint, text: HINTS[state.hint] },
        mastery: { integral_definite: Number(state.mastery.toFixed(2)), integral_improper: 0.64, det: 0.78 },
        why: { mastery: Number(state.mastery.toFixed(2)), difficulty: cur.difficulty,
               gap: Number((cur.difficulty - state.mastery).toFixed(2)),
               last_error: err === "calc" ? "计算出错" : "上次答对了", memory: ["evt_0031"] },
        review: [{ kc: cur.kc, due: "2026-11-17", reason: "下次复习" }],
        elapsed_ms: 3
      });
    });
  });
  return srv;
}

/* ── 最小 DOM 桩（与 check_live.js 同口径）───────────────────── */
function stubEl(id) {
  const e = {
    id: id || "", textContent: "", value: "", disabled: false, className: "",
    style: {}, children: [], _h: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(t, fn) { (this._h[t] || (this._h[t] = [])).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return stubEl(); }, querySelectorAll() { return []; }
  };
  let h = "";
  Object.defineProperty(e, "innerHTML", { get() { return h; }, set(v) { h = String(v); if (v === "") e.children.length = 0; } });
  return e;
}
const els = {};
global.document = {
  getElementById(id) { return els[id] || (els[id] = stubEl(id)); },
  createElement() { return stubEl(); }, querySelectorAll() { return []; },
  addEventListener() {}, body: stubEl("body")
};
global.window = global;
global.requestAnimationFrame = fn => setTimeout(fn, 0);
const nativeFetch = global.fetch;

let failed = 0;
function ok(cond, label, detail) {
  console.log((cond ? "  PASS  " : "  FAIL  ") + label + (cond ? "" : "  -> " + (detail || "")));
  if (!cond) failed++;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
function notice() { return els["notice"] ? els["notice"].innerHTML : ""; }

(async function () {
  let srv = makeServer();
  await new Promise(r => srv.listen(0, "127.0.0.1", r));
  const port = srv.address().port;
  const URL = `http://127.0.0.1:${port}/api/ask`;
  console.log(`== C8 真实 HTTP 自检 · mock /api/ask @ ${URL} ==`);

  /* fetch 分流：页面加载 tutor.json 走本地文件；/api/ask 走真实网络 */
  global.fetch = function (url, opt) {
    if (String(url).indexOf("/api/ask") >= 0) return nativeFetch(url, opt);
    if (String(url).indexOf("tutor.json") >= 0 || String(url).indexOf("compare.json") >= 0) {
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve(JSON.parse(JSON.stringify(tutorJson))) });
    }
    return nativeFetch(url, opt);
  };

  try { vm.runInThisContext(m[1], { filename: "tutor.html" }); }
  catch (e) { console.log("  FAIL  页面脚本抛异常 -> " + e.stack); failed++; }
  await sleep(80);
  const dbg = global.__tutorDebug;
  ok(!!dbg, "页面暴露 __tutorDebug 钩子");
  if (!dbg) { srv.close(); process.exit(1); }

  /* ① 服务在：注入 endpoint（只改运行时状态，不改源码） */
  dbg.state.endpoint = URL;
  dbg.setMode("live");
  await sleep(120);
  ok(dbg.state.on === true, "① 服务在：进入实时模式成功");
  const startReq = reqLog.find(r => r.body && r.body.action === "start");
  ok(!!startReq, "① setMode(live) 真实发出 POST /api/ask（action=start）");
  ok(!!startReq && startReq.body.sid === "live01", "① 请求体带 sid=live01", JSON.stringify(startReq && startReq.body));
  ok(els["turns"] && els["turns"].children.length > 0, "① 题面渲染出来", els["turns"] && els["turns"].children.length);

  /* ② 连答 5 题：真接口判分 + 提示级别变化 */
  const before = reqLog.length;
  await dbg.request("submit", "1");   // 第 1 题答对 → 换题
  await dbg.request("submit", "99");  // 错 → hint 升 1
  await dbg.request("submit", "99");  // 错 → hint 升 2
  await dbg.request("hint", null);    // 再要提示 → 升 3
  await dbg.request("submit", "4");   // 第 2 题答对 → 换题、提示归零
  const submits = reqLog.slice(before).filter(r => r.body && r.body.action === "submit");
  ok(submits.length === 4, "② 真实 HTTP 发出 4 次 submit", "实际 " + submits.length);
  ok(reqLog.length - before >= 5, "② 连答 5 题全部走真实网络", "请求数 " + (reqLog.length - before));
  const rightRes = await dbg.ask("submit", "1");
  ok(rightRes && rightRes.ok === true && rightRes.verdict === "right", "② 真接口判 right（有理数 1）", JSON.stringify(rightRes && rightRes.verdict));
  const wrongRes = await dbg.ask("submit", "99");
  ok(wrongRes && wrongRes.verdict === "wrong" && wrongRes.hint.level >= 1,
     "② 真接口判 wrong 且提示级别递增", JSON.stringify(wrongRes && wrongRes.hint));
  ok(dbg.state.on === true, "② 服务在期间没有误切回放");

  /* ③ 重置回首题 */
  await dbg.request("reset", null);
  const resetReq = reqLog.filter(r => r.body && r.body.action === "reset").pop();
  ok(!!resetReq, "③ reset 真实发出");
  ok(dbg.state.on === true, "③ 重置后仍在实时模式");

  /* ④ 关掉后端再点：真实连接被拒 → 提示 + 自动切回放 */
  await new Promise(r => srv.close(r));
  srv = null;
  await sleep(30);
  dbg.setMode("live");
  await sleep(60);
  await dbg.request("submit", "1");
  await sleep(60);
  ok(dbg.state.on === false, "④ 关掉后端：自动切回回放（LIVE.on=false）");
  ok(notice().indexOf("实时模式不可用") >= 0, "④ 页面提示「实时模式不可用」", notice().slice(0, 90));
  ok(/ECONNREFUSED|fetch failed|Unable to connect/i.test(notice()),
     "④ 提示里带真实网络错误（不是桩里写死的假错误）", notice().slice(0, 120));
  ok(els["turns"] && els["turns"].children.length > 0, "④ 回放数据仍在渲染，没有白屏");

  /* ⑤ 重启服务：后端 ok:false → 同样回退；并确认能恢复 */
  srv = makeServer();
  await new Promise(r => srv.listen(0, "127.0.0.1", r));
  const URL2 = `http://127.0.0.1:${srv.address().port}/api/ask`;
  dbg.state.endpoint = URL2;
  dbg.setMode("live");
  await sleep(100);
  ok(dbg.state.on === true, "⑤ 服务恢复后能重新进入实时模式");

  forceError = true;
  await dbg.request("submit", "1");
  await sleep(60);
  ok(dbg.state.on === false, "⑤ 后端 ok:false：自动切回回放");
  ok(notice().indexOf("mock 后端故障") >= 0, "⑤ 提示里带出后端错误原文", notice().slice(0, 100));
  forceError = false;

  /* ⑥ 源码未被改动 */
  const src = fs.readFileSync(path.join(ROOT, "web", "tutor.html"), "utf8");
  ok(/endpoint:\s*null/.test(src), "⑥ web/tutor.html 的 LIVE.endpoint 仍为 null（B 接口到位才改）");

  if (srv) await new Promise(r => srv.close(r));
  console.log(failed ? `RESULT: FAIL（${failed} 项）` : "RESULT: PASS（真实 HTTP 路径全通过）");
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error("FATAL", e); process.exit(2); });
