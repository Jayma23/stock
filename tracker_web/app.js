const state = {
  scans: [],
  currentScan: null,
  currentRows: [],
  currentNewRows: [],
  previousScanLabel: null,
  filteredRows: [],
  selectedIndex: -1,
  statusOptions: [],
};

const elements = {
  scanSelect: document.querySelector("#scan-select"),
  refreshButton: document.querySelector("#refresh-button"),
  scanMeta: document.querySelector("#scan-meta"),
  stockList: document.querySelector("#stock-list"),
  listCount: document.querySelector("#list-count"),
  searchInput: document.querySelector("#search-input"),
  newcomerMeta: document.querySelector("#newcomer-meta"),
  newcomerCount: document.querySelector("#newcomer-count"),
  newcomerList: document.querySelector("#newcomer-list"),
  selectionHint: document.querySelector("#selection-hint"),
  selectedSymbol: document.querySelector("#selected-symbol"),
  selectedName: document.querySelector("#selected-name"),
  breakoutDate: document.querySelector("#metric-breakout-date"),
  metricDays: document.querySelector("#metric-days"),
  metricClose: document.querySelector("#metric-close"),
  metricPremium: document.querySelector("#metric-premium"),
  tvChartHost: document.querySelector("#tv-chart-host"),
  tvOpenLink: document.querySelector("#tv-open-link"),
  historyList: document.querySelector("#history-list"),
  statusSelect: document.querySelector("#status-select"),
  noteTextarea: document.querySelector("#note-textarea"),
  saveNoteButton: document.querySelector("#save-note-button"),
  noteSavedAt: document.querySelector("#note-saved-at"),
};

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "--";
  }
  return Number(value).toFixed(digits);
}

function currentRow() {
  return state.filteredRows[state.selectedIndex] ?? null;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`);
  }
  return response.json();
}

async function loadScans() {
  const payload = await fetchJson("/api/scans");
  state.scans = payload.scans;
  state.statusOptions = payload.status_options;
  renderScanOptions();
  populateStatusOptions();

  if (!state.scans.length) {
    elements.scanMeta.textContent = "没有找到归档结果，请先运行扫描脚本。";
    return;
  }

  const selected = state.currentScan ?? state.scans[0].file;
  elements.scanSelect.value = selected;
  await loadScan(selected);
}

function renderScanOptions() {
  elements.scanSelect.innerHTML = "";
  state.scans.forEach((scan) => {
    const option = document.createElement("option");
    option.value = scan.file;
    option.textContent = `${scan.label} (${scan.count})`;
    elements.scanSelect.appendChild(option);
  });
}

function populateStatusOptions() {
  elements.statusSelect.innerHTML = "";
  state.statusOptions.forEach((status) => {
    const option = document.createElement("option");
    option.value = status;
    option.textContent = status;
    elements.statusSelect.appendChild(option);
  });
}

async function loadScan(file) {
  const payload = await fetchJson(`/api/scan?file=${encodeURIComponent(file)}`);
  state.currentScan = payload.file;
  state.currentRows = payload.rows;
  state.currentNewRows = payload.newcomers ?? [];
  state.previousScanLabel = payload.previous_label ?? null;
  state.filteredRows = [...payload.rows];
  state.selectedIndex = state.filteredRows.length ? 0 : -1;
  elements.scanMeta.textContent = `${payload.label}，共 ${payload.count} 只。`;
  renderNewcomers();
  applyFilter();
}

function applyFilter() {
  const keyword = elements.searchInput.value.trim().toUpperCase();
  if (!keyword) {
    state.filteredRows = [...state.currentRows];
  } else {
    state.filteredRows = state.currentRows.filter((row) => {
      return (
        row.symbol.toUpperCase().includes(keyword) ||
        row.exchange.toUpperCase().includes(keyword) ||
        row.tv_symbol.toUpperCase().includes(keyword)
      );
    });
  }

  if (!state.filteredRows.length) {
    state.selectedIndex = -1;
  } else if (state.selectedIndex < 0 || state.selectedIndex >= state.filteredRows.length) {
    state.selectedIndex = 0;
  }

  renderList();
  renderSelection();
}

function renderList() {
  elements.stockList.innerHTML = "";
  elements.listCount.textContent = String(state.filteredRows.length);

  if (!state.filteredRows.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "没有匹配到股票，可以换个关键词。";
    elements.stockList.appendChild(empty);
    return;
  }

  state.filteredRows.forEach((row, index) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `stock-item${index === state.selectedIndex ? " selected" : ""}`;
    item.innerHTML = `
      <div class="stock-item-top">
        <strong>${row.symbol}</strong>
        <span class="pill">${row.sessions_since_breakout}天</span>
      </div>
      <div class="stock-item-bottom">
        <span>${row.exchange}</span>
        <span>${formatNumber(row.latest_premium_pct)}%</span>
      </div>
    `;
    item.addEventListener("click", () => selectIndex(index));
    elements.stockList.appendChild(item);
  });
}

function renderNewcomers() {
  elements.newcomerList.innerHTML = "";
  elements.newcomerCount.textContent = String(state.currentNewRows.length);

  if (!state.currentRows.length) {
    elements.newcomerMeta.textContent = "当前归档还没有股票数据。";
    elements.newcomerList.innerHTML = '<div class="empty-state">暂无新晋股票。</div>';
    return;
  }

  if (!state.previousScanLabel) {
    elements.newcomerMeta.textContent = "这是目前最早的一期归档，暂时没有可对比的上一期。";
    elements.newcomerList.innerHTML = '<div class="empty-state">暂无可比较的新晋股票。</div>';
    return;
  }

  elements.newcomerMeta.textContent = `相对上一期 ${state.previousScanLabel} 新进入本期名单。`;

  if (!state.currentNewRows.length) {
    elements.newcomerList.innerHTML = '<div class="empty-state">这一期没有新增进入名单的股票。</div>';
    return;
  }

  state.currentNewRows.forEach((row) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "newcomer-item";
    item.innerHTML = `
      <div class="stock-item-top">
        <strong>${row.symbol}</strong>
        <span class="pill new-pill">新晋</span>
      </div>
      <div class="stock-item-bottom">
        <span>${row.exchange}</span>
        <span>${formatNumber(row.latest_premium_pct)}%</span>
      </div>
    `;
    item.addEventListener("click", () => selectSymbol(row.symbol));
    elements.newcomerList.appendChild(item);
  });
}

function renderSelection() {
  const row = currentRow();
  if (!row) {
    elements.selectionHint.textContent = "未选择";
    elements.selectedSymbol.textContent = "--";
    elements.selectedName.textContent = "请先选择一只股票";
    elements.breakoutDate.textContent = "--";
    elements.metricDays.textContent = "--";
    elements.metricClose.textContent = "--";
    elements.metricPremium.textContent = "--";
    elements.tvOpenLink.href = "#";
    elements.tvChartHost.innerHTML = '<div class="empty-state">请选择股票</div>';
    elements.historyList.innerHTML = '<div class="empty-state">暂无历史记录</div>';
    elements.noteTextarea.value = "";
    elements.noteSavedAt.textContent = "";
    return;
  }

  elements.selectionHint.textContent = `${state.selectedIndex + 1} / ${state.filteredRows.length}`;
  elements.selectedSymbol.textContent = row.symbol;
  elements.selectedName.textContent = `${row.exchange} · ${row.tv_symbol}`;
  elements.breakoutDate.textContent = row.breakout_date;
  elements.metricDays.textContent = `${row.sessions_since_breakout} 天`;
  elements.metricClose.textContent = formatNumber(row.latest_close, 2);
  elements.metricPremium.textContent = `${formatNumber(row.latest_premium_pct, 2)}%`;
  elements.tvOpenLink.href = `https://www.tradingview.com/symbols/${row.tv_symbol.replace(":", "-")}/`;
  renderTradingView(row.tv_symbol);
  loadSymbolContext(row.symbol);
}

async function loadSymbolContext(symbol) {
  const payload = await fetchJson(`/api/symbol?symbol=${encodeURIComponent(symbol)}`);
  renderHistory(payload.history);
  elements.statusSelect.value = payload.notes.status || "观察";
  elements.noteTextarea.value = payload.notes.note || "";
  elements.noteSavedAt.textContent = payload.notes.updated_at ? `上次保存: ${payload.notes.updated_at}` : "";
}

function renderHistory(items) {
  elements.historyList.innerHTML = "";
  if (!items.length) {
    elements.historyList.innerHTML = '<div class="empty-state">这只股票还没有历史归档记录。</div>';
    return;
  }

  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "history-item";
    card.innerHTML = `
      <div class="history-item-header">
        <span class="label">${item.label}</span>
        <span class="pill">${item.sessions_since_breakout}天</span>
      </div>
      <div class="meta">突破日期: ${item.breakout_date}</div>
      <div class="meta">最新收盘: ${formatNumber(item.latest_close, 2)} · 高于250日线: ${formatNumber(item.latest_premium_pct, 2)}%</div>
    `;
    elements.historyList.appendChild(card);
  });
}

function renderTradingView(tvSymbol) {
  elements.tvChartHost.innerHTML = "";
  const frame = document.createElement("iframe");
  frame.className = "tv-chart-frame";
  frame.loading = "eager";
  frame.referrerPolicy = "no-referrer-when-downgrade";
  frame.src = `/chart?symbol=${encodeURIComponent(tvSymbol)}&file=${encodeURIComponent(state.currentScan ?? "")}`;
  elements.tvChartHost.appendChild(frame);
}

function selectIndex(index) {
  if (index < 0 || index >= state.filteredRows.length) {
    return;
  }
  state.selectedIndex = index;
  renderList();
  renderSelection();
  const selected = elements.stockList.children[index];
  selected?.scrollIntoView({ block: "nearest" });
}

function selectSymbol(symbol) {
  const index = state.filteredRows.findIndex((row) => row.symbol === symbol);
  if (index >= 0) {
    selectIndex(index);
    return;
  }

  const fallbackIndex = state.currentRows.findIndex((row) => row.symbol === symbol);
  if (fallbackIndex >= 0) {
    elements.searchInput.value = "";
    applyFilter();
    const nextIndex = state.filteredRows.findIndex((row) => row.symbol === symbol);
    if (nextIndex >= 0) {
      selectIndex(nextIndex);
    }
  }
}

function handleArrowKey(event) {
  const activeTag = document.activeElement?.tagName ?? "";
  if (activeTag === "TEXTAREA" || activeTag === "INPUT" || activeTag === "SELECT") {
    return;
  }

  if (event.key === "ArrowDown") {
    event.preventDefault();
    selectIndex(Math.min(state.selectedIndex + 1, state.filteredRows.length - 1));
  }

  if (event.key === "ArrowUp") {
    event.preventDefault();
    selectIndex(Math.max(state.selectedIndex - 1, 0));
  }
}

async function saveNote() {
  const row = currentRow();
  if (!row) {
    return;
  }
  const payload = await fetchJson("/api/notes", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      symbol: row.symbol,
      status: elements.statusSelect.value,
      note: elements.noteTextarea.value,
    }),
  });
  elements.noteSavedAt.textContent = `上次保存: ${payload.updated_at}`;
}

elements.scanSelect.addEventListener("change", async (event) => {
  await loadScan(event.target.value);
});

elements.refreshButton.addEventListener("click", async () => {
  await loadScans();
});

elements.searchInput.addEventListener("input", () => {
  applyFilter();
});

elements.saveNoteButton.addEventListener("click", async () => {
  await saveNote();
});

window.addEventListener("keydown", handleArrowKey);

loadScans().catch((error) => {
  console.error(error);
  elements.scanMeta.textContent = `加载失败: ${error.message}`;
});
