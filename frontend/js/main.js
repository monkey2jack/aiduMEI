/* =============================================================================
   aiduMEI — page wiring v2
   ============================================================================= */

/* ---------------------------------------------------------------------------
   random hexagon backdrop (ported from hycoForce)
   --------------------------------------------------------------------------- */
function createHexBackground(selector, count, colors, opacity) {
  const container = document.querySelector(selector);
  if (!container) return;
  container.innerHTML = '';

  const palette = [];
  const per = Math.floor(count / colors.length);
  colors.forEach(function (c) {
    for (let i = 0; i < per; i++) palette.push(c);
  });
  while (palette.length < count) palette.push(colors[palette.length % colors.length]);
  for (let i = palette.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    const t = palette[i]; palette[i] = palette[j]; palette[j] = t;
  }

  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const left = Math.random() * 100;
    const top = Math.random() * 100;
    const size = 15 + Math.random() * 70;
    const height = size * 1.1547;
    const rot = Math.floor(Math.random() * 61) - 30;
    const stroke = (0.3 + Math.random() * 0.5).toFixed(1);

    const el = document.createElement('div');
    el.className = 'hex-bg-item';
    el.style.cssText = 'left:' + left + '%; top:' + top + '%; width:' + size +
      'px; height:' + height + 'px; transform:rotate(' + rot + 'deg); opacity:' + opacity + ';';
    el.innerHTML = '<svg viewBox="0 0 100 115.47"><polygon points="50,0 100,28.87 100,86.6 50,115.47 0,86.6 0,28.87" fill="none" stroke="' +
      palette[i] + '" stroke-width="' + stroke + '"/></svg>';
    frag.appendChild(el);
  }
  container.appendChild(frag);
}

const BRAND_COLORS = ['#1f4e79', '#525252', '#000000'];

/* ---------------------------------------------------------------------------
   footer version display + version check
   --------------------------------------------------------------------------- */

// Current deployed version — mapped from aiduMEM /health
let currentVersion = '—';
const GITHUB_REPO_API = 'https://api.github.com/repos/monkey2jack/aiduMEI/releases/latest';

function setDeployedVersion(ver) {
  currentVersion = ver || '—';
  document.getElementById('foot-ver').textContent = 'v' + currentVersion;
}

// numeric segment compare: -1 / 0 / +1
// version like "18.2.0-zeus" is normalized to its pure numeric part first.
function cmpVersion(a, b) {
  const norm = function (s) {
    return String(s).replace(/^v/i, '').replace(/-.*$/, '').trim();
  };
  const pa = norm(a).split('.').map(function (x) { return parseInt(x, 10) || 0; });
  const pb = norm(b).split('.').map(function (x) { return parseInt(x, 10) || 0; });
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

async function checkLatestVersion() {
  const badge = document.getElementById('foot-update');
  try {
    const resp = await fetch(GITHUB_REPO_API, { headers: { Accept: 'application/vnd.github+json' } });
    if (!resp.ok) return;
    const release = await resp.json();
    const latest = (release.tag_name || '').replace(/^v/, '');
    if (!latest || !currentVersion || currentVersion === '—') return;
    // Show badge only when the deployed version lags behind GitHub latest.
    if (cmpVersion(currentVersion, latest) < 0) {
      badge.textContent = '最新 / Latest';
      badge.style.display = 'inline-block';
    }
  } catch (e) {
    // silently ignore — GitHub API might be rate-limited
  }
}

/* ---------------------------------------------------------------------------
   panel plumbing
   --------------------------------------------------------------------------- */
const overlay = document.getElementById('overlay');
const panelIco = document.getElementById('panelIco');
const panelEn = document.getElementById('panelEn');
const panelTitle = document.getElementById('panelTitle');
const panelBody = document.getElementById('panelBody');
const panelFoot = document.getElementById('panelFoot');

let openKey = null;
let lastFocus = null;

async function openPanel(key) {
  const def = PANELS[key];
  if (!def) return;

  openKey = key;
  lastFocus = document.activeElement;

  // Measure header/footer positions to constrain panel between them
  var header = document.querySelector('.brandbar');
  var footer = document.querySelector('.site-foot');
  var headerBottom = header ? header.getBoundingClientRect().bottom : 0;
  var footerTop = footer ? footer.getBoundingClientRect().top : window.innerHeight;
  var availH = footerTop - headerBottom;

  // Position overlay over the stage area only
  overlay.style.top = headerBottom + 'px';
  overlay.style.bottom = (window.innerHeight - footerTop) + 'px';
  overlay.style.background = 'rgba(255,255,255,0.45)';
  overlay.style.backdropFilter = 'blur(2px)';
  overlay.style.webkitBackdropFilter = 'blur(2px)';

  var panel = overlay.querySelector('.panel');
  panel.style.height = availH + 'px';

  // icon
  const pngMap = {
    pulse: 'pulse.png', vault: 'vault.png', map: 'map.png',
    recall: 'recall.png', evolve: 'evolve.png', settings: 'setting.png'
  };
  const png = pngMap[key];
  panelIco.innerHTML = png ? '<img src="' + png + '" alt="" />' : '';

  // title: EN (gray) then CN (blue)
  panelEn.textContent = def.en;
  panelTitle.textContent = def.cn;

  // footer: the hover tooltip text (EN + CN)
  var btn = document.getElementById(def.hex);
  var tipEn = btn ? btn.getAttribute('data-tip-en') : '';
  var tipCn = btn ? btn.getAttribute('data-tip-cn') : '';
  panelFoot.innerHTML = '<span class="pf-en">' + esc(tipEn) + '</span> <span class="pf-cn">' + esc(tipCn) + '</span>';

  panelBody.innerHTML = '';
  panelBody.scrollTop = 0;

  createHexBackground('#panelHexBg', 220, BRAND_COLORS, 0.16);

  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';
  if (location.hash !== '#' + key) history.replaceState(null, '', '#' + key);
  document.getElementById('panelClose').focus();

  const t0 = performance.now();
  try {
    await def.render(panelBody);
  } catch (e) {
    panelBody.innerHTML = failure(e);
  }
}

function closePanel() {
  overlay.classList.remove('open');
  overlay.style.top = '';
  overlay.style.bottom = '';
  document.body.style.overflow = '';
  openKey = null;
  if (location.hash) history.replaceState(null, '', location.pathname);
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

document.querySelectorAll('[data-panel]').forEach(function (el) {
  el.addEventListener('click', function (e) {
    e.preventDefault();
    openPanel(el.dataset.panel);
  });
});

document.getElementById('panelClose').addEventListener('click', closePanel);

overlay.addEventListener('click', function (e) {
  if (e.target === overlay) closePanel();
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && openKey) closePanel();
});

/* ---------------------------------------------------------------------------
   hover tooltip: single floating element, follows mouse cursor
   --------------------------------------------------------------------------- */
(function () {
  var tip = document.createElement('div');
  tip.className = 'hex-tip';
  tip.innerHTML = '<span class="ht-en"></span><span class="ht-cn"></span>';
  document.body.appendChild(tip);

  var currentBtn = null;

  document.querySelectorAll('.hex-btn[data-tip-en]').forEach(function (btn) {
    btn.addEventListener('mouseenter', function (e) {
      currentBtn = btn;
      tip.querySelector('.ht-en').textContent = btn.getAttribute('data-tip-en') || '';
      tip.querySelector('.ht-cn').textContent = btn.getAttribute('data-tip-cn') || '';
      tip.style.display = 'block';
      moveTip(e);
    });
    btn.addEventListener('mousemove', function (e) {
      moveTip(e);
    });
    btn.addEventListener('mouseleave', function () {
      currentBtn = null;
      tip.style.display = 'none';
    });
  });

  function moveTip(e) {
    var x = e.clientX + 16;
    var y = e.clientY + 18;
    // keep inside viewport
    var tw = tip.offsetWidth;
    var th = tip.offsetHeight;
    if (x + tw > window.innerWidth - 10) x = e.clientX - tw - 8;
    if (y + th > window.innerHeight - 10) y = e.clientY - th - 8;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
})();

/* ---------------------------------------------------------------------------
   boot
   --------------------------------------------------------------------------- */
createHexBackground('#hexBg', 666, BRAND_COLORS, 0.3);

// Kick off health check to get deployed version
(async function boot() {
  try {
    const h = await API.get('/health');
    setDeployedVersion(h.version || '');
  } catch (e) {
    setDeployedVersion('—');
  }
  checkLatestVersion();
})();

// deep link
const initial = location.hash.replace('#', '');
if (initial && PANELS[initial]) openPanel(initial);
