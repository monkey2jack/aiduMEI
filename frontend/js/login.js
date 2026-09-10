/* 从 login.html 的 inline <script> 原样搬出（v20.4-alpha，2026-09-08）。
   搬家理由同 js/auth-guard.js：CSP script-src 'self' 不执行 inline 块。
   内容逐字未改（六边形背景 + 版本徽章 + 口令提示 + 登录提交）。 */
/* aiduPARK lattice-bg backdrop (tri-colour geometric lattice) */
(function () {
  var container = document.getElementById('hexBg');
  if (container && window.LatticeBG) {
    window.LatticeBG.mount(container);
  }
})();

/* Orbital Slogan Letters animation */
(function () {
  var host = document.getElementById('heroSloganOrbital');
  if (host && window.OrbitalSlogan) {
    window.OrbitalSlogan.attach(host, { centered: true });
  }
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
