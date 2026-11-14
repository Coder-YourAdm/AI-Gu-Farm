/* C10 附带的对照页冒烟（Node，零依赖）。
 *
 * 和 check_live.js 同一套路：DOM 桩加载 web/compare.html 的真脚本，
 * 验证 9 行明细、3 组条形、口径自检 9/9、以及指标切换后条形真的重画。
 *
 * 运行：node docs/demo/check_compare.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HTML = path.join(__dirname, "..", "..", "web", "compare.html");
const html = fs.readFileSync(HTML, "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error("FAIL: compare.html 里找不到 <script>");
  process.exit(2);
}

function stubEl(id) {
  const el = {
    id: id || "",
    textContent: "",
    value: "",
    className: "",
    style: {},
    children: [],
    _handlers: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on === undefined ? !this._set.has(c) : on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); }
    },
    addEventListener(type, fn) { this._handlers[type] = fn; },
    appendChild(c) { this.children.push(c); },
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return stubEl(); },
    querySelectorAll() { return []; }
  };
  let htmlText = "";
  Object.defineProperty(el, "innerHTML", {
    get() { return htmlText; },
    set(v) { htmlText = v; if (v === "") el.children.length = 0; }
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
global.requestAnimationFrame = function (fn) { fn(); };
global.fetch = function () {
  return Promise.reject(new Error("offline: 没有 web/compare.json"));
};

try {
  vm.runInThisContext(m[1], { filename: "web/compare.html" });
} catch (e) {
  console.error("FAIL: 页面脚本执行报错 ->", (e && e.stack) || e);
  process.exit(2);
}

let failed = 0;
function ok(cond, label, detail) {
  if (cond) console.log("  PASS  " + label);
  else { failed += 1; console.log("  FAIL  " + label + (detail ? "  -> " + detail : "")); }
}
/* 页面用 appendChild 建 DOM，innerHTML 一直是空串；用子节点做签名 */
function sig(el) {
  return (el.children || []).map(function (c) {
    return c.className + "|" + c.innerHTML + "|" +
      (c.children || []).map(function (x) { return x.innerHTML; }).join("~");
  }).join(";");
}

(async function main() {
  await new Promise((r) => setTimeout(r, 30));

  console.log("== A. 3×3 矩阵与明细表 ==");
  ok((els["chart"] || { children: [] }).children.length === 3,
     "左图有 3 组（每档学生一组）", String((els["chart"] || {}).children?.length));
  ok((els["tbody"] || { children: [] }).children.length === 9,
     "明细表有 9 行", String((els["tbody"] || {}).children?.length));

  console.log("== B. 口径自检与来源 ==");
  const src = (els["src-text"] || {}).textContent || "";
  ok(src.indexOf("9/9") >= 0, "页脚口径自检 9/9", src);
  ok(els["src"].className.indexOf("ok") >= 0 &&
     els["src"].className.indexOf("warnx") < 0,
     "来源标记是 ok 而不是 warnx", els["src"].className);
  ok((els["seed"] || {}).textContent === "seed 42", "种子显示 42",
     (els["seed"] || {}).textContent);

  console.log("== C. 指标切换：条形重画、提示语跟着换 ==");
  const before = sig(els["chart"]);
  const btn = { getAttribute() { return "cost"; } };
  els["metric"]._handlers.click.call(els["metric"], { target: { closest() { return btn; } } });
  const after = sig(els["chart"]);
  ok(before !== after, "切到 cost 后条形重画（子节点签名变了）");
  ok((els["axis-note"] || {}).textContent.indexOf("cost") >= 0,
     "轴提示语换成 cost 口径", (els["axis-note"] || {}).textContent);

  console.log("");
  if (failed === 0) console.log("RESULT: PASS（0 项失败）");
  else console.log("RESULT: FAIL（" + failed + " 项失败）");
  process.exit(failed === 0 ? 0 : 1);
})();
