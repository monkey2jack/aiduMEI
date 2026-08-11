/* =============================================================================
   aiduMEI — API adapter
   -----------------------------------------------------------------------------
   Talks to aiduMEM through the /api/* alias layer served by the backend itself.
   The /api prefix routes to the same flat endpoints the backend natively exposes;
   the control台 frontend and the API share a single origin, no CORS needed.

   aiduMEM's payload shapes are not uniform — /recent nests results twice,
   /search nests once, /facts uses its own key, /knowledge/tree returns a bare
   object. Normalising happens here so panel code stays clean.
   ============================================================================= */

const API = {
  base: '/api',

  async get(path, params) {
    let url = this.base + path;
    if (params) {
      const q = new URLSearchParams(params).toString();
      if (q) url += '?' + q;
    }
    const r = await fetch(url, { headers: { Accept: 'application/json' } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(r.status, body, path);
    return body;
  },

  async post(path, payload) {
    const r = await fetch(this.base + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload || {}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(r.status, body, path);
    return body;
  },
};

class ApiError extends Error {
  constructor(status, body, path) {
    super((body && (body.error || body.detail)) || 'HTTP ' + status);
    this.status = status;
    this.body = body || {};
    this.path = path;
  }
}

/* ---------------------------------------------------------------------------
   Normalisers — one per shape quirk observed on the live 18.1.0-zeus instance.
   --------------------------------------------------------------------------- */

/* /recent  -> { results: { results: [...] } }
   /search  -> { results: [...] }
   /stats   -> { memories: { results: [...] } }
   Dig until we find the array. */
function asRecords(payload) {
  if (!payload) return [];
  let node = payload.results !== undefined ? payload.results : payload.memories;
  for (let depth = 0; depth < 3; depth++) {
    if (Array.isArray(node)) return node;
    if (node && Array.isArray(node.results)) return node.results;
    if (node && typeof node === 'object') node = node.results;
    else break;
  }
  return [];
}

/* A memory record from the vector store. Fields verified against live data. */
function readRecord(raw) {
  const meta = raw.metadata || {};
  return {
    id: raw.id || '',
    text: raw.memory || raw.text || '',
    category: meta.category || '',
    source: meta.source || '',
    factId: meta.fact_id != null ? meta.fact_id : null,
    createdAt: raw.created_at || '',
    updatedAt: raw.updated_at || '',
    userId: raw.user_id || '',
    score: raw.score != null ? raw.score : null,
    rerank: raw._rerank_score != null ? raw._rerank_score : null,
    mediaUrl: meta.media_url || null,
  };
}

/* A fact row from the SQLite ledger — the richest record type aiduMEM has. */
function readFact(raw) {
  return {
    id: raw.id,
    category: raw.category || '',
    key: raw.fact_key || '',
    value: raw.fact_value || '',
    summary: raw.summary || '',
    source: raw.source || '',
    confidence: raw.confidence != null ? raw.confidence : null,
    trust: raw.trust_score != null ? raw.trust_score : null,
    helpful: raw.helpful_count || 0,
    unhelpful: raw.unhelpful_count || 0,
    retrieved: raw.retrieval_count || 0,
    lastSeen: raw.last_accessed_at || '',
    createdAt: raw.created_at || '',
    archived: !!raw.archived,
    mediaUrl: raw.media_url || null,
  };
}

/* /knowledge/tree returns nested objects with a _count leaf on each node.
   Flatten the top level into { name, count, children[] } for rendering. */
function readKnowledgeTree(payload) {
  const tree = (payload && payload.tree) || {};
  const out = [];
  for (const [name, node] of Object.entries(tree)) {
    if (!node || typeof node !== 'object') continue;
    const children = [];
    let own = 0;
    for (const [ck, cv] of Object.entries(node)) {
      if (ck === '_count') { own = cv || 0; continue; }
      if (cv && typeof cv === 'object') {
        children.push({ name: ck, count: cv._count || 0 });
      }
    }
    const total = own || children.reduce((s, c) => s + c.count, 0);
    out.push({ name, count: total, children });
  }
  out.sort((a, b) => b.count - a.count);
  return { domains: (payload && payload.domains) || out.length,
           totalFacts: (payload && payload.total_facts) || 0,
           nodes: out };
}

/* /usage returns { usage: { "YYYY-MM-DD": { llm:{...}, embedding:{...}, rerank:{...} } } }.
   Days with no traffic are simply absent from the payload, so walking the keys
   would draw a chart where 07-09 sits next to 07-27 as if they were adjacent.
   Fill the calendar instead: `days` real days back from today, zeros included. */
function readUsage(payload, days) {
  const usage = (payload && payload.usage) || {};

  const pick = function (date) {
    const d = usage[date] || {};
    const llm = d.llm || {};
    const emb = d.embedding || {};
    const rer = d.rerank || d.reranker || {};
    const vis = d.vision || {};
    return {
      date: date,
      llmCalls: llm.calls || 0,
      llmTokens: llm.total_tokens || 0,
      embCalls: emb.calls || 0,
      embTokens: emb.total_tokens || 0,
      rerCalls: rer.calls || 0,
      rerTokens: rer.total_tokens || 0,
      visCalls: vis.calls || 0,
      visTokens: vis.total_tokens || 0,
    };
  };

  if (!days) return Object.keys(usage).sort().map(pick);

  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const key = d.getFullYear() + '-' +
      String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');
    out.push(pick(key));
  }
  return out;
}

/* Totals across the whole payload, for "since day one" tiles. */
function sumUsage(payload) {
  const rows = readUsage(payload, 0);
  return rows.reduce(function (a, r) {
    a.llmCalls += r.llmCalls; a.llmTokens += r.llmTokens;
    a.embCalls += r.embCalls; a.embTokens += r.embTokens;
    a.rerCalls += r.rerCalls; a.rerTokens += r.rerTokens;
    a.visCalls += r.visCalls; a.visTokens += r.visTokens;
    a.days += (r.llmCalls || r.embCalls || r.rerCalls || r.visCalls) ? 1 : 0;
    return a;
  }, { llmCalls: 0, llmTokens: 0, embCalls: 0, embTokens: 0, rerCalls: 0, rerTokens: 0, visCalls: 0, visTokens: 0, days: 0 });
}

/* ---------------------------------------------------------------------------
   small formatting helpers shared by every panel
   --------------------------------------------------------------------------- */

function fmtInt(n) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US');
}

/* 12345 -> 12.3k, 4200000 -> 4.2M — keeps stat tiles one line wide */
function fmtCompact(n) {
  if (n == null || isNaN(n)) return '—';
  n = Number(n);
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + 'k';
  return (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + 'M';
}

function fmtPct(x, digits) {
  if (x == null || isNaN(x)) return '—';
  return (Number(x) * 100).toFixed(digits == null ? 0 : digits) + '%';
}

/* "2026-07-27T03:55:02.587860+00:00" -> "07-27 11:55" in local time */
function fmtWhen(iso) {
  if (!iso) return '—';
  const t = new Date(iso.replace(' ', 'T'));
  if (isNaN(t.getTime())) return String(iso).slice(0, 16);
  const p = function (n) { return String(n).padStart(2, '0'); };
  return p(t.getMonth() + 1) + '-' + p(t.getDate()) + ' ' + p(t.getHours()) + ':' + p(t.getMinutes());
}

/* Text from the memory store is displayed verbatim, so escape it. */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function clip(s, n) {
  s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n) + '…' : s;
}

/* Memories were written by an agent that talks in Markdown, so raw values are
   full of **bold**, `code`, > quotes and --- rules. Stripping the syntax for
   the preview line is presentation, not editing: the stored text is untouched
   and the detail view will show it verbatim. */
function stripMd(s) {
  return String(s == null ? '' : s)
    .replace(/```[\s\S]*?```/g, ' ')          // fenced code blocks
    .replace(/`([^`]*)`/g, '$1')              // inline code
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1') // links and images -> label
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')       // headings
    .replace(/^\s{0,3}>\s?/gm, '')            // block quotes
    .replace(/^\s{0,3}([-*_]\s*){3,}$/gm, ' ') // horizontal rules
    .replace(/^\s{0,3}[-*+]\s+/gm, '')        // bullets
    .replace(/\*\*([^*]+)\*\*/g, '$1')        // bold
    .replace(/(^|\s)\*([^*\n]+)\*/g, '$1$2')  // italics
    .replace(/~~([^~]+)~~/g, '$1')            // strikethrough
    .replace(/\s+/g, ' ')
    .trim();
}
