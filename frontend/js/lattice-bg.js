/* =============================================================================
   lattice-bg.js
   -----------------------------------------------------------------------------
   Tri-colour geometric lattice backdrop. Vanilla port of the "Lattice
   Background" component by daiwiikharihar
   (https://21st.dev/@daiwiikharihar/components/lattice-background,
   sourced via app.viainti.com). Replaces the old random hexagon field.

   Brand discipline: the mesh only ever draws in the site's three brand
   colours — blue #1F4E79, gray #525252, black #000000 (the :root vars in
   css/style.css). Geometry near the pointer is highlighted by raising the
   alpha of the SAME palette colour, never by adding a fourth hue.

   The canvas itself is transparent: whatever paper the surface already has
   (homepage body, card popup, side popup, docs page) shows through, so every
   popup level inherits the exact same backdrop as the homepage.

   Usage: window.LatticeBG.mount(el) / window.LatticeBG.unmount(el).
   mount() is idempotent; unmount() stops the rAF loop and removes the canvas.
   Honours prefers-reduced-motion with a single static frame.
   ============================================================================= */

(function () {
  "use strict";

  /* Brand tri-colour palette as [r, g, b]; each point owns one colour. */
  var PALETTE = [
    [31, 78, 121], /* --blue  #1F4E79 */
    [82, 82, 82],  /* --gray  #525252  */
    [0, 0, 0],     /* --ink   #000000  */
  ];

  /* Pre-built rgba() strings per palette colour with alpha quantised to
     0.01 — the frame loop must never allocate colour strings. */
  var COLOR_TABLE = PALETTE.map(function (rgb) {
    var table = new Array(101);
    for (var i = 0; i <= 100; i++) {
      table[i] = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (i / 100).toFixed(2) + ")";
    }
    return table;
  });

  function rgba(colorIdx, alpha) {
    var q = Math.round(alpha * 100);
    if (q < 0) q = 0;
    else if (q > 100) q = 100;
    return COLOR_TABLE[colorIdx][q];
  }

  var MAX_DIST = 140;
  var MAX_DIST_SQ = MAX_DIST * MAX_DIST;
  var MOUSE_REPEL_RADIUS_SQ = 200 * 200;
  var MOUSE_ACCENT_RADIUS = 220;
  var MOUSE_ACCENT_RADIUS_SQ = MOUSE_ACCENT_RADIUS * MOUSE_ACCENT_RADIUS;

  /* Alpha tuning for the white paper: the far field stays a quiet but
     clearly present mesh (a touch stronger than the old 0.3-opacity
     hexagons), the pointer wake is the loud region. */
  var FILL_FAR = 0.05;
  var STROKE_FAR = 0.12;
  var FILL_NEAR_MAX = 0.16;
  var NODE_FAR = 0.42;

  var instances = typeof WeakMap !== "undefined" ? new WeakMap() : null;

  function motionReduced() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function initPoints(inst, w, h) {
    var points = [];
    var density = Math.floor((w * h) / 9000);
    var cap = Math.min(w, h) < 768 ? 70 : 120;
    var count = Math.min(Math.max(density, 40), cap);
    for (var i = 0; i < count; i++) {
      points.push({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.8,
        vy: (Math.random() - 0.5) * 0.8,
        pulse: Math.random() * Math.PI * 2,
        pulseSpeed: 1 + Math.random() * 1.5,
        color: i % PALETTE.length,
      });
    }
    inst.points = points;
  }

  function resize(inst) {
    var rect = inst.container.getBoundingClientRect();
    var w = rect.width;
    var h = rect.height;
    if (w < 2 || h < 2) {
      inst.hidden = true;
      return;
    }
    inst.hidden = false;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    inst.width = w;
    inst.height = h;
    inst.canvas.width = Math.round(w * dpr);
    inst.canvas.height = Math.round(h * dpr);
    inst.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    initPoints(inst, w, h);
    if (inst.staticOnly) drawFrame(inst, performance.now(), 0.016);
  }

  function onPointerMove(inst, e) {
    var rect = inst.container.getBoundingClientRect();
    inst.mouse.tx = e.clientX - rect.left;
    inst.mouse.ty = e.clientY - rect.top;
  }

  function onPointerOut(inst) {
    inst.mouse.tx = -1000;
    inst.mouse.ty = -1000;
  }

  function drawFrame(inst, now, dt) {
    var ctx = inst.ctx;
    var width = inst.width;
    var height = inst.height;
    var mouse = inst.mouse;
    var points = inst.points;

    mouse.x += (mouse.tx - mouse.x) * 0.1;
    mouse.y += (mouse.ty - mouse.y) * 0.1;

    ctx.clearRect(0, 0, width, height);

    /* 1. point physics: drift, wall bounce, pointer repulsion */
    var pCount = points.length;
    for (var i = 0; i < pCount; i++) {
      var p = points[i];
      p.pulse += dt * p.pulseSpeed;
      p.x += p.vx * dt * 60;
      p.y += p.vy * dt * 60;
      if (p.x < 0) { p.x = 0; p.vx *= -1; }
      else if (p.x > width) { p.x = width; p.vx *= -1; }
      if (p.y < 0) { p.y = 0; p.vy *= -1; }
      else if (p.y > height) { p.y = height; p.vy *= -1; }

      var mdx = mouse.x - p.x;
      var mdy = mouse.y - p.y;
      var mDistSq = mdx * mdx + mdy * mdy;
      if (mDistSq < MOUSE_REPEL_RADIUS_SQ && mDistSq > 0) {
        var mDist = Math.sqrt(mDistSq);
        var force = (1 - mDist / 200) * 35;
        p.x -= (mdx / mDist) * force * dt * 6;
        p.y -= (mdy / mDist) * force * dt * 6;
      }
    }

    /* 2. spatial grid partition for the triangulation lookup */
    var cellSize = MAX_DIST;
    var cols = Math.max(1, Math.ceil(width / cellSize));
    var rows = Math.max(1, Math.ceil(height / cellSize));
    var grid = new Array(cols);
    for (var c = 0; c < cols; c++) {
      grid[c] = new Array(rows);
      for (var r = 0; r < rows; r++) grid[c][r] = [];
    }
    for (i = 0; i < pCount; i++) {
      p = points[i];
      var gc = Math.min(cols - 1, Math.max(0, Math.floor(p.x / cellSize)));
      var gr = Math.min(rows - 1, Math.max(0, Math.floor(p.y / cellSize)));
      grid[gc][gr].push(i);
    }

    /* 3. triangulated mesh: fill + stroke in the owning point's colour */
    for (c = 0; c < cols; c++) {
      for (r = 0; r < rows; r++) {
        var cellPoints = grid[c][r];
        if (!cellPoints.length) continue;

        var neighbors = [];
        for (var nc = Math.max(0, c - 1); nc <= Math.min(cols - 1, c + 1); nc++) {
          for (var nr = Math.max(0, r - 1); nr <= Math.min(rows - 1, r + 1); nr++) {
            var nList = grid[nc][nr];
            for (var k = 0; k < nList.length; k++) neighbors.push(nList[k]);
          }
        }
        var neighborCount = neighbors.length;

        for (i = 0; i < cellPoints.length; i++) {
          var idx1 = cellPoints[i];
          var p1 = points[idx1];

          for (var j = 0; j < neighborCount; j++) {
            var idx2 = neighbors[j];
            if (idx1 >= idx2) continue;
            var p2 = points[idx2];
            var dx12 = p1.x - p2.x;
            var dy12 = p1.y - p2.y;
            if (dx12 * dx12 + dy12 * dy12 > MAX_DIST_SQ) continue;

            for (k = j + 1; k < neighborCount; k++) {
              var idx3 = neighbors[k];
              if (idx2 >= idx3) continue;
              var p3 = points[idx3];
              var dx23 = p2.x - p3.x;
              var dy23 = p2.y - p3.y;
              if (dx23 * dx23 + dy23 * dy23 > MAX_DIST_SQ) continue;
              var dx31 = p3.x - p1.x;
              var dy31 = p3.y - p1.y;
              if (dx31 * dx31 + dy31 * dy31 > MAX_DIST_SQ) continue;

              var avgX = (p1.x + p2.x + p3.x) * 0.3333;
              var avgY = (p1.y + p2.y + p3.y) * 0.3333;
              var tDx = mouse.x - avgX;
              var tDy = mouse.y - avgY;
              var tDistSq = tDx * tDx + tDy * tDy;
              var near = tDistSq < MOUSE_ACCENT_RADIUS_SQ;

              var fillAlpha = near
                ? (1 - Math.sqrt(tDistSq) / MOUSE_ACCENT_RADIUS) * FILL_NEAR_MAX
                : FILL_FAR;
              ctx.fillStyle = rgba(p1.color, fillAlpha);
              ctx.strokeStyle = rgba(p1.color, near ? fillAlpha * 1.6 : STROKE_FAR);
              ctx.lineWidth = near ? 0.8 : 0.4;

              ctx.beginPath();
              ctx.moveTo(p1.x, p1.y);
              ctx.lineTo(p2.x, p2.y);
              ctx.lineTo(p3.x, p3.y);
              ctx.closePath();
              ctx.fill();
              ctx.stroke();
            }
          }
        }
      }
    }

    /* 4. nodes + pulse rings near the pointer */
    for (i = 0; i < pCount; i++) {
      p = points[i];
      var nDx = mouse.x - p.x;
      var nDy = mouse.y - p.y;
      var isNear = nDx * nDx + nDy * nDy < MOUSE_ACCENT_RADIUS_SQ;

      ctx.fillStyle = rgba(p.color, isNear ? 0.9 : NODE_FAR);
      ctx.beginPath();
      ctx.arc(p.x, p.y, isNear ? 3.5 : 1.8 + Math.sin(p.pulse) * 1.0, 0, Math.PI * 2);
      ctx.fill();

      if (isNear) {
        ctx.strokeStyle = rgba(p.color, 0.35);
        ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 7 + Math.sin(p.pulse * 2) * 2.5, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
  }

  function tick(inst, now) {
    if (inst.destroyed) return;
    var dt = Math.min((now - inst.lastTime) / 1000, 0.033);
    inst.lastTime = now;
    if (!inst.hidden) drawFrame(inst, now, dt);
    inst.rafId = requestAnimationFrame(function (t) { tick(inst, t); });
  }

  function mount(container) {
    if (!container || instances.has(container)) return null;

    var canvas = document.createElement("canvas");
    canvas.className = "lattice-bg-canvas";
    canvas.setAttribute("aria-hidden", "true");
    container.appendChild(canvas);

    var inst = {
      container: container,
      canvas: canvas,
      ctx: canvas.getContext("2d", { alpha: true }),
      width: 0,
      height: 0,
      points: [],
      mouse: { x: -1000, y: -1000, tx: -1000, ty: -1000 },
      rafId: 0,
      lastTime: performance.now(),
      hidden: false,
      destroyed: false,
      staticOnly: motionReduced(),
      resizeObserver: null,
      moveHandler: null,
      outHandler: null,
    };
    instances.set(container, inst);

    inst.resizeObserver = new ResizeObserver(function () { resize(inst); });
    inst.resizeObserver.observe(container);

    /* The backdrop containers are pointer-events:none, so pointer tracking
       lives on window and is translated into container coordinates. */
    inst.moveHandler = function (e) { onPointerMove(inst, e); };
    inst.outHandler = function (e) {
      if (!e.relatedTarget) onPointerOut(inst);
    };
    window.addEventListener("pointermove", inst.moveHandler, { passive: true });
    window.addEventListener("pointerout", inst.outHandler, { passive: true });
    window.addEventListener("blur", inst.outHandler);

    resize(inst);
    if (!inst.staticOnly) {
      inst.rafId = requestAnimationFrame(function (t) { tick(inst, t); });
    }
    return inst;
  }

  function unmount(container) {
    var inst = container && instances.get(container);
    if (!inst) return;
    inst.destroyed = true;
    cancelAnimationFrame(inst.rafId);
    inst.resizeObserver.disconnect();
    window.removeEventListener("pointermove", inst.moveHandler);
    window.removeEventListener("pointerout", inst.outHandler);
    window.removeEventListener("blur", inst.outHandler);
    inst.canvas.remove();
    instances.delete(container);
  }

  window.LatticeBG = { mount: mount, unmount: unmount };
})();
