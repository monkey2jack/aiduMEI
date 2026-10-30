/* =============================================================================
   aiduMEI — page wiring v2
   ============================================================================= */

/* ---------------------------------------------------------------------------
   lattice-bg backdrop
   --------------------------------------------------------------------------- */
function createHexBackground(selector, count, colors, opacity) {
  const container = document.querySelector(selector);
  if (!container) return;
  if (window.LatticeBG) {
    window.LatticeBG.mount(container);
    return;
  }
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
   footer version display
   --------------------------------------------------------------------------- */

// Current deployed version — mapped from aiduMEI /health
let currentVersion = '—';

function setDeployedVersion(ver, fullVer) {
  currentVersion = fullVer || ver || '—';
  var display = currentVersion;
  if (display !== '—' && !display.startsWith('f') && !display.startsWith('v')) {
    display = 'v' + display;
  }
  var el = document.getElementById('foot-ver');
  if (el) {
    el.textContent = display;
    if (fullVer) {
      el.href = 'https://github.com/monkey2jack/aiduMEI/releases/tag/' + fullVer;
    }
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
  // 设定面板(settings)与成真面板(evolve)属于系统级全局运维监控，不绑定单个域；数据面板(vault/pulse/map/recall)展示当前域
  let domSuffix = '';
  if (key !== 'settings' && key !== 'evolve') {
    const activeDom = API.getActiveDomain();
    if (activeDom) {
      const domUser = activeDom.split(':')[0];
      let icon = '⚙️';
      if (domUser === 'hermes') icon = '🐎';
      else if (domUser === 'openclaw') icon = '🦞';
      domSuffix = ' · ' + icon + ' ' + domUser;
    }
  }
  panelTitle.textContent = def.cn + domSuffix;

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
   orbital slogan（与登录页同一动效，v20.5.0 继承修复）
   --------------------------------------------------------------------------- */
(function () {
  var host = document.getElementById('heroSloganOrbital');
  if (host && window.OrbitalSlogan) {
    window.OrbitalSlogan.attach(host, { centered: true });
  }
})();

/* ---------------------------------------------------------------------------
   boot & domain switcher
   --------------------------------------------------------------------------- */
createHexBackground('#hexBg', 666, BRAND_COLORS, 0.3);

async function initDomainSwitcher() {
  // f0.4++：域切换下拉已按用户要求从 UI 移除（版本号旁不该有下拉菜单）。
  // 但本函数**不能删** —— api.js 的数据面请求会 `await API.domainReady`，
  // 而 domainReady 只在这里 resolve。删了它 = 每个面板请求永久挂起。
  // 所以保留「拉 /domains 目录 + 激活默认域 + resolve」这条命脉，只去掉
  // 对已不存在的 <select> DOM 的所有引用。UI 从此固定使用服务端 default_domain。
  try {
    const res = await API.get('/domains');
    const domains = (res && res.domains) || [];
    API.domainCatalog = domains.filter(function (d) {
      return d && d.status === 'active' && d.user_id && d.bank_id;
    });
    API.defaultDomain = (res && res.default_domain) || null;

    if (!API.domainCatalog.length) {
      if (API.resolveDomainReady) API.resolveDomainReady();
      return;
    }
  } catch (e) {
    // 域目录加载失败：清空目录（不发带猜测 scope 的请求），但必须 resolve，
    // 否则数据面请求全卡死。
    API.domainCatalog = [];
    if (API.resolveDomainReady) API.resolveDomainReady();
    return;
  }

  // 选中的域：优先服务端仍标记为 active 的历史选择，否则回落默认域/首个。
  const stored = (function () {
    try { return localStorage.getItem('aidumei_active_domain') || ''; } catch (e) { return ''; }
  })();
  const serverDefault = API.defaultDomain && API.domainCatalog.some(function (d) {
    return API.domainKey(d) === API.domainKey(API.defaultDomain);
  }) ? API.domainKey(API.defaultDomain) : '';
  const fallback = serverDefault || API.domainKey(API.domainCatalog[0]);
  const active = API.domainCatalog.some(function (d) { return API.domainKey(d) === stored; }) ? stored : fallback;
  API.setActiveDomain(active);
  if (API.resolveDomainReady) API.resolveDomainReady();
}

// Kick off health check to get deployed version
(async function boot() {
  API.domainReady = new Promise(function (resolve) { API.resolveDomainReady = resolve; });
  try {
    const h = await API.get('/health');
    setDeployedVersion(h.version || '', h.full_version || '');
  } catch (e) {
    setDeployedVersion('—');
  }
  await initDomainSwitcher();
})();

// deep link
const initial = location.hash.replace('#', '');
if (initial && PANELS[initial]) openPanel(initial);
