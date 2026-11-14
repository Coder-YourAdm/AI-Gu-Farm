/* 正式实验页渲染自检：轻量 DOM 桩执行 web/formal.html 的脚本。
   沙箱里起不了浏览器（命名管道被拦），用它验证「四要点 + 数字 + 三硬标准」的渲染路径。
   由 tools/check_formal_page.py 调用，也可单独跑：node tools/check_formal_render.js
   注意：桩不能替代真人浏览器验收，只保证脚本层不塌。 */
"use strict";
const fs = require("fs"), vm = require("vm"), path = require("path");
const ROOT = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "web", "formal.html"), "utf8");

const briefM = html.match(/<script id="brief" type="application\/json">([\s\S]*?)<\/script>/);
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!briefM || scripts.length !== 1) throw new Error("页面 script 结构变了");
const BRIEF = JSON.parse(briefM[1]);

let els = {}, allEls = [];
function stub(id) {
  const e = {
    id: id || "", textContent: "", value: "", className: "", style: {}, children: [], _h: {}, _q: {},
    title: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(t, fn) { (this._h[t] || (this._h[t] = [])).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k] === undefined ? null : this[k]; },
    querySelector(sel) { return this._q[sel] || (this._q[sel] = stub(this.id + ":" + sel)); }, querySelectorAll() { return []; },
    closest() { return null; },
  };
  let h = "";
  Object.defineProperty(e, "innerHTML", {
    get() { return h; },
    set(v) { h = String(v); if (v === "") e.children.length = 0; }
  });
  allEls.push(e);
  return e;
}
global.document = {
  getElementById(id) {
    if (id === "brief") return { textContent: briefM[1] };
    return els[id] || (els[id] = stub(id));
  },
  createElement() { return stub(); },
  querySelectorAll() { return []; },
  addEventListener() {},
  body: stub("body")
};
global.window = global;
global.requestAnimationFrame = fn => setTimeout(fn, 0);
global.setTimeout = setTimeout;
global.clearTimeout = clearTimeout;

let failed = 0;
function ok(cond, label, detail) {
  console.log((cond ? "  PASS  " : "  FAIL  ") + label + (cond ? "" : "  -> " + (detail || "")));
  if (!cond) failed++;
}
function T(id) {
  const e = els[id];
  if (!e) return "";
  let s = e.textContent + " " + e.innerHTML;
  Object.values(e._q || {}).forEach(q => { s += " " + q.textContent + " " + q.innerHTML; });
  return s;
}
function all() { return Object.values(els).map(e => e.textContent + " " + e.innerHTML).join("\n"); }

try {
  vm.runInThisContext(scripts[0], { filename: "formal.html" });
} catch (e) {
  console.log("  FAIL  页面脚本抛异常 -> " + e.stack);
  failed++;
}

const badges = T("badges");
const chal = T("chal");
const runs = T("runs");
const limits = T("limits");
const ev = T("evidence");
const finalTbl = T("tbl-final");
const verdict = T("verdict");
const sc = els["selfcheck"] ? els["selfcheck"].textContent : "";

console.log("== 四要点 ==");
ok(/真模型/.test(badges) && !/mock/i.test(badges), "① 真模型绿标、无 mock 徽章", badges.slice(0, 160));
ok(/未达 3\/6 门槛/.test(chal) && /不画成达标/.test(chal) && /观察窗口已关闭/.test(chal), "② 挑战图标「未达标」并翻译 state", chal.slice(0, 220));
ok(/X04/.test(chal) && /X05/.test(chal), "② 挑战图 X04/X05 都在");
ok(/最高 0\/6|最高 2\/6/.test(chal), "② 挑战图显示最高观察值", chal.slice(0, 200));
ok(/未启动/.test(runs) && /取消/.test(runs) && /不计 0 分/.test(runs), "③ 取消/未启动明确展示且不计 0 分", runs.slice(0, 200));
ok((limits.match(/<li/g) || []).length === BRIEF.limits.length, "④ limits 五条上页面", "li=" + (limits.match(/<li/g) || []).length);
ok(BRIEF.limits.every(t => limits.indexOf(t.slice(0, 12)) >= 0), "④ limits 文案与数据一致");

console.log("== 数字与数据 ==");
BRIEF.pairs.forEach((p, i) => {
  const f = p.final;
  ok(finalTbl.indexOf(f.E.toFixed(2)) >= 0, `pairs[${i}] E=${f.E.toFixed(2)}`);
  ok(finalTbl.indexOf(f.F.toFixed(2)) >= 0, `pairs[${i}] F=${f.F.toFixed(2)}`);
});
ok(verdict.indexOf(BRIEF.main_result.E_minus_F_mean.toFixed(2)) >= 0, "结论条 +0.33 来自 main_result");
ok(/口径自检 \d+\/\d+ · 全部通过/.test(sc), "页面口径自检全通过", sc);
ok(/data-src=/.test(finalTbl + chal), "数字可点、带字段路径");
ok(/逐条复算/.test(ev) && /证据等级/.test(ev) && /tag/.test(ev), "证据等级如实标注（含 tag 已推 / runs 未入库）", ev.slice(0, 120));

console.log("== 三硬标准（页面脚本已跑通）==");
ok(!/reference/i.test(html), "页面无 reference 字样");
ok(!/(?:src|href)="https?:\/\//.test(html), "零外链");
ok(briefM[1].length > 1000, "数据内联，断网可开");
ok(scripts.length === 1, "单文件内联脚本");

console.log(failed ? "RESULT: FAIL (" + failed + ")" : "RESULT: PASS（渲染路径 + 四要点 + 三硬标准全通过）");
process.exit(failed ? 1 : 0);
