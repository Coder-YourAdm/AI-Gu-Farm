/* C13 布局自检 —— 贴进浏览器 DevTools Console 回车即可。
 *
 * 用法：
 *   1) 打开 cover.html / tutor.html / compare.html
 *   2) F11 全屏，缩放 100%（Ctrl+0）
 *   3) 窗口宽度调到 1280（或 1920），粘这段代码回车
 *   4) 看输出：第一行 PASS 或 FAIL；FAIL 会列出具体元素
 *
 * 它检查的是「投影上会出事」的几类问题：
 *   页面横向溢出 / 整页纵向溢出（单屏页不该整页滚）/ 元素越界 /
 *   字号过小 / 契约 id 缺失 / 截图会带黄条 / 出现 reference 字样。
 */
(function () {
  "use strict";

  var W = window.innerWidth, H = window.innerHeight;
  var doc = document.scrollingElement || document.documentElement;
  var page = (location.pathname.split("/").pop() || "index.html");
  var issues = [];
  var info = [];

  function nameOf(el) {
    var s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    if (typeof el.className === "string" && el.className.trim()) {
      s += "." + el.className.trim().split(/\s+/).join(".");
    }
    return s;
  }

  /* 1. 页面级横向溢出 */
  if (doc.scrollWidth > W + 1) {
    issues.push("页面横向溢出 " + doc.scrollWidth + "px > 视口 " + W + "px");
  }

  /* 2. 整页纵向溢出：三页的 main 都是 flex:1，面板内部滚动，整页不该滚 */
  if (doc.scrollHeight > H + 1) {
    issues.push("整页纵向溢出 " + doc.scrollHeight + "px > 视口 " + H + "px" +
                "（单屏页不该出现；先 Ctrl+F5 清缓存再看）");
  }

  /* 3. 元素越界：右/左边缘超出视口 */
  var body = document.body;
  var all = body ? body.querySelectorAll("*") : [];
  var out = [];
  Array.prototype.forEach.call(all, function (el) {
    var r = el.getBoundingClientRect();
    if (r.width <= 0 && r.height <= 0) return;
    if (r.right > W + 1 || r.left < -1) {
      out.push(nameOf(el) + " [" + Math.round(r.left) + "," + Math.round(r.right) + "]");
    }
  });
  if (out.length) {
    issues.push("越界元素 " + out.length + " 个：" + out.slice(0, 10).join(" | "));
  }

  /* 4. 投影可读性：叶子节点字号 < 10px */
  var tiny = [];
  Array.prototype.forEach.call(all, function (el) {
    if (el.children.length) return;
    var t = (el.textContent || "").trim();
    if (!t) return;
    var fs = parseFloat(window.getComputedStyle(el).fontSize);
    if (fs && fs < 10) tiny.push(nameOf(el) + "@" + fs.toFixed(1) + "px");
  });
  if (tiny.length) issues.push("字号过小 " + tiny.length + " 处：" + tiny.slice(0, 8).join(" | "));

  /* 5. 契约 id（只对 tutor.html 强制） */
  if (page === "tutor.html") {
    var need = ["mode", "kcs", "turns", "why-panel", "memory-timeline",
                "review-list", "version", "answer", "send"];
    var miss = need.filter(function (id) { return !document.getElementById(id); });
    if (miss.length) issues.push("缺契约 id：" + miss.join(","));
  }

  /* 6. 截图禁忌：黄条 / reference 字样 */
  var notice = document.getElementById("notice");
  if (notice && notice.classList.contains("on")) {
    issues.push("提示条可见（截图会带黄条）：" +
                (notice.textContent || "").trim().slice(0, 40));
  }
  if (body && (body.innerText || "").indexOf("reference") >= 0) {
    issues.push("页面出现 reference 字样");
  }

  /* 7. 页面各自的硬数字 */
  if (page === "tutor.html") {
    var kcs = document.querySelectorAll("#kcs .kc");
    info.push("掌握度条 " + kcs.length + " 条");
    var turns = document.getElementById("turns");
    if (turns && turns.getBoundingClientRect().width < 360) {
      issues.push("对话流太窄：" + Math.round(turns.getBoundingClientRect().width) + "px");
    }
  }
  if (page === "compare.html") {
    info.push("明细行 " + document.querySelectorAll("#tbody tr").length + " 行");
    info.push("条形组 " + document.querySelectorAll("#chart .sgrp").length + " 组");
    if (document.querySelectorAll("#tbody tr").length !== 9) issues.push("明细不是 9 行");
  }
  if (page === "cover.html") {
    info.push("数字卡片 " + document.querySelectorAll(".nums .num").length + " 张");
  }

  /* ── 输出 ── */
  var pass = issues.length === 0;
  console.log("%c[C13] " + (pass ? "PASS" : "FAIL") + "  " + page + " @ " + W + "×" + H,
              "font-weight:bold;font-size:14px;color:" + (pass ? "#3ddc97" : "#ff6b6b"));
  console.log("devicePixelRatio = " + window.devicePixelRatio +
              "，页面尺寸 " + doc.scrollWidth + "×" + doc.scrollHeight + "px");
  if (info.length) console.log("数字核对：" + info.join(" · "));
  if (!pass) {
    issues.forEach(function (s, i) { console.warn((i + 1) + ". " + s); });
  } else {
    console.log("未发现溢出 / 越界 / 字号 / 契约 id 问题。");
  }
  return { page: page, viewport: W + "x" + H, pass: pass, issues: issues, info: info };
})();
