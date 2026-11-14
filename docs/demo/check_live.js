/* C7 实时层自检（Node，零依赖）。
 *
 * 用途：沙箱里起不了浏览器（命名管道被拦），用轻量 DOM 桩加载
 *       web/tutor.html 的真脚本，再通过页面里的 __tutorDebug 钩子驱动
 *       同一套状态机，验证契约 4.5 的返回形状与 C7 的验收动作。
 *
 * 运行：node docs/demo/check_live.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "..", "web", "tutor.html");
const html = fs.readFileSync(HTML, "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error("FAIL: tutor.html 里找不到 <script>");
  process.exit(2);
}

/* ── 最小 DOM 桩：只满足脚本加载与渲染路径，不做真实布局 ── */
function stubEl(id) {
  const el = {
    id: id || "",
    textContent: "",
    value: "",
    disabled: false,
    className: "",
    style: {},
    children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {},
    appendChild(c) { this.children.push(c); },
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return stubEl(); },
    querySelectorAll() { return []; }
  };
  /* innerHTML 被清空时同步清掉 children —— 页面里是先 innerHTML="" 再 appendChild */
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get() { return html; },
    set(v) { html = v; if (v === "") el.children.length = 0; }
  });
  return el;
}
const els = {};
global.document = {
  getElementById(id) { return els[id] || (els[id] = stubEl(id)); },
  createElement() { return stubEl(); },
  querySelectorAll() { return []; },
  body: stubEl("body")
};
global.window = global;
global.fetch = function () {
  return Promise.reject(new Error("offline: 没有 web/tutor.json"));
};

try {
  vm.runInThisContext(m[1], { filename: "web/tutor.html" });
} catch (e) {
  console.error("FAIL: 页面脚本执行报错 ->", e && e.stack || e);
  process.exit(2);
}

const dbg = global.__tutorDebug;
if (!dbg) {
  console.error("FAIL: 没有 __tutorDebug 钩子");
  process.exit(2);
}

let failed = 0;
function ok(cond, label, detail) {
  if (cond) {
    console.log("  PASS  " + label);
  } else {
    failed += 1;
    console.log("  FAIL  " + label + (detail ? "  -> " + detail : ""));
  }
}
function near(a, b, eps) {
  return Math.abs(a - b) <= (eps === undefined ? 1e-3 : eps);
}

(async function main() {
  await new Promise((r) => setTimeout(r, 30));  // 让 load() 的兜底渲染跑完

  const H = dbg.handle;
  const pool = dbg.pool;
  // 这个脚本只验「页面内置状态机」；真接口路径由 check_browser_live.js --base 验。
  // 页面可能已被 switch_live.py 指向 /api/ask，这里强制关掉，保证测试自洽。
  dbg.state.endpoint = null;

  console.log("== A. 返回形状（契约 4.5 的字段一个不少） ==");
  const start = H("start", null);
  const keys = ["ok", "turn", "kc", "verdict", "err", "statement", "answer_format",
                "hint", "mastery", "why", "review", "elapsed_ms"];
  ok(keys.every((k) => k in start), "十二个字段齐全", keys.filter((k) => !(k in start)).join(","));
  ok(Array.isArray(start.review) && start.review.length > 0, "review 是非空数组");
  ok(["mastery", "difficulty", "last_error", "memory"].every((k) => k in start.why),
     "why 含三个数字与 memory");
  ok(typeof start.hint.level === "number" && typeof start.hint.text === "string",
     "hint 含 level 与 text");

  console.log("== A2. 大整数精度（BigInt，B 体检台账 ⑥） ==");
  const f1 = dbg.frac("9007199254740993");   // 2^53 + 1
  const f2 = dbg.frac("9007199254740992");   // 2^53
  ok(!!f1 && !!f2 && f1.n !== f2.n, "2^53+1 与 2^53 不再相等",
     { a: f1 && String(f1.n), b: f2 && String(f2.n) });
  const bigFrac = dbg.frac("123456789012345678901/7");
  ok(!!bigFrac && String(bigFrac.n) === "123456789012345678901",
     "超大分数分子不被截断", bigFrac && String(bigFrac.n));
  const h1 = dbg.frac("0.5"), h2 = dbg.frac("1/2");
  ok(!!h1 && !!h2 && h1.n === h2.n && h1.d === h2.d, "0.5 与 1/2 仍解析为同一个数");

  console.log("== B. 重置 → 回到首题、掌握度归 0.20 ==");
  const r0 = H("reset", null);
  ok(r0.statement === pool[0].statement, "reset 后是第 1 题");
  ok(near(r0.mastery.integral_definite, 0.20) &&
     near(r0.mastery.integral_improper, 0.20) &&
     near(r0.mastery.det, 0.20), "三条掌握度都是 0.20",
     JSON.stringify(r0.mastery));
  ok(r0.hint.level === 0, "reset 后提示回到 L0");

  console.log("== C. 连答 5 题：判分变了、提示单调升到 4 并封顶 ==");
  H("reset", null);
  const levels = [];
  const errs = [];
  let last = null;
  for (let i = 0; i < 5; i += 1) {
    last = H("submit", "0");          // 正确答案是 1，交 0 必错
    levels.push(last.hint.level);
    errs.push(last.err);
    if (i === 0) {
      ok(near(last.mastery.integral_definite, 0.1774),
         "0.20 答错一次 = 0.1774", String(last.mastery.integral_definite));
    }
  }
  ok(JSON.stringify(levels) === JSON.stringify([1, 2, 3, 4, 4]),
     "提示级别 1→2→3→4→4", JSON.stringify(levels));
  ok(errs.every((e) => e === "calc"), "五次错因都判成 calc", JSON.stringify(errs));
  ok(last.verdict === "wrong", "判分是 wrong");
  ok(last.mastery.integral_definite < 0.1774, "连错后掌握度继续下降",
     String(last.mastery.integral_definite));
  ok(last.hint.text.indexOf(pool[0].answer) >= 0, "L4 提示里给出答案");

  console.log("== D. 答对：掌握度按 BKT 上升、换题、提示归零 ==");
  H("reset", null);
  const right = H("submit", "1");
  ok(right.verdict === "right" && right.err === "none", "判分是 right / none");
  ok(near(right.mastery.integral_definite, 0.5526), "0.20 答对一次 = 0.5526",
     String(right.mastery.integral_definite));
  ok(right.statement === pool[1].statement, "答对后换到第 2 题");
  ok(right.hint.level === 0, "答对后提示归零");

  console.log("== E. 有理数判分：1.0 与 2/2 都该算对 ==");
  H("reset", null);
  ok(H("submit", "1.0").verdict === "right", "1.0 判对");
  H("reset", null);
  ok(H("submit", "2/2").verdict === "right", "2/2 判对");
  H("reset", null);
  ok(H("submit", "1/2").verdict === "wrong", "1/2 判错（不是 1）");

  console.log("== F. 我不会 / 为什么给我 / 提示封顶 ==");
  H("reset", null);
  const skip = H("skip", null);
  ok(skip.verdict === "wrong" && skip.err === "giveup", "skip 判 giveup");
  ok(skip.hint.level === 4 && skip.hint.text.indexOf(pool[0].answer) >= 0,
     "skip 直接给到 L4 答案");
  const turnBefore = dbg.state.turn;
  H("why", null);
  ok(dbg.state.turn === turnBefore, "why 不推进轮次");
  for (let i = 0; i < 6; i += 1) H("hint", null);
  ok(H("why", null).hint.level === 4, "提示连点也封顶在 4");

  console.log("== G. 六题轮换：连对 6 次回到第 1 题 ==");
  H("reset", null);
  for (let i = 0; i < pool.length; i += 1) {
    const ans = pool[(dbg.state.i) % pool.length].answer;
    const res = H("submit", ans);
    if (res.verdict !== "right") { ok(false, "第 " + (i + 1) + " 题答对", JSON.stringify(res)); break; }
    if (i === pool.length - 1) ok(res.statement === pool[0].statement, "六题之后回到第 1 题");
  }

  console.log("== H. 切到实时模式：自动 start，对话流渲染出题面 ==");
  dbg.setMode("live");
  await new Promise((r) => setTimeout(r, 30));
  ok(dbg.state.on === true, "LIVE.on 切到 true");
  ok(dbg.state.log.length === 1, "自动 start 后有 1 条对话",
     String(dbg.state.log.length));
  const turnHost = els["turns"];
  const first = turnHost && turnHost.children[0];
  ok(!!first && first.innerHTML.indexOf("stmt") >= 0, "对话流里渲染出题面段落",
     first ? first.innerHTML.slice(0, 60) : "no child");
  ok((els["kcs"] || { children: [] }).children.length === 3, "左侧仍是 3 条掌握度");
  dbg.setMode("replay");
  ok(dbg.state.on === false, "切回放后 LIVE.on 回 false");

  console.log("== I. 失败路径：断服务 / 后端 ok:false → 提示 + 自动回放 ==");
  dbg.state.endpoint = "/api/ask";

  dbg.setMode("live");
  ok(dbg.state.on === true, "先进入实时模式");
  global.fetch = function () { return Promise.reject(new Error("ECONNREFUSED")); };
  await dbg.request("start", null);
  ok(dbg.state.on === false, "fetch 断开：自动切回回放");
  ok(els["notice"].innerHTML.indexOf("实时模式不可用") >= 0,
     "提示里有「实时模式不可用」", els["notice"].innerHTML.slice(0, 60));

  dbg.setMode("live");
  global.fetch = function () {
    return Promise.resolve({ ok: true, json: function () {
      return Promise.resolve({ ok: false, error: "Tutor: boom" });
    } });
  };
  await dbg.request("submit", "1");
  ok(dbg.state.on === false, "后端 ok:false：同样自动回放");
  ok(els["notice"].innerHTML.indexOf("boom") >= 0,
     "提示里带出后端错误原文", els["notice"].innerHTML.slice(0, 60));
  dbg.state.endpoint = null;

  console.log("");
  if (failed === 0) {
    console.log("RESULT: PASS（" + 0 + " 项失败）");
  } else {
    console.log("RESULT: FAIL（" + failed + " 项失败）");
  }
  process.exit(failed === 0 ? 0 : 1);
})();
