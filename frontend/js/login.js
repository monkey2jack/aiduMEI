/* 从 login.html 的 inline <script> 原样搬出（v20.4-alpha，2026-09-08）。
   搬家理由同 js/auth-guard.js：CSP script-src 'self' 不执行 inline 块。
   内容逐字未改（六边形背景 + 版本徽章 + 口令提示 + 登录提交）。 */
/* random hexagon backdrop (same as index) */
(function () {
  var BRAND_COLORS = ['#1f4e79', '#525252', '#000000'];
  var container = document.getElementById('hexBg');
  var count = 666;
  var palette = [];
  var per = Math.floor(count / BRAND_COLORS.length);
  BRAND_COLORS.forEach(function (c) {
    for (var i = 0; i < per; i++) palette.push(c);
  });
  while (palette.length < count) palette.push(BRAND_COLORS[palette.length % BRAND_COLORS.length]);
  for (var i = palette.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var t = palette[i]; palette[i] = palette[j]; palette[j] = t;
  }
  var frag = document.createDocumentFragment();
  for (var k = 0; k < count; k++) {
    var left = Math.random() * 100;
    var top = Math.random() * 100;
    var size = 15 + Math.random() * 70;
    var height = size * 1.1547;
    var rot = Math.floor(Math.random() * 61) - 30;
    var stroke = (0.3 + Math.random() * 0.5).toFixed(1);
    var el = document.createElement('div');
    el.className = 'hex-bg-item';
    el.style.cssText = 'left:' + left + '%; top:' + top + '%; width:' + size +
      'px; height:' + height + 'px; transform:rotate(' + rot + 'deg); opacity:0.3;';
    el.innerHTML = '<svg viewBox="0 0 100 115.47"><polygon points="50,0 100,28.87 100,86.6 50,115.47 0,86.6 0,28.87" fill="none" stroke="' +
      palette[k] + '" stroke-width="' + stroke + '"/></svg>';
    frag.appendChild(el);
  }
  container.appendChild(frag);
})();

/* version badge (same as index) */
fetch('/api/health', { headers: { Accept: 'application/json' } })
  .then(function (r) { return r.json().catch(function () { return {}; }); })
  .then(function (d) {
    var ver = d.version || '';
    if (ver) document.getElementById('foot-ver').textContent = 'v' + ver;
  })
  .catch(function () {});

/* password hint (backend decides whether default password applies) */
fetch('/api/login/hint', { headers: { Accept: 'application/json' } })
  .then(function (r) { return r.json().catch(function () { return {}; }); })
  .then(function (d) {
    if (d.hint) document.getElementById('loginHint').textContent = d.hint;
  })
  .catch(function () {});

/* login */
(function () {
  var form = document.getElementById('loginForm');
  var passwordInput = document.getElementById('password');
  var btn = document.getElementById('loginBtn');
  var err = document.getElementById('loginErr');
  var card = document.getElementById('loginCard');

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var password = passwordInput.value;
    if (!password) { err.textContent = '请输入密码 / Please enter the password'; return; }
    err.textContent = '';
    btn.disabled = true;
    btn.textContent = '验证中… / Verifying…';

    /* v19.4.1 P0-1: credentials:'same-origin' so the HttpOnly session cookie
       issued by the backend is stored and sent on subsequent panel requests. */
    fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ password: password })
    })
      .then(function (r) {
        return r.json().catch(function () { return { success: false }; }).then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        if (res.ok && res.d.success) {
          sessionStorage.setItem('aidumei_auth', '1');
          location.replace('index.html');
        } else {
          err.textContent = (res.d && res.d.message) || '密码错误 / Wrong password';
          card.classList.remove('shake');
          void card.offsetWidth;
          card.classList.add('shake');
          passwordInput.value = '';
          passwordInput.focus();
        }
      })
      .catch(function () {
        err.textContent = '无法连接服务 / Connection failed';
      })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = '登 录 / Login';
      });
  });
})();
