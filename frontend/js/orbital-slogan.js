/**
 * js/orbital-slogan.js
 * Orbital Letters Slogan Transition.
 * Inspired by arihantcodes's Orbital Letters component.
 *
 * Letters orbit in along spring-damped elliptical trajectories, settle into
 * position, hold, and then orbit out before the next group arrives at a
 * newly randomized position strictly bounded within the container.
 *
 * Pairs:
 *   1. aidu · 爱嘟
 *   2. I do · 惟吾
 *   3. AI do · 智助
 */
(function (global) {
  "use strict";

  var SLOGANS = [
    { en: "aidu", cn: "爱嘟", tier: "ink" },
    { en: "I do", cn: "惟吾", tier: "gray" },
    { en: "AI do", cn: "智助", tier: "blue" },
  ];

  var DURATION_HOLD_MS = 2800; // 停留时间
  var DURATION_IN_MS = 900;   // 轨道汇聚时长
  var DURATION_OUT_MS = 600;  // 轨道离散时长

  function checkReducedMotion() {
    return (
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function OrbitalSloganCtrl(container, options) {
    this.root = container;
    if (!this.root) return;
    this.options = Object.assign({}, options || {});
    this.currentIndex = 0;
    this.stage = null;
    this.timer = null;
    this.destroyed = false;
    this.init();
  }

  OrbitalSloganCtrl.prototype.init = function () {
    this.root.innerHTML = "";
    this.root.classList.add("orbital-slogan-wrap");

    var stage = document.createElement("div");
    stage.className = "orbital-slogan-stage";
    this.root.appendChild(stage);
    this.stage = stage;

    var self = this;
    window.addEventListener("resize", function () {
      if (!self.destroyed) self.reclampPosition();
    });

    this.showGroup(this.currentIndex);
  };

  OrbitalSloganCtrl.prototype.reclampPosition = function () {
    if (!this.root || !this.stage) return;
    var containerW = this.root.clientWidth;
    var containerH = this.root.clientHeight;
    var stageW = this.stage.offsetWidth || 160;
    var stageH = this.stage.offsetHeight || 44;

    var maxLeft = Math.max(0, containerW - stageW - 20);
    var maxTop = Math.max(0, containerH - stageH - 8);

    var curLeft = parseFloat(this.stage.style.left) || 0;
    var curTop = parseFloat(this.stage.style.top) || 0;

    if (curLeft > maxLeft + 10) this.stage.style.left = Math.min(curLeft, maxLeft + 10) + "px";
    if (curTop > maxTop + 4) this.stage.style.top = Math.min(curTop, maxTop + 4) + "px";
  };

  OrbitalSloganCtrl.prototype.randomizePosition = function () {
    if (!this.root || !this.stage) return;
    var containerW = this.root.clientWidth;
    var containerH = this.root.clientHeight;
    var stageW = this.stage.offsetWidth || 160;
    var stageH = this.stage.offsetHeight || 44;

    if (this.options.centered) {
      this.stage.style.left = Math.max(0, (containerW - stageW) / 2) + "px";
      this.stage.style.top = Math.max(0, (containerH - stageH) / 2) + "px";
      return;
    }

    // 安全边界：左右各留 10px，上下各留 4px，在大高度区域内充分享受随机施展空间
    var maxLeft = Math.max(0, containerW - stageW - 20);
    var maxTop = Math.max(0, containerH - stageH - 8);

    var targetLeft = 10 + Math.floor(Math.random() * maxLeft);
    var targetTop = 4 + Math.floor(Math.random() * maxTop);

    this.stage.style.left = targetLeft + "px";
    this.stage.style.top = targetTop + "px";
  };

  OrbitalSloganCtrl.prototype.buildChars = function (item) {
    var fullText = item.en + " · " + item.cn;
    this.stage.innerHTML = "";
    this.stage.setAttribute("data-tier", item.tier);

    var charNodes = [];
    var totalChars = fullText.length;

    for (var i = 0; i < totalChars; i++) {
      var ch = fullText[i];
      var span = document.createElement("span");
      span.className = "orbital-char";
      span.textContent = ch === " " ? "\u00A0" : ch;

      // 为每个字符计算专属的轨道物理参数（角度、离心半径，更大更舒展的轨道空间）
      var angle = ((i / totalChars) * Math.PI * 2) + (Math.random() * 0.5 - 0.25);
      var distance = 60 + Math.random() * 80; // 扩大轨道半径至 60px - 140px，动作更舒展
      var rx = Math.cos(angle) * distance;
      var ry = Math.sin(angle) * (distance * 0.7); // 椭圆轨道
      var rot = (Math.random() * 90 - 45); // 随机自旋角度

      span.style.setProperty("--orb-ox", rx + "px");
      span.style.setProperty("--orb-oy", ry + "px");
      span.style.setProperty("--orb-rot", rot + "deg");
      span.style.setProperty("--orb-delay", (i * 40) + "ms");

      this.stage.appendChild(span);
      charNodes.push(span);
    }
    return charNodes;
  };

  OrbitalSloganCtrl.prototype.showGroup = function (index) {
    if (this.destroyed) return;
    var self = this;
    var item = SLOGANS[index];

    // 1. 构建字符节点并测量
    var charNodes = this.buildChars(item);

    // 2. 随机落位在安全矩形内
    this.randomizePosition();

    if (checkReducedMotion()) {
      charNodes.forEach(function (node) { node.classList.add("is-settled"); });
      this.timer = setTimeout(function () {
        self.nextGroup();
      }, DURATION_HOLD_MS);
      return;
    }

    // 3. 强制重绘后触发 Orbital In 动画
    void this.stage.offsetWidth;
    charNodes.forEach(function (node) {
      node.classList.add("is-in");
    });

    // 4. 保持展示并计划退出
    this.timer = setTimeout(function () {
      if (self.destroyed) return;
      // 触发 Orbital Out
      charNodes.forEach(function (node) {
        node.classList.remove("is-in");
        node.classList.add("is-out");
      });

      // 离场完成后切换到下一组
      setTimeout(function () {
        if (self.destroyed) return;
        self.nextGroup();
      }, DURATION_OUT_MS);
    }, DURATION_IN_MS + DURATION_HOLD_MS);
  };

  OrbitalSloganCtrl.prototype.nextGroup = function () {
    this.currentIndex = (this.currentIndex + 1) % SLOGANS.length;
    this.showGroup(this.currentIndex);
  };

  OrbitalSloganCtrl.prototype.destroy = function () {
    this.destroyed = true;
    if (this.timer) clearTimeout(this.timer);
  };

  global.OrbitalSlogan = {
    attach: function (container, options) {
      if (!container) return null;
      return new OrbitalSloganCtrl(container, options);
    },
  };
})(window);
