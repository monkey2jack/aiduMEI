/* =============================================================================
   aiduMEI — panel renderers v2
   -----------------------------------------------------------------------------
   Six panels, all bilingual (EN/CN), all talking to live aiduMEM.
   - PULSE:   health + storage layers + usage + probes
   - VAULT:   search + edit/delete/feedback + categories + recent facts
   - MAP:     knowledge-tree star map (hand-drawn SVG)
   - RECALL:  search_trace funnel + ignition scores
   - EVOLVE:  quality report + adjustments + trust + crystals
   - SETTINGS:model config read/edit/test + modules + federation + params
   ============================================================================= */

const PANELS = {
  pulse:    { cn: '心动', en: 'PULSE',    hex: 'hexPulse',  say: '它现在还好吗',      render: renderPulse },
  vault:    { cn: '忆思', en: 'VAULT',    hex: 'hexVault',  say: '它记住了什么',      render: renderVault },
  map:      { cn: '心图', en: 'MAP',      hex: 'hexMap',    say: '它的记忆长什么样',  render: renderMap },
  recall:   { cn: '追忆', en: 'RECALL',   hex: 'hexRecall', say: '它是怎么想起来的',  render: renderRecall },
  evolve:   { cn: '成真', en: 'EVOLVE',   hex: 'hexEvolve', say: '它在变好还是变坏',  render: renderEvolve },
  settings: { cn: '设定', en: 'SETTINGS', hex: 'hexCrown',  say: '模型与连接',        render: renderSettings },
};

/* ===========================================================================
   shared helpers — bilingual headings, loading, failure
   =========================================================================== */

function secHead(cn, en, note) {
  return '<div class="sec-h"><h3>' + esc(cn) +
    (en ? ' <span class="en-label">' + esc(en) + '</span>' : '') + '</h3>' +
    (note ? '<span class="note">' + esc(note) + '</span>' : '') +
    '<span class="rule"></span></div>';
}

function loading(cn) {
  return '<div class="hint">' + esc(cn) + ' …</div>';
}

function failure(err) {
  const isTunnel = err && err.status === 502;
  const isBlocked = err && err.status === 403;
  let msg;
  if (isTunnel) {
    msg = '连不上 aiduMEM / Cannot reach aiduMEM';
  } else if (isBlocked) {
    msg = '接口不在白名单 / Blocked by allow-list: <code>' +
      esc((err.body && err.body.path) || '') + '</code>';
  } else {
    msg = '读取失败 / Error: ' + esc((err && err.message) || 'unknown');
  }
  return '<div class="hint bad">' + msg + '</div>';
}

function pill(label, value, tone) {
  tone = tone || '';
  return '<span class="pill ' + tone + '">' + esc(label) + ': <b>' + esc(value) + '</b></span>';
}

/* Grouped bar + line combo chart — calls as bars, tokens as lines. */
function barChart(rows, series) {
  /* series: [{ name, color, fn, type }] — type: 'bar' | 'line'
     bars show call counts, lines show token counts */
  const W = 900, H = 150;
  const padL = 34, padR = 34, padT = 10, padB = 26;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  if (!rows.length) return '<div class="hint">这段时间没有调用记录 / No data.</div>';

  const barSeries = series.filter(function (s) { return s.type === 'bar'; });
  const lineSeries = series.filter(function (s) { return s.type === 'line'; });
  const nBar = barSeries.length;

  // bar peak = max call count; line peak = max token count (two scales)
  var barPeak = 1;
  var linePeak = 1;
  rows.forEach(function (r) {
    barSeries.forEach(function (s) { barPeak = Math.max(barPeak, s.fn(r)); });
    lineSeries.forEach(function (s) { linePeak = Math.max(linePeak, s.fn(r)); });
  });

  const slot = innerW / rows.length;
  const barW = Math.max(2, slot * (0.72 / Math.max(nBar, 1)));
  const gap = barW * 0.28;

  var bars = '';
  var lines = '';
  var dots = '';
  var labels = '';

  rows.forEach(function (r, i) {
    const cx = padL + slot * (i + 0.5);
    // bars (calls) — bottom group
    barSeries.forEach(function (s, si) {
      const v = s.fn(r);
      const h = Math.max(v > 0 ? 1.5 : 0, (v / barPeak) * innerH);
      const x = cx - (barW * nBar + gap * (nBar - 1)) / 2 + si * (barW + gap);
      bars += '<rect class="cbar" fill="' + esc(s.color) + '" opacity="0.75" x="' + x.toFixed(1) + '" y="' + (padT + innerH - h).toFixed(1) +
        '" width="' + barW.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="1.2">' +
        '<title>' + esc(r.date) + ' ' + esc(s.name) + ' ' + fmtInt(v) + ' 调用</title></rect>';
    });

    // lines (tokens) — continuous across days
    lineSeries.forEach(function (s) {
      const v = s.fn(r);
      if (v > 0) {
        const x = cx;
        const y = padT + innerH - (v / linePeak) * innerH;
        dots += '<circle class="cdot" cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="2" fill="' + esc(s.color) + '">' +
          '<title>' + esc(r.date) + ' ' + esc(s.name) + ' ' + fmtInt(v) + ' tokens</title></circle>';
      }
    });

    const step = rows.length > 16 ? 3 : rows.length > 9 ? 2 : 1;
    if (i % step === 0 || i === rows.length - 1) {
      labels += '<text class="clabel" x="' + cx.toFixed(1) + '" y="' + (H - 10) +
        '" text-anchor="middle">' + esc(r.date.slice(5)) + '</text>';
    }
  });

  // draw line paths (connect dots per series)
  lineSeries.forEach(function (s) {
    var pts = [];
    rows.forEach(function (r, i) {
      const v = s.fn(r);
      const cx = padL + slot * (i + 0.5);
      if (v > 0) {
        pts.push([cx, padT + innerH - (v / linePeak) * innerH]);
      }
    });
    if (pts.length > 1) {
      var d = pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
      for (var j = 1; j < pts.length; j++) {
        d += ' L' + pts[j][0].toFixed(1) + ',' + pts[j][1].toFixed(1);
      }
      lines += '<path class="cline" d="M' + d + '" fill="none" stroke="' + esc(s.color) + '" stroke-width="1.2" stroke-dasharray="4,2" opacity="0.7"/>';
    }
  });

  var legend = series.map(function (s) {
    if (s.type === 'bar') {
      return '<span><i style="background:' + esc(s.color) + ';width:8px;height:8px;border-radius:2px"></i>' + esc(s.name) + '</span>';
    } else {
      return '<span><i style="background:none;border-top:2px dashed ' + esc(s.color) + ';width:12px;height:0"></i>' + esc(s.name) + '</span>';
    }
  }).join('');

  return '<div class="chart">' +
    '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(series[0].name) + '">' +
    '<line class="cgrid" x1="' + padL + '" y1="' + (padT + innerH) + '" x2="' + (W - padR) + '" y2="' + (padT + innerH) + '"/>' +
    bars + lines + dots + labels +
    '</svg>' +
    '<div class="chart-legend">' +
      legend +
      '<span style="margin-left:auto">峰值 peak: ' + fmtCompact(barPeak) + ' calls / ' + fmtCompact(linePeak) + ' tokens</span>' +
    '</div></div>';
}

/* One row of the layer chart. */
function layerRow(cn, en, n, peak, tone, note) {
  const pct = peak > 0 ? Math.max(n > 0 ? 0.7 : 0, (n / peak) * 100) : 0;
  return '<div class="layer"' + (tone ? ' data-tone="' + tone + '"' : '') + '>' +
    '<div class="lname">' + esc(cn) + (en ? '<span>' + esc(en) + '</span>' : '') + '</div>' +
    '<div class="lbar"><div class="lfill" style="width:' + pct.toFixed(2) + '%"></div></div>' +
    '<div class="lnum">' + fmtInt(n) + (note ? '<small>' + esc(note) + '</small>' : '') + '</div>' +
    '</div>';
}

/* ===========================================================================
   PULSE — is it alright right now
   =========================================================================== */
async function renderPulse(body) {
  body.innerHTML = loading('服务状态 / Service status');

  let health, stats, factsHead, tree, usage, recent;
  try {
    [health, stats, factsHead, tree, usage, recent] = await Promise.all([
      API.get('/health'),
      API.get('/stats'),
      API.get('/facts', { limit: 1 }),
      API.get('/knowledge/tree'),
      API.get('/usage'),
      API.get('/recent', { limit: 4 }).catch(function () { return null; }),
    ]);
  } catch (e) {
    body.innerHTML = failure(e);
    return;
  }

  const probes = health.probes || {};
  const modules = health.modules || {};
  const modOn = Object.values(modules).filter(Boolean).length;
  const modAll = Object.keys(modules).length;
  const degraded = (health.degraded || []).length;

  const nFacts = factsHead.count || 0;
  const nFts = probes.fts_memories || 0;
  const nVec = stats.total_memories || 0;
  const nRaw = probes.raw_drawer_count || 0;
  const peak = Math.max(nFacts, nFts, nVec, nRaw) || 1;

  const kTree = readKnowledgeTree(tree);
  const usageRows = readUsage(usage, 14);
  const total = sumUsage(usage);
  const today = usageRows[usageRows.length - 1];
  const busy = usageRows.filter(function (r) { return r.llmCalls > 0; }).length;

  const recs = recent ? asRecords(recent).map(readRecord).slice(0, 4) : [];

  body.innerHTML =
    '<div class="sec">' + secHead('服务状态', 'SERVICE STATUS', health.service || '') +
      '<div class="tiles">' +
        '<div class="tile"><div class="k">健康 Health</div><div class="v">' +
          (health.health_status === 'ok' ? '正常 OK' : esc(health.health_status || '—')) +
          '</div><div class="u">' + (degraded ? degraded + ' 项降级 / degraded' : '没有降级项 / no degradation') + '</div></div>' +
        '<div class="tile"><div class="k">版本 Version</div><div class="v" style="font-size:19px">' +
          esc(health.version || '—') + '</div><div class="u">代号 ' + esc(health.codename || '—') + '</div></div>' +
        '<div class="tile"><div class="k">核心模块 Modules</div><div class="v">' + modOn +
          '<small>/ ' + modAll + '</small></div><div class="u">全部在线 / all online</div></div>' +
        '<div class="tile"><div class="k">知识域 Domains</div><div class="v">' + kTree.domains +
          '</div><div class="u">分类维度总数 / categories</div></div>' +
      '</div>' +
    '</div>' +

    '<div class="sec">' + secHead('它记住了多少', 'STORAGE LAYERS', '同一套记忆分四层存放') +
      '<div class="layers">' +
        layerRow('事实账本', 'FACTS', nFacts, peak, '', '结构化事实') +
        layerRow('全文索引', 'FULL-TEXT', nFts, peak, 'soft', '可关键词命中') +
        layerRow('向量记忆', 'VECTOR', nVec, peak, 'accent', '可语义召回') +
        layerRow('原味抽屉', 'RAW DRAWER', nRaw, peak, 'soft', '原文零改写') +
      '</div>' +
    '</div>' +

    '<div class="sec">' + secHead('最近 14 天用量', 'USAGE · 14 DAYS', busy + ' 天有活动') +
      '<div class="tiles" style="margin-bottom:12px">' +
        '<div class="tile"><div class="k">今日 LLM</div><div class="v">' + fmtCompact(today.llmTokens) +
          '<small>tokens</small></div><div class="u">' + fmtInt(today.llmCalls) + ' 次调用 / calls</div></div>' +
        '<div class="tile"><div class="k">今日向量化 Embedding</div><div class="v">' + fmtCompact(today.embTokens) +
          '<small>tokens</small></div><div class="u">' + fmtInt(today.embCalls) + ' 次调用 / calls</div></div>' +
        '<div class="tile"><div class="k">今日重排 Reranker</div><div class="v">' + fmtCompact(today.rerTokens) +
          '<small>tokens</small></div><div class="u">' + fmtInt(today.rerCalls) + ' 次调用 / calls</div></div>' +
        '<div class="tile"><div class="k">累计 LLM</div><div class="v">' + fmtCompact(total.llmTokens) +
          '<small>tokens</small></div><div class="u">' + fmtInt(total.llmCalls) + ' 次调用 / calls</div></div>' +
        '<div class="tile"><div class="k">累计向量化 Embedding</div><div class="v">' + fmtCompact(total.embTokens) +
          '<small>tokens</small></div><div class="u">' + fmtInt(total.embCalls) + ' 次调用 / calls</div></div>' +
        '<div class="tile"><div class="k">累计重排 Reranker</div><div class="v">' + fmtCompact(total.rerTokens) +
          '<small>tokens</small></div><div class="u">' + fmtInt(total.rerCalls) + ' 次调用 / calls</div></div>' +
      '</div>' +
      barChart(usageRows, [
        { name: 'LLM 调用', color: '#1f4e79', fn: function (r) { return r.llmCalls; }, type: 'bar' },
        { name: '向量化 Embedding 调用', color: '#525252', fn: function (r) { return r.embCalls; }, type: 'bar' },
        { name: '重排 Reranker 调用', color: '#7030a0', fn: function (r) { return r.rerCalls; }, type: 'bar' },
        { name: 'LLM tokens', color: '#1f4e79', fn: function (r) { return r.llmTokens; }, type: 'line' },
        { name: 'Embedding tokens', color: '#7c7c7c', fn: function (r) { return r.embTokens; }, type: 'line' },
        { name: 'Reranker tokens', color: '#b388c9', fn: function (r) { return r.rerTokens; }, type: 'line' },
      ]) +
    '</div>' +

    (recs.length
      ? '<div class="sec">' + secHead('最近记住的', 'RECENT MEMORIES', '向量层最新 ' + recs.length + ' 条') +
          '<div class="recs">' + recs.map(recordRow).join('') + '</div>' +
        '</div>'
      : '') +

    '<div class="sec">' + secHead('探针', 'PROBES', '后端自检项') +
      '<div class="probes">' + probeCells(probes) + '</div>' +
    '</div>';
}

function probeCells(probes) {
  const labels = {
    facts_db: '事实库 Facts', text_fts_db: '全文库 Full-text', mem0_singleton: '记忆引擎 Mem0',
    port_service: '端口服务 Port', fts_ok: '全文索引 FTS', entity_keywords_ok: '实体词表 Entities',
    raw_drawer_ok: '原味抽屉 Raw Drawer', code_graph_ok: '代码图谱 Code Graph', evolve_mem_ok: '自进化 EvolveMem',
  };
  const counts = { fts_memories: '全文条数 FTS', entity_keywords: '实体词 Entities', raw_drawer_count: '原味条数 Raw' };
  let out = '';
  for (const [k, label] of Object.entries(labels)) {
    if (probes[k] === undefined) continue;
    out += '<div class="probe" data-ok="' + (probes[k] ? 1 : 0) + '"><i></i>' + esc(label) +
      '<b>' + (probes[k] ? 'OK' : 'FAIL') + '</b></div>';
  }
  for (const [k, label] of Object.entries(counts)) {
    if (probes[k] === undefined) continue;
    out += '<div class="probe" data-ok="1"><i></i>' + esc(label) + '<b>' + fmtInt(probes[k]) + '</b></div>';
  }
  return out;
}

/* ===========================================================================
   VAULT — what it remembers (search + edit/delete/feedback)
   =========================================================================== */
async function renderVault(body) {
  body.innerHTML =
    '<div class="sec">' + secHead('搜一条记忆', 'SEARCH MEMORY', '语义检索 + 重排') +
      '<div class="searchrow">' +
        '<input class="sinput" id="vaultQ" type="search" placeholder="比如 / e.g.：aduBOX 是什么" autocomplete="off" />' +
        '<button class="sbtn" id="vaultGo">搜索 Search</button>' +
      '</div>' +
      '<div id="vaultResults"></div>' +
    '</div>' +
    '<div class="sec" id="vaultCats">' + secHead('分类家底', 'CATEGORIES', '') + loading('分类') + '</div>' +
    '<div class="sec" id="vaultRecent">' + secHead('最近写入的事实', 'RECENT FACTS', '') + loading('事实账本') + '</div>';

  const q = body.querySelector('#vaultQ');
  const go = body.querySelector('#vaultGo');
  const out = body.querySelector('#vaultResults');

  const doSearch = async function () {
    const text = q.value.trim();
    if (!text) return;
    out.innerHTML = loading('检索结果 / results');
    try {
      const r = await API.post('/search', { query: text, limit: 8 });
      const recs = asRecords(r).map(readRecord);
      if (!recs.length) {
        out.innerHTML = '<div class="hint">没有命中 / No results. 换个说法再试 —— 它是按意思找，不是按字面找。</div>';
        return;
      }
      out.innerHTML = '<div class="recs">' + recs.map(recordRow).join('') + '</div>';
      attachRecordActions(out, text);
    } catch (e) {
      out.innerHTML = failure(e);
    }
  };

  go.addEventListener('click', doSearch);
  q.addEventListener('keydown', function (e) { if (e.key === 'Enter') doSearch(); });

  // categories
  try {
    const cats = await API.get('/facts/categories');
    const list = (cats.categories || []).slice().sort(function (a, b) { return b.cnt - a.cnt; });
    body.querySelector('#vaultCats').innerHTML =
      secHead('分类家底', 'CATEGORIES', list.length + ' 个分类') +
      '<div class="chips">' + list.slice(0, 24).map(function (c) {
        return '<span class="chip">' + esc(c.category) + '<b>' + fmtInt(c.cnt) + '</b></span>';
      }).join('') + '</div>';
  } catch (e) {
    body.querySelector('#vaultCats').innerHTML = secHead('分类家底', 'CATEGORIES', '') + failure(e);
  }

  // recent facts
  try {
    const f = await API.get('/facts', { limit: 6 });
    const facts = (f.facts || []).map(readFact);
    body.querySelector('#vaultRecent').innerHTML =
      secHead('最近写入的事实', 'RECENT FACTS', '账本共 ' + fmtInt(f.count || 0) + ' 条') +
      '<div class="recs">' + facts.map(factRow).join('') + '</div>';
  } catch (e) {
    body.querySelector('#vaultRecent').innerHTML = secHead('最近写入的事实', 'RECENT FACTS', '') + failure(e);
  }
}

/* attach action buttons to search result cards */
function attachRecordActions(container, query) {
  container.querySelectorAll('.rec').forEach(function (card) {
    const id = card.dataset.id;
    if (!id) return;
    const actions = document.createElement('div');
    actions.className = 'racts';
    actions.innerHTML =
      '<button class="ract useful" data-id="' + esc(id) + '">有用 Useful</button>' +
      '<button class="ract useless" data-id="' + esc(id) + '">没用 Useless</button>' +
      '<button class="ract edit" data-id="' + esc(id) + '">编辑 Edit</button>' +
      '<button class="ract del" data-id="' + esc(id) + '">删除 Delete</button>';

    actions.querySelector('.useful').addEventListener('click', function () {
      sendFeedback(this.dataset.id, 'useful', query, card);
    });
    actions.querySelector('.useless').addEventListener('click', function () {
      sendFeedback(this.dataset.id, 'useless', query, card);
    });
    actions.querySelector('.edit').addEventListener('click', function () {
      editMemory(this.dataset.id, card);
    });
    actions.querySelector('.del').addEventListener('click', function () {
      deleteMemory(this.dataset.id, card);
    });

    card.appendChild(actions);
  });
}

async function sendFeedback(id, signal, query, card) {
  try {
    await API.post('/evolve/feedback', { memory_id: id, signal: signal, query: query });
    card.style.opacity = '0.5';
    card.querySelector('.racts').innerHTML = '<span class="hint">已反馈 / Feedback sent</span>';
  } catch (e) {
    card.querySelector('.racts').innerHTML = '<span class="hint bad">' + esc(e.message || '失败') + '</span>';
  }
}

function editMemory(id, card) {
  const text = card.querySelector('.rtext').textContent;
  const editor = document.createElement('div');
  editor.className = 'editor';
  editor.innerHTML =
    '<textarea class="etext">' + esc(text) + '</textarea>' +
    '<div class="ebar">' +
      '<button class="ebtn save">保存 Save</button>' +
      '<button class="ebtn cancel">取消 Cancel</button>' +
    '</div>';
  card.appendChild(editor);
  card.querySelector('.racts').style.display = 'none';

  editor.querySelector('.save').addEventListener('click', async function () {
    const newText = editor.querySelector('.etext').value.trim();
    if (!newText) return;
    try {
      await API.post('/update', { memory_id: id, content: newText });
      card.querySelector('.rtext').textContent = newText;
      editor.remove();
      card.querySelector('.racts').style.display = '';
    } catch (e) {
      editor.querySelector('.ebar').innerHTML = '<span class="hint bad">' + esc(e.message || '更新失败') + '</span>';
    }
  });
  editor.querySelector('.cancel').addEventListener('click', function () {
    editor.remove();
    card.querySelector('.racts').style.display = '';
  });
}

async function deleteMemory(id, card) {
  if (!confirm('确认删除这条记忆？\nDelete this memory?')) return;
  try {
    await API.post('/delete', { memory_id: id });
    card.style.opacity = '0.3';
    card.querySelector('.racts').innerHTML = '<span class="hint">已删除 / Deleted</span>';
  } catch (e) {
    alert('删除失败 / Delete failed: ' + (e.message || ''));
  }
}

function recordRow(r) {
  const bits = [];
  if (r.category) bits.push('<span class="cat">' + esc(r.category) + '</span>');
  if (r.rerank != null) bits.push('重排 ' + r.rerank.toFixed(3));
  else if (r.score != null) bits.push('相似 ' + r.score.toFixed(3));
  if (r.createdAt) bits.push(fmtWhen(r.createdAt));
  if (r.factId != null) bits.push('#' + r.factId);
  return '<div class="rec" data-id="' + esc(r.id || '') + '"><div class="rtext">' +
    esc(clip(stripMd(r.text), 260)) + '</div>' +
    '<div class="rmeta">' + bits.join('<span style="opacity:.4">·</span>') + '</div></div>';
}

function factRow(f) {
  const bits = [];
  if (f.category) bits.push('<span class="cat">' + esc(f.category) + '</span>');
  if (f.trust != null) bits.push('信任 ' + f.trust.toFixed(2));
  if (f.retrieved) bits.push('被想起 ' + f.retrieved + ' 次');
  if (f.helpful || f.unhelpful) bits.push('有用 ' + f.helpful + ' / 没用 ' + f.unhelpful);
  if (f.createdAt) bits.push(fmtWhen(f.createdAt));
  return '<div class="rec"><div class="rtext">' + esc(clip(stripMd(f.summary || f.value), 260)) + '</div>' +
    '<div class="rmeta">' + bits.join('<span style="opacity:.4">·</span>') + '</div></div>';
}

/* ===========================================================================
   MAP — knowledge-tree star map (ECharts force-directed graph)
   =========================================================================== */
async function renderMap(body) {
  body.innerHTML = loading('知识树 / Knowledge tree');

  let tree, entities, scenes;
  try {
    [tree, entities, scenes] = await Promise.all([
      API.get('/knowledge/tree'),
      API.get('/facts/entities/list').catch(function () { return null; }),
      API.get('/scene').catch(function () { return null; }),
    ]);
  } catch (e) {
    body.innerHTML = failure(e);
    return;
  }

  const k = readKnowledgeTree(tree);
  const domains = k.nodes;

  if (!domains.length) {
    body.innerHTML = '<div class="hint">知识树为空 / Knowledge tree is empty</div>';
    return;
  }

  body.innerHTML =
    '<div class="sec">' + secHead('知识域星图', 'STAR MAP', k.domains + ' 个域 / ' + fmtInt(k.totalFacts) + ' 条事实') +
      '<div id="starMapChart" class="starmap-echarts"></div>' +
      '<div class="hint" style="padding-top:6px">滚轮缩放 / 拖拽节点 / 悬停查看详情<br>' +
        '<span class="en-label">Scroll to zoom · Drag nodes · Hover for details</span></div>' +
    '</div>' +
    '<div class="sec">' + secHead('域详情', 'DOMAIN DETAILS', '') +
      '<div id="domainList"></div>' +
    '</div>';

  drawEChartsStarMap(body.querySelector('#starMapChart'), domains, entities, scenes);
  drawDomainList(body.querySelector('#domainList'), domains);
}

/* ECharts force-directed graph — interactive, zoomable, draggable */
function drawEChartsStarMap(container, domains, entitiesPayload, scenesPayload) {
  var chart = echarts.init(container);
  var maxCount = Math.max.apply(null, domains.map(function (d) { return d.count; })) || 1;

  var nodes = [];
  var links = [];
  var categories = [{ name: '核心 Core' }, { name: '知识域 Domain' }, { name: '分类 Category' }, { name: '实体 Entity' }];

  // center node — big, bright, white
  nodes.push({
    id: 'core',
    name: 'aiduMEM',
    symbolSize: 48,
    category: 0,
    itemStyle: { color: '#d0e8ff', shadowBlur: 30, shadowColor: 'rgba(92,179,255,0.6)' },
    label: { show: true, color: '#d0e8ff', fontWeight: 'bold', fontSize: 14 }
  });

  // domain nodes — glowing blue
  domains.slice(0, 15).forEach(function (d, i) {
    var nid = 'd' + i;
    var size = 20 + 34 * (d.count / maxCount);
    nodes.push({
      id: nid,
      name: d.name + '\n(' + d.count + ')',
      symbolSize: size,
      category: 1,
      itemStyle: { color: '#4a9eff', shadowBlur: 12, shadowColor: 'rgba(74,158,255,0.5)', opacity: 0.9 },
      label: { show: true, color: '#b8d4ee', fontSize: 11, fontWeight: 600 },
      value: d.count
    });
    links.push({ source: 'core', target: nid, lineStyle: { color: '#4a9eff', opacity: 0.3, width: 1.2 } });

    // category children — smaller gray
    d.children.slice(0, 5).forEach(function (c, j) {
      var cid = nid + '_c' + j;
      var csize = 8 + 14 * (c.count / maxCount);
      nodes.push({
        id: cid,
        name: c.name + '(' + c.count + ')',
        symbolSize: csize,
        category: 2,
        itemStyle: { color: '#6b8fab', opacity: 0.5 },
        label: { show: csize > 14, color: '#8aabb8', fontSize: 9 },
        value: c.count
      });
      links.push({ source: nid, target: cid, lineStyle: { color: '#4a6a8a', opacity: 0.2, width: 0.8 } });
    });
  });

  // entity nodes — purple glow
  var ents = (entitiesPayload && entitiesPayload.entities) || [];
  ents.slice(0, 20).forEach(function (e, i) {
    var eid = 'e' + i;
    var fc = e.fact_count || e.count || 1;
    var esize = 7 + 12 * Math.min(fc / 10, 2.5);
    nodes.push({
      id: eid,
      name: e.name || e.entity || '?',
      symbolSize: esize,
      category: 3,
      itemStyle: { color: '#a06eec', shadowBlur: 8, shadowColor: 'rgba(160,110,236,0.4)', opacity: 0.7 },
      label: { show: esize > 14, color: '#c9a2f2', fontSize: 9 },
      value: fc
    });
    if (domains.length) links.push({ source: 'd0', target: eid, lineStyle: { color: '#a06eec', opacity: 0.12, width: 0.7 } });
  });

  var option = {
    backgroundColor: '#0a0e14',
    tooltip: {
      backgroundColor: 'rgba(18,26,38,0.94)',
      borderColor: '#3a5a7a',
      borderWidth: 1,
      textStyle: { color: '#d0e8ff', fontSize: 12 },
      formatter: function (p) {
        if (p.dataType === 'node') {
          return '<b>' + esc(p.data.name) + '</b><br>条数 Count: ' + (p.data.value || '—');
        }
        return '';
      }
    },
    legend: {
      data: categories.map(function (c) { return c.name; }),
      top: 10, left: 10,
      textStyle: { fontSize: 10, color: '#7c8ba0' },
      itemWidth: 10, itemHeight: 10,
      inactiveColor: 'rgba(124,139,160,0.25)'
    },
    series: [{
      type: 'graph',
      layout: 'force',
      data: nodes,
      links: links,
      categories: categories,
      roam: true,
      zoom: 1.2,
      force: {
        repulsion: 180,
        edgeLength: [50, 150],
        gravity: 0.08,
        layoutAnimation: true
      },
      lineStyle: { curveness: 0.18 },
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 2.5, opacity: 0.7 },
        label: { show: true, fontSize: 13, color: '#ffffff', textShadowBlur: 8, textShadowColor: 'rgba(74,158,255,0.8)' }
      },
      label: { show: true, position: 'inside' },
      progressive: 200,
      animationDuration: 900,
      animationEasingUpdate: 'cubicOut'
    }]
  };

  chart.setOption(option);

  // resize on window resize
  var resizeFn = function () { chart.resize(); };
  window.addEventListener('resize', resizeFn);
  // cleanup when panel closes
  container._chart = chart;
  container._resizeFn = resizeFn;
}

function drawDomainList(container, domains) {
  const peak = domains.length ? domains[0].count : 1;
  container.innerHTML = '<div class="layers">' + domains.slice(0, 18).map(function (n) {
    return layerRow(n.name, n.children.length ? n.children.length + ' 子域' : '', n.count, peak, '', '');
  }).join('') + '</div>';
}

/* ===========================================================================
   RECALL — how it came to mind (search_trace funnel)
   =========================================================================== */
async function renderRecall(body) {
  body.innerHTML =
    '<div class="sec">' + secHead('看它想一遍', 'RECALL TRACE', '输入一句话，看完整召回过程') +
      '<div class="searchrow">' +
        '<input class="sinput" id="rcQ" type="search" placeholder="比如 / e.g.：助手的生日礼物" autocomplete="off" />' +
        '<button class="sbtn" id="rcGo">追踪 Trace</button>' +
      '</div>' +
      '<div class="hint" style="padding-top:10px">这一屏是 aiduMEI 最想做好的地方：' +
        '别家的记忆面板只给你"存了什么"，这里给你"它凭什么想起这条"。<br>' +
        '<span class="en-label">This panel shows WHY each memory was recalled, not just WHAT was stored.</span></div>' +
      '<div id="rcOut"></div>' +
    '</div>';

  const q = body.querySelector('#rcQ');
  const out = body.querySelector('#rcOut');

  const trace = async function () {
    const text = q.value.trim();
    if (!text) return;
    out.innerHTML = loading('召回链路 / trace');
    try {
      const r = await API.post('/search_trace', { query: text, limit: 6 });
      out.innerHTML = renderTrace(r);
    } catch (e) {
      out.innerHTML = failure(e);
    }
  };

  body.querySelector('#rcGo').addEventListener('click', trace);
  q.addEventListener('keydown', function (e) { if (e.key === 'Enter') trace(); });
}

const TRACE_STAGES = {
  candidate_pool: { cn: '候选池',      tip: '先按向量相似度粗捞一批 / vector candidates' },
  ignition:       { cn: '点火',        tip: '分数高过阈值的强命中直接放行 / strong hits pass' },
  dedup:          { cn: '去重',        tip: '把内容重复的合并掉 / deduplicate' },
  time_decay:     { cn: '时间衰减',    tip: '越旧的记忆往后排 / older memories sink' },
  rerank:         { cn: '重排',        tip: '用 reranker 重新打分排序 / rerank' },
  final:          { cn: '最终入选',    tip: '交回给 Agent 的就是这几条 / final output' },
};

function renderTrace(payload) {
  const recs = asRecords(payload).map(readRecord);
  const raw = payload.results || [];
  const trace = payload.trace || null;

  if (!trace || !Array.isArray(trace.stages) || !trace.stages.length) {
    return (recs.length ? '<div class="recs">' + recs.map(recordRow).join('') + '</div>' : '') +
      '<div class="hint">这次后端没有返回分阶段链路 / No stage trace returned.</div>';
  }

  const stages = trace.stages;
  const flowOf = function (s) { return s.remaining != null ? s.remaining : (s.count || 0); };
  const widest = Math.max.apply(null, stages.map(flowOf)) || 1;

  const rows = stages.map(function (s, i) {
    const meta = TRACE_STAGES[s.name] || { cn: s.name, tip: '' };
    const flow = flowOf(s);
    const pct = Math.max(flow > 0 ? 4 : 0, (flow / widest) * 100);
    const dropped = i > 0 ? flowOf(stages[i - 1]) - flow : 0;

    const notes = [];
    if (meta.tip) notes.push(meta.tip);
    if (s.name === 'ignition' && s.threshold != null) {
      notes.push('阈值 ' + s.threshold + (s.ignited ? '，点火 ' + s.ignited + ' 条' : '，无点火'));
    } else if (s.count && s.name !== 'final' && s.name !== 'candidate_pool' && s.name !== 'time_decay') {
      notes.push('处理 ' + s.count + ' 条');
    }
    if (dropped > 0) notes.push('淘汰 ' + dropped + ' 条');

    return '<div class="fstage">' +
      '<div class="fname">' + esc(meta.cn) + '<span>' + esc(s.name) + '</span></div>' +
      '<div class="ftrack"><div class="fbar" style="width:' + pct.toFixed(1) + '%"></div></div>' +
      '<div class="fnum">' + flow + '<small>' + (s.ms ? s.ms + ' ms' : '&lt;1 ms') + '</small></div>' +
      '<div class="fnote">' + esc(notes.join(' · ')) + '</div>' +
      '</div>';
  }).join('');

  const cards = recs.map(function (r, i) {
    const extra = raw[i] || {};
    const bits = [];
    if (extra._ignition_score != null) bits.push('点火分 ' + Number(extra._ignition_score).toFixed(3));
    if (extra._ignited) bits.push('已点火');
    return recordRow(r).replace('</div></div>',
      (bits.length ? '<span style="opacity:.4">·</span>' + esc(bits.join(' · ')) : '') + '</div></div>');
  }).join('');

  return '<div class="sec">' +
      secHead('召回漏斗', 'RECALL FUNNEL', '总耗时 ' + (trace.total_ms || 0) + ' ms / 最终 ' + (trace.final_count || recs.length) + ' 条') +
      '<div class="funnel">' + rows + '</div>' +
      (trace.has_ignition === false
        ? '<div class="hint" style="padding-bottom:0">这次没有"点火"命中 / No ignition this time.</div>' : '') +
    '</div>' +
    '<div class="sec">' + secHead('它最后选了这几条', 'FINAL RESULTS', '按最终顺序') +
      (cards ? '<div class="recs">' + cards + '</div>'
             : '<div class="hint">这次没有命中任何记忆 / No matches.</div>') +
    '</div>';
}

/* ===========================================================================
   EVOLVE — is it getting better
   =========================================================================== */
async function renderEvolve(body) {
  body.innerHTML = loading('进化报告 / Evolution report');

  let report, trust, crystals;
  try {
    [report, trust, crystals] = await Promise.all([
      API.get('/evolve/report'),
      API.get('/facts/trust-stats'),
      API.get('/crystals').catch(function () { return null; }),
    ]);
  } catch (e) {
    body.innerHTML = failure(e);
    return;
  }

  const s7 = report.last_7d_search || {};
  const adj = report.last_7d_adjustments || [];
  const cats = (trust.categories || []).slice(0, 12);
  const peakCnt = cats.length ? Math.max.apply(null, cats.map(function (c) { return c.cnt; })) : 1;
  const quiet = !s7.total_queries;

  body.innerHTML =
    '<div class="sec">' + secHead('最近 7 天检索质量', 'SEARCH QUALITY · 7 DAYS',
      report.last_cycle_human ? '上次进化 ' + esc(report.last_cycle_human) : '') +
      '<div class="tiles">' +
        '<div class="tile"><div class="k">检索次数 Queries</div><div class="v">' + fmtInt(s7.total_queries || 0) + '</div>' +
          '<div class="u">' + (quiet ? '这一周没走过 /search' : '有效样本 / valid') + '</div></div>' +
        '<div class="tile"><div class="k">平均命中 Avg Hits</div><div class="v">' + (s7.avg_hits || 0).toFixed(1) + '</div>' +
          '<div class="u">每次召回条数 / per query</div></div>' +
        '<div class="tile"><div class="k">平均得分 Avg Score</div><div class="v">' + (s7.avg_score || 0).toFixed(2) + '</div>' +
          '<div class="u">相关性 / relevance</div></div>' +
        '<div class="tile"><div class="k">零命中 Zero-hit</div><div class="v">' + fmtInt(s7.zero_hit_queries || 0) + '</div>' +
          '<div class="u">一条都没找到 / found nothing</div></div>' +
      '</div>' +
      (quiet ? '<div class="hint">系统在跑进化周期，但这一周没有搜索样本 / No search samples this week.</div>' : '') +
    '</div>' +

    '<div class="sec">' + secHead('权重调整动作', 'WEIGHT ADJUSTMENTS', '自进化在偷偷做的事') +
      (adj.length
        ? '<div class="probes">' + adj.map(function (a) {
            const up = (a.avg_delta || 0) >= 0;
            return '<div class="probe" data-ok="1"><i></i>' +
              esc(a.action === 'salience_boost' ? '提权 salience_boost' :
                  a.action === 'salience_decay' ? '衰减 salience_decay' : a.action) +
              '<b>' + fmtInt(a.count) + ' 次 ' + (up ? '+' : '') + (a.avg_delta || 0).toFixed(4) + '</b></div>';
          }).join('') + '</div>'
        : '<div class="hint">这一周没有权重调整 / No adjustments this week.</div>') +
    '</div>' +

    '<div class="sec">' + secHead('各分类信任度', 'TRUST BY CATEGORY', '满分 1.00') +
      '<div class="layers">' + cats.map(function (c) {
        const pct = ((c.avg_trust || 0) * 100).toFixed(1);
        return '<div class="layer">' +
          '<div class="lname">' + esc(c.category) + '<span>' + fmtInt(c.cnt) + ' 条</span></div>' +
          '<div class="lbar"><div class="lfill" style="width:' + pct + '%"></div></div>' +
          '<div class="lnum" style="font-size:13px">' + (c.avg_trust || 0).toFixed(2) +
            '<small>' + (c.helpful || 0) + '↑ ' + (c.unhelpful || 0) + '↓</small></div>' +
          '</div>';
      }).join('') + '</div>' +
    '</div>' +

    '<div class="sec">' + secHead('结晶候选', 'CRYSTALS', crystals ? '' : '接口未响应') +
      (crystals && (crystals.crystals || crystals.items || []).length
        ? '<div class="recs">' + (crystals.crystals || crystals.items).slice(0, 5).map(function (c) {
            return '<div class="rec"><div class="rtext">' + esc(clip(c.pattern || c.text || JSON.stringify(c), 200)) + '</div></div>';
          }).join('') + '</div>'
        : '<div class="hint">目前没有待结晶的模式 / No crystal candidates. 结晶是把反复出现的事实压成"技能"，数据量上来之后才会有。</div>') +
    '</div>';
}

/* ===========================================================================
   SETTINGS — model config + modules + federation + params
   =========================================================================== */
async function renderSettings(body) {
  body.innerHTML = loading('配置 / Configuration');

  let health, agents, config;
  try {
    [health, agents, config] = await Promise.all([
      API.get('/health'),
      API.get('/federation/agents').catch(function () { return null; }),
      API.get('/config').catch(function () { return null; }),
    ]);
  } catch (e) {
    body.innerHTML = failure(e);
    return;
  }

  const modules = health.modules || {};
  const agentList = (agents && agents.agents) || [];
  const cfg = config || {};

  body.innerHTML =
    // ---- model config ----
    '<div class="sec">' + secHead('模型配置', 'MODEL CONFIG', 'LLM / Embedding / Reranker') +
      '<div id="cfgModels">' + renderModelConfig(cfg) + '</div>' +
      '<div class="cfg-actions">' +
        '<button class="cfg-btn" id="cfgEditModels">编辑配置 Edit Config</button>' +
        '<button class="cfg-btn" id="cfgReload">热重载 Reload</button>' +
        '<button class="cfg-btn" id="cfgTest">测试连接 Test Connection</button>' +
      '</div>' +
    '</div>' +

    // ---- reasoning / thinking mode ----
    '<div class="sec">' + secHead('思考模式', 'REASONING MODE', '深度推理 / deep thinking') +
      '<div id="cfgReasoning">' + renderReasoning(cfg) + '</div>' +
    '</div>' +

    // ---- tunable parameters ----
    '<div class="sec">' + secHead('可调参数', 'TUNABLE PARAMS', 'salience / coalesce / capacity') +
      '<div id="cfgParams">' + renderParams(cfg) + '</div>' +
    '</div>' +

    // ---- modules ----
    '<div class="sec">' + secHead('核心模块', 'CORE MODULES', Object.keys(modules).length + ' 项') +
      '<div class="probes">' + Object.entries(modules).map(function (kv) {
        return '<div class="probe" data-ok="' + (kv[1] ? 1 : 0) + '"><i></i>' + esc(kv[0]) +
          '<b>' + (kv[1] ? 'ON' : 'OFF') + '</b></div>';
      }).join('') + '</div>' +
    '</div>' +

    // ---- federation ----
    '<div class="sec">' + secHead('联邦成员', 'FEDERATION', agentList.length + ' 个 Agent') +
      (agentList.length
        ? '<div class="recs">' + agentList.map(function (a) {
            const on = a.available && !a.stale;
            return '<div class="rec"><div class="rtext"><b>' + esc(a.display_name || a.agent_id) + '</b>' +
              (a.description ? ' — ' + esc(a.description) : '') + '</div>' +
              '<div class="rmeta">' +
                '<span class="cat">' + (on ? '在线 Online' : '静默 Idle') + '</span>' +
                '<span style="opacity:.4">·</span>' + fmtInt(a.fact_count || 0) + ' 条事实 / facts' +
                '<span style="opacity:.4">·</span>心跳 ' + fmtWhen(a.last_seen_at) +
                '<span style="opacity:.4">·</span>profile ' + esc(a.profile || '—') +
              '</div></div>';
          }).join('') + '</div>'
        : '<div class="hint">读不到联邦成员 / No federation agents.</div>') +
    '</div>';

  // wire up config actions
  var editBtn = body.querySelector('#cfgEditModels');
  var reloadBtn = body.querySelector('#cfgReload');
  var testBtn = body.querySelector('#cfgTest');

  if (editBtn) editBtn.addEventListener('click', function () { editModelConfig(body, cfg); });
  if (reloadBtn) reloadBtn.addEventListener('click', function () { reloadConfig(body); });
  if (testBtn) testBtn.addEventListener('click', function () { testConnection(body); });

  // wire up param edit buttons
  body.querySelectorAll('.param-edit').forEach(function (btn) {
    btn.addEventListener('click', function () {
      editParam(body, btn.dataset.key, btn);
    });
  });
}

function editModelConfig(body, cfg) {
  var existing = body.querySelector('#cfgModels');
  var llm = cfg.llm || {};
  var emb = cfg.embedder || {};
  var rer = cfg.rerank || {};

  var form = '<div class="editor" style="margin-top:10px">' +
    '<div class="mc-row"><span class="mc-k">LLM Model</span><input class="sinput mc-input" id="edLlmModel" value="' + esc((llm.config && llm.config.model) || '') + '"></div>' +
    '<div class="mc-row"><span class="mc-k">LLM Base URL</span><input class="sinput mc-input" id="edLlmUrl" value="' + esc((llm.config && llm.config.openai_base_url) || '') + '"></div>' +
    '<div class="mc-row"><span class="mc-k">LLM API Key</span><input class="sinput mc-input" id="edLlmKey" type="password" placeholder="留空不修改 / blank=no change"></div>' +
    '<div class="mc-row"><span class="mc-k">Embed Model</span><input class="sinput mc-input" id="edEmbModel" value="' + esc((emb.config && emb.config.model) || '') + '"></div>' +
    '<div class="mc-row"><span class="mc-k">Embed Base URL</span><input class="sinput mc-input" id="edEmbUrl" value="' + esc((emb.config && emb.config.openai_base_url) || '') + '"></div>' +
    '<div class="mc-row"><span class="mc-k">Embed API Key</span><input class="sinput mc-input" id="edEmbKey" type="password" placeholder="留空不修改"></div>' +
    '<div class="mc-row"><span class="mc-k">Rerank Model</span><input class="sinput mc-input" id="edRerModel" value="' + esc((rer.config && rer.config.model) || '') + '" placeholder="可选 optional"></div>' +
    '<div class="mc-row"><span class="mc-k">Rerank Base URL</span><input class="sinput mc-input" id="edRerUrl" value="' + esc((rer.config && rer.config.openai_base_url) || '') + '" placeholder="可选 optional"></div>' +
    '<div class="mc-row"><span class="mc-k">Rerank API Key</span><input class="sinput mc-input" id="edRerKey" type="password" placeholder="可选，留空不修改"></div>' +
    '<div class="mc-row"><span class="mc-k">Rerank Enabled</span><input type="checkbox" id="edRerEnabled" ' + ((rer && rer.enabled) ? 'checked' : '') + '></div>' +
    '<div class="hint" style="padding:6px 0">Reranker 为可选项，留空或取消勾选即不启用。<br><span class="en-label">Reranker is optional. Leave blank or uncheck to disable.</span></div>' +
    '<div class="ebar">' +
      '<button class="ebtn save" id="edSave">保存 Save</button>' +
      '<button class="ebtn cancel" id="edCancel">取消 Cancel</button>' +
    '</div>' +
    '<div class="hint" style="padding-top:6px">保存后会调用 <code>PUT /config/{section}</code>，再 <code>POST /reload</code>。<br>' +
      '<span class="en-label">Saves via PUT /config/{section} then POST /reload.</span></div>' +
  '</div>';

  existing.insertAdjacentHTML('afterend', form);

  body.querySelector('#edSave').addEventListener('click', async function () {
    var patch = {};
    var lm = body.querySelector('#edLlmModel').value.trim();
    var lu = body.querySelector('#edLlmUrl').value.trim();
    var lk = body.querySelector('#edLlmKey').value.trim();
    if (lm || lu || lk) {
      patch.llm = { provider: 'openai', config: {} };
      if (lm) patch.llm.config.model = lm;
      if (lu) patch.llm.config.openai_base_url = lu;
      if (lk) patch.llm.config.api_key = lk;
    }
    var em = body.querySelector('#edEmbModel').value.trim();
    var eu = body.querySelector('#edEmbUrl').value.trim();
    var ek = body.querySelector('#edEmbKey').value.trim();
    if (em || eu || ek) {
      patch.embedder = { provider: 'openai', config: {} };
      if (em) patch.embedder.config.model = em;
      if (eu) patch.embedder.config.openai_base_url = eu;
      if (ek) patch.embedder.config.api_key = ek;
    }
    var rm = body.querySelector('#edRerModel').value.trim();
    var ru = body.querySelector('#edRerUrl').value.trim();
    var rk = body.querySelector('#edRerKey').value.trim();
    var ren = body.querySelector('#edRerEnabled').checked;
    if (rm || ru || rk || ren) {
      patch.rerank = { enabled: ren, config: {} };
      if (rm) patch.rerank.config.model = rm;
      if (ru) patch.rerank.config.openai_base_url = ru;
      if (rk) patch.rerank.config.api_key = rk;
    }
    if (!Object.keys(patch).length) { alert('没有改动 / No changes'); return; }
    try {
      for (var section in patch) {
        var r = await fetch('/api/config/' + section, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch[section])
        });
        if (!r.ok) {
          var errBody = await r.json().catch(function () { return {}; });
          throw new Error((errBody.detail || errBody.message || section) + ' (' + r.status + ')');
        }
      }
      await API.post('/reload', {});
      alert('配置已保存并热重载 / Saved and reloaded');
      openPanel('settings');
    } catch (e) {
      alert('保存失败 / Save failed: ' + (e.message || ''));
    }
  });

  body.querySelector('#edCancel').addEventListener('click', function () {
    var ed = body.querySelector('.editor');
    if (ed) ed.remove();
  });
}

async function reloadConfig(body) {
  try {
    var r = await API.post('/reload', {});
    alert('热重载成功 / Reload OK: ' + JSON.stringify(r));
  } catch (e) {
    alert('热重载失败 / Reload failed: ' + (e.message || ''));
  }
}

async function testConnection(body) {
  try {
    var r = await API.get('/health');
    alert('连接正常 / Connection OK\n' +
      '版本 Version: ' + (r.version || '—') + '\n' +
      '状态 Status: ' + (r.health_status || '—'));
  } catch (e) {
    alert('连接失败 / Connection failed: ' + (e.message || ''));
  }
}

function editParam(body, key, btn) {
  var row = btn.parentElement;
  var valSpan = row.querySelector('.param-v');
  var oldVal = valSpan.textContent;
  var input = document.createElement('input');
  input.className = 'sinput mc-input';
  input.value = oldVal;
  input.style.width = '120px';
  valSpan.replaceWith(input);
  btn.textContent = '保存 Save';
  btn.classList.add('save');

  btn.removeEventListener('click', arguments.callee);
  btn.addEventListener('click', async function () {
    var newVal = input.value.trim();
    try {
      await fetch('/api/config/_speed', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: key, value: newVal })
      });
      var span = document.createElement('span');
      span.className = 'param-v';
      span.textContent = newVal;
      input.replaceWith(span);
      btn.textContent = '改 Edit';
      btn.classList.remove('save');
    } catch (e) {
      alert('保存失败 / Save failed: ' + (e.message || ''));
    }
  });
}

function renderModelConfig(cfg) {
  const llm = cfg.llm || {};
  const emb = cfg.embedder || {};
  const rer = cfg.rerank || {};

  const card = function (title, en, m) {
    const provider = m.provider || '—';
    const model = m.model || m.config && m.config.model || '—';
    const baseUrl = m.base_url || m.config && m.config.openai_base_url || '—';
    const key = m.api_key || m.config && m.config.api_key || '';
    const keyMasked = key ? key.slice(0, 3) + '***' + key.slice(-4) : '—';
    return '<div class="tile mcard">' +
      '<div class="k">' + esc(title) + ' <span class="en-label">' + esc(en) + '</span></div>' +
      '<div class="mc-row"><span class="mc-k">Provider</span><b>' + esc(provider) + '</b></div>' +
      '<div class="mc-row"><span class="mc-k">Model</span><b>' + esc(model) + '</b></div>' +
      '<div class="mc-row"><span class="mc-k">Base URL</span><code>' + esc(baseUrl) + '</code></div>' +
      '<div class="mc-row"><span class="mc-k">API Key</span><code>' + esc(keyMasked) + '</code></div>' +
      '</div>';
  };

  return '<div class="tiles">' +
    card('语言模型', 'LLM', llm) +
    card('向量模型', 'EMBEDDING', emb) +
    card('重排模型', 'RERANKER', rer) +
  '</div>' +
  '<div class="hint" style="padding-top:8px">配置只读展示。修改需通过 <code>PUT /config/{key}</code> 或编辑 <code>mem0_config_local.json</code> 后 <code>POST /reload</code>。<br>' +
    '<span class="en-label">Read-only display. Edit mem0_config_local.json then POST /reload to apply.</span></div>';
}

function renderReasoning(cfg) {
  var llm = cfg.llm || {};
  var lc = llm.config || {};
  var note = lc._note || '';
  return '<div class="reasoning-block">' +
    '<div class="param-row">' +
      '<span class="param-k">深度思考 Deep Thinking ' +
        '<span class="en-label">is_reasoning_model</span></span>' +
      '<span class="pill bad" style="border-color:var(--bad);color:var(--bad)">已关闭 OFF</span>' +
    '</div>' +
    '<div class="hint" style="padding:6px 0">' + esc(note || 'LLM关闭思考模式用于记忆提取 / reasoning disabled for memory extraction') + '</div>' +
    '<div class="hint" style="padding-bottom:0">思考模式在配置文件中写死为关闭。LLM 用于记忆提取时需要快速直答，不需要深度推理。<br>' +
      '<span class="en-label">Reasoning is hardcoded OFF in config. Memory extraction needs fast direct answers, not deep reasoning.</span></div>' +
  '</div>';
}

function renderParams(cfg) {
  const speed = cfg._speed || {};
  const params = [
    { key: 'max_tokens', cn: 'LLM 最大 tokens', en: 'LLM max tokens', val: speed.max_tokens },
    { key: 'coalesce_enabled', cn: '合并缓冲', en: 'Coalesce buffer', val: speed.coalesce_enabled },
    { key: 'coalesce_window_sec', cn: '合并窗口(秒)', en: 'Coalesce window (s)', val: speed.coalesce_window_sec },
    { key: 'coalesce_idle_sec', cn: '合并空闲(秒)', en: 'Coalesce idle (s)', val: speed.coalesce_idle_sec },
    { key: 'coalesce_max_parts', cn: '合并最大段', en: 'Coalesce max parts', val: speed.coalesce_max_parts },
    { key: 'coalesce_max_chars', cn: '合并最大字符', en: 'Coalesce max chars', val: speed.coalesce_max_chars },
    { key: 'extract_cache_ttl_sec', cn: '提取缓存TTL(秒)', en: 'Extract cache TTL (s)', val: speed.extract_cache_ttl_sec },
    { key: 'extract_cache_max', cn: '提取缓存上限', en: 'Extract cache max', val: speed.extract_cache_max },
    { key: 'long_text_chars', cn: '长文本阈值', en: 'Long text threshold', val: speed.long_text_chars },
    { key: 'capacity_merge_async', cn: '异步合并', en: 'Async merge', val: speed.capacity_merge_async },
    { key: 'fastpath_enabled', cn: '快速通道', en: 'Fast path', val: speed.fastpath_enabled },
  ];

  const rows = params.filter(function (p) { return p.val !== undefined && p.val !== null; }).map(function (p) {
    return '<div class="param-row">' +
      '<span class="param-k">' + esc(p.cn) + ' <span class="en-label">' + esc(p.en) + '</span></span>' +
      '<span class="param-v">' + esc(String(p.val)) + '</span>' +
      '<button class="param-edit" data-key="' + esc(p.key) + '">改 Edit</button>' +
      '</div>';
  }).join('');

  return rows || '<div class="hint">无法读取参数 / Cannot read params</div>';
}
