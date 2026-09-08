/* 从 index.html 的 inline <script> 原样搬出（v20.4-alpha，2026-09-08）。
   搬家理由：CSP script-src 'self' 不执行 inline 块——生产 3209 热修曾为此
   放宽 CSP，这里改成收进外部文件，CSP 保持紧口径。内容逐字未改。 */
/* v19.4.1 P0-1: this is a UX shortcut only — it avoids rendering an empty
   console before the first 401. The real gate lives server-side (HttpOnly
   session cookie validated by the auth middleware); API.get/post redirect
   here on any 401. Clearing sessionStorage grants no access. */
if (!sessionStorage.getItem('aidumei_auth')) {
  location.replace('login.html');
}
