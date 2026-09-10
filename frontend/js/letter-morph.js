/**
 * js/letter-morph.js
 * Gooey letter-morph word transition.
 * Vanilla ES5/ES6 port of senommu's LetterMorph React component
 * (prompt-senommu-letter-morph / 21st.dev).
 *
 * Two stacked text layers crossfade through blur while an inline SVG
 * feColorMatrix filter fuses the silhouettes — letters reshape into each
 * other instead of swapping.
 *
 * Usage: window.LetterMorph.attach(el, { words, morphTime, cooldownTime })
 */
(function (global) {
  "use strict";

  var morphIdCounter = 0;

  function blurFor(fraction) {
    var f = Math.max(fraction, 0.0001);
    return Math.min(8 / f - 8, 100);
  }

  function checkReducedMotion() {
    return (
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function LetterMorphCtrl(el, options) {
    this.root = el;
    if (!this.root) return;
    this.options = Object.assign(
      { words: [], morphTime: 1.2, cooldownTime: 1.5 },
      options || {}
    );
    this.words = this.options.words.filter(function (w) { return w.length > 0; });
    if (this.words.length < 2) {
      this.root.textContent = this.words[0] || "";
      return;
    }
    this.morphSeconds = Math.max(this.options.morphTime, 0.05);
    this.cooldownSeconds = Math.max(this.options.cooldownTime, 0);
    this.textIndex = this.words.length - 1;
    this.morph = 0;
    this.cooldown = this.cooldownSeconds;
    this.rafId = 0;
    this.lastTime = 0;
    this.destroyed = false;
    this.init();
  }

  LetterMorphCtrl.prototype.init = function () {
    var id = "lm-filter-" + (++morphIdCounter);
    this.root.innerHTML = "";
    this.root.setAttribute("aria-live", "polite");
    this.root.setAttribute("aria-atomic", "true");

    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    svg.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none;";
    var defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    var filter = document.createElementNS("http://www.w3.org/2000/svg", "filter");
    filter.setAttribute("id", id);
    var matrix = document.createElementNS("http://www.w3.org/2000/svg", "feColorMatrix");
    matrix.setAttribute("in", "SourceGraphic");
    matrix.setAttribute("type", "matrix");
    matrix.setAttribute("values", "1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 255 -140");
    filter.appendChild(matrix);
    defs.appendChild(filter);
    svg.appendChild(defs);
    this.root.appendChild(svg);

    var sizer = document.createElement("span");
    sizer.className = "letter-morph__sizer";
    sizer.textContent = this.words.reduce(function (a, b) {
      return b.length > a.length ? b : a;
    }, "");
    this.root.appendChild(sizer);

    var stage = document.createElement("div");
    stage.className = "letter-morph__stage";
    stage.style.filter = "url(#" + id + ")";

    this.text1 = document.createElement("span");
    this.text1.className = "letter-morph__layer";
    this.text2 = document.createElement("span");
    this.text2.className = "letter-morph__layer";
    stage.appendChild(this.text1);
    stage.appendChild(this.text2);
    this.root.appendChild(stage);

    this.stage = stage;
    this.sizer = sizer;

    this.text1.textContent = this.words[this.textIndex];
    this.text2.textContent = this.words[0];
    this.doCooldown();
    this.randomizePosition();
    this.lastTime = performance.now();
    this.startAnimation();
  };

  LetterMorphCtrl.prototype.randomizePosition = function () {
    var stageW = this.sizer.scrollWidth;
    this.stage.style.width = stageW + "px";
    var containerW = this.root.clientWidth;
    var maxLeft = Math.max(0, containerW - stageW);
    this.stage.style.left = Math.floor(Math.random() * maxLeft) + "px";
  };

  LetterMorphCtrl.prototype.setMorph = function (fraction) {
    if (this.destroyed) return;
    this.text2.style.filter = "blur(" + blurFor(fraction) + "px)";
    this.text2.style.opacity = String(Math.pow(fraction, 0.4));
    var inv = 1 - fraction;
    this.text1.style.filter = "blur(" + blurFor(inv) + "px)";
    this.text1.style.opacity = String(Math.pow(inv, 0.4));
  };

  LetterMorphCtrl.prototype.doCooldown = function () {
    this.morph = 0;
    this.text2.style.filter = "";
    this.text2.style.opacity = "1";
    this.text1.style.filter = "";
    this.text1.style.opacity = "0";
  };

  LetterMorphCtrl.prototype.doMorph = function () {
    this.morph -= this.cooldown;
    this.cooldown = 0;
    var fraction = this.morph / this.morphSeconds;
    if (fraction > 1) {
      this.cooldown = this.cooldownSeconds;
      fraction = 1;
    }
    this.setMorph(fraction);
  };

  LetterMorphCtrl.prototype.startAnimation = function () {
    var self = this;
    if (checkReducedMotion()) {
      this.startReducedMotionFallback();
      return;
    }
    function animate(now) {
      if (self.destroyed) return;
      self.rafId = requestAnimationFrame(animate);
      var shouldIncrement = self.cooldown > 0;
      var dt = Math.min((now - self.lastTime) / 1000, 0.1);
      self.lastTime = now;
      self.cooldown -= dt;
      if (self.cooldown <= 0) {
        if (shouldIncrement) {
          self.textIndex = (self.textIndex + 1) % self.words.length;
          var current = self.words[self.textIndex];
          var next = self.words[(self.textIndex + 1) % self.words.length];
          self.text1.textContent = current;
          self.text2.textContent = next;
          self.randomizePosition();
        }
        self.doMorph();
      } else {
        self.doCooldown();
      }
    }
    this.rafId = requestAnimationFrame(animate);
  };

  LetterMorphCtrl.prototype.startReducedMotionFallback = function () {
    var self = this;
    this.text2.style.opacity = "1";
    this.text2.style.filter = "";
    this.text1.style.opacity = "0";
    this.text2.textContent = this.words[0];
    var idx = 0;
    this.intervalId = setInterval(function () {
      idx = (idx + 1) % self.words.length;
      self.text2.textContent = self.words[idx];
    }, (this.morphSeconds + this.cooldownSeconds) * 1000);
  };

  LetterMorphCtrl.prototype.destroy = function () {
    this.destroyed = true;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    if (this.intervalId) clearInterval(this.intervalId);
  };

  global.LetterMorph = {
    attach: function (el, options) {
      if (!el) return null;
      return new LetterMorphCtrl(el, options);
    },
  };
})(window);
