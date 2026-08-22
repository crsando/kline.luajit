const state = { dates: [], date: null, instruments: [], instrument: null, data: null, chart: null };
const $ = (id) => document.getElementById(id);
const api = async (path) => {
  const response = await fetch(path);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

function renderSummary(data) {
  const q = data.quality;
  $("summary").innerHTML = [
    `<span>${escapeHtml(data.instrument)} · ${escapeHtml(data.tradingDay)}</span>`,
    `<span>Ticks <b>${q.validTicks.toLocaleString()}</b></span>`,
    `<span>1min <b>${data.bars.length}</b></span>`,
    `<span>Morning <b>${q.morningTicks.toLocaleString()}</b></span>`,
    `<span>Afternoon <b>${q.afternoonTicks.toLocaleString()}</b></span>`,
    `<span>Volume reset <b>${q.volumeResets}</b></span>`,
    `<span>VWAP <b>snapshot estimate</b></span>`,
  ].join("");
}

function renderProfileStats(data) {
  const poc = data.zones.find((zone) => zone.type === "VPOC");
  const val = data.zones.find((zone) => zone.type === "VAL");
  const vah = data.zones.find((zone) => zone.type === "VAH");
  $("profileStats").innerHTML = [poc, val, vah].map((zone) => zone ? (
    `<div class="profile-stat"><span>${zone.type}</span><strong>${zone.center}</strong></div>`
  ) : "").join("");
}

function roleLabel(role) {
  return role === "support" ? "support" : role === "resistance" ? "resistance" : "neutral";
}

function renderZones(data) {
  $("zones").innerHTML = data.zones.map((zone) => (
    `<article class="zone role-${escapeHtml(zone.role)}"><div class="zone-head"><span class="zone-type">${escapeHtml(zone.type)}</span><span class="zone-price">${zone.lower}–${zone.upper}</span></div><div class="zone-meta">${roleLabel(zone.role)}${zone.firstTouchTime ? ` · ${escapeHtml(zone.firstTouchTime)}` : ""}</div></article>`
  )).join("");
}

function candleRender(params, chartApi) {
  const values = [0, 1, 2, 3, 4].map((dimension) => Number(chartApi.value(dimension)));
  if (values.some((value) => !Number.isFinite(value))) return null;
  const xPoint = chartApi.coord([values[0], values[1]]);
  const openPoint = chartApi.coord([values[0], values[1]]);
  const closePoint = chartApi.coord([values[0], values[2]]);
  const lowPoint = chartApi.coord([values[0], values[3]]);
  const highPoint = chartApi.coord([values[0], values[4]]);
  if (![xPoint, openPoint, closePoint, lowPoint, highPoint].every((point) => (
    Array.isArray(point) && point.length >= 2 && point.every(Number.isFinite)
  ))) return null;
  const size = chartApi.size([1, 0]);
  const width = Math.max(2, Math.min(9, (Array.isArray(size) && Number.isFinite(size[0]) ? size[0] : 6) * .58));
  const color = values[2] >= values[1] ? "#d9544d" : "#218a63";
  return { type: "group", children: [
    { type: "line", shape: { x1: xPoint[0], y1: highPoint[1], x2: xPoint[0], y2: lowPoint[1] }, style: { stroke: color, lineWidth: 1 } },
    { type: "rect", shape: { x: xPoint[0] - width / 2, y: Math.min(openPoint[1], closePoint[1]), width, height: Math.max(1, Math.abs(closePoint[1] - openPoint[1])) }, style: { fill: color, stroke: color } },
  ] };
}

function zoneRender(params, chartApi) {
  const zone = state.data.zones[params.dataIndex];
  if (!zone) return null;
  // ECharts custom renderItem params does not expose the raw data item.
  const startX = Number(chartApi.value(0));
  if (!Number.isFinite(startX)) return null;
  const p0 = chartApi.coord([startX, zone.upper]);
  const p1 = chartApi.coord([state.data.bars.length - 1, zone.lower]);
  if (![p0, p1].every((point) => (
    Array.isArray(point) && point.length >= 2 && point.every(Number.isFinite)
  ))) return null;
  const rect = window.echarts.graphic.clipRectByRect({ x: p0[0], y: p0[1], width: p1[0] - p0[0], height: p1[1] - p0[1] }, params.coordSys);
  if (!rect) return null;
  const color = zone.role === "support" ? "#14866d" : zone.role === "resistance" ? "#c94b3c" : "#b57418";
  return { type: "rect", shape: rect, style: { fill: color, opacity: .12, stroke: color, lineWidth: 1, lineDash: zone.role === "neutral" ? [5, 4] : null } };
}

function profileRender(params, chartApi) {
  const volume = Number(chartApi.value(0));
  const price = Number(chartApi.value(1));
  if (!Number.isFinite(volume) || !Number.isFinite(price)) return null;
  const p0 = chartApi.coord([0, price]);
  const p1 = chartApi.coord([volume, price]);
  if (![p0, p1].every((point) => (
    Array.isArray(point) && point.length >= 2 && point.every(Number.isFinite)
  ))) return null;
  const size = chartApi.size([0, state.data.tickSize]);
  const height = Math.max(1, Math.min(8, (Array.isArray(size) && Number.isFinite(size[1]) ? Math.abs(size[1]) : 6) * .72));
  return { type: "rect", shape: { x: p0[0], y: p0[1] - height / 2, width: Math.max(0, p1[0] - p0[0]), height }, style: { fill: "#6d7f99", opacity: .5 } };
}

function renderChart(data) {
  state.data = data;
  state.chart?.dispose();
  state.chart = window.echarts.init($("chart"), null, { renderer: "canvas" });
  const bars = data.bars;
  const maxX = Math.max(1, bars.length - 1);
  const afternoonIndex = Math.max(0, bars.findIndex((bar) => bar.session === "afternoon"));
  const startX = Math.max(0, afternoonIndex - .5);
  const barTime = (value) => bars[Math.max(0, Math.min(bars.length - 1, Math.round(value)))]?.time.slice(11, 16) || "";
  const tickData = data.ticks.map((tick) => [tick.x, tick.price, tick.time, tick.deltaVolume]);
  const vwapData = data.vwap.map((point) => [point.x, point.price]);
  const option = {
    animation: false,
    textStyle: { color: "#667085", fontFamily: "-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" },
    grid: [
      { left: 58, right: 205, top: 24, height: "58%" },
      { right: 18, width: 150, top: 24, height: "58%" },
      { left: 58, right: 205, top: "70%", bottom: 42 },
    ],
    xAxis: [
      { type: "value", min: -.7, max: maxX + .7, axisLabel: { formatter: barTime }, splitLine: { show: true } },
      { type: "value", gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      { type: "value", min: -.7, max: maxX + .7, gridIndex: 2, axisLabel: { formatter: barTime }, splitLine: { show: false } },
    ],
    yAxis: [
      { type: "value", min: data.priceMin, max: data.priceMax, scale: true, splitLine: { show: true } },
      { type: "value", min: data.priceMin, max: data.priceMax, scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      { type: "value", gridIndex: 2, splitLine: { show: true } },
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 2], filterMode: "none" }, { type: "slider", xAxisIndex: [0, 2], bottom: 7, height: 18 }],
    tooltip: { trigger: "axis", confine: true, formatter: (params) => {
      const tick = params.find((item) => item.seriesId === "ticks");
      if (tick) return `${tick.data[2]}<br>Tick ${tick.data[1]}<br>ΔV ${tick.data[3]}`;
      const index = Math.max(0, Math.min(bars.length - 1, Math.round(params[0]?.value?.[0] ?? 0)));
      const bar = bars[index];
      return bar ? `${bar.time}<br>O ${bar.open} H ${bar.high} L ${bar.low} C ${bar.close}<br>Volume ${bar.volume}` : "";
    } },
    series: [
      { id: "zones", name: "Zones", type: "custom", xAxisIndex: 0, yAxisIndex: 0, renderItem: zoneRender, data: data.zones.map(() => [startX]), silent: true, z: 0 },
      { id: "candles", name: "1min", type: "custom", xAxisIndex: 0, yAxisIndex: 0, renderItem: candleRender, data: bars.map((bar) => [bar.index, bar.open, bar.close, bar.low, bar.high]), z: 3 },
      { id: "ticks", name: "Tick", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: tickData, showSymbol: false, lineStyle: { color: "#8b95a5", width: .7, opacity: .55 }, z: 4 },
      { id: "vwap", name: "session VWAP", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: vwapData, showSymbol: false, lineStyle: { color: "#285fa8", width: 1.6 }, z: 5 },
      { id: "touches", name: "First touch", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: data.zones.filter((zone) => zone.firstTouchX !== null).map((zone) => ({ value: [zone.firstTouchX, zone.center], itemStyle: { color: zone.role === "support" ? "#14866d" : "#c94b3c" } })), symbolSize: 9, z: 7 },
      { id: "volume", name: "Volume", type: "bar", xAxisIndex: 2, yAxisIndex: 2, data: bars.map((bar) => [bar.index, bar.volume]), barWidth: "55%", itemStyle: { color: "#285fa8", opacity: .45 }, z: 2 },
      { id: "profile", name: "Profile", type: "custom", xAxisIndex: 1, yAxisIndex: 1, renderItem: profileRender, data: data.profile.map((bin) => [bin.volume, bin.price]), silent: true, z: 2 },
    ],
  };
  state.chart.setOption(option);
  state.chart.on("datazoom", () => state.chart?.resize());
  window.onresize = () => state.chart?.resize();
}

async function loadChart() {
  if (!state.date || !state.instrument) return;
  setStatus("计算中");
  try {
    const data = await api(`/api/days/${state.date}/instruments/${encodeURIComponent(state.instrument)}/chart`);
    renderSummary(data);
    renderProfileStats(data);
    renderZones(data);
    renderChart(data);
    setStatus("就绪");
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function loadInstruments() {
  state.instruments = await api(`/api/days/${state.date}/instruments`);
  const select = $("instrumentSelect");
  select.innerHTML = state.instruments.map((item) => `<option value="${escapeHtml(item.instrument)}">${escapeHtml(item.instrument)}</option>`).join("");
  state.instrument = state.instruments[0]?.instrument ?? null;
  if (state.instrument) await loadChart();
}

async function boot() {
  try {
    state.dates = await api("/api/days");
    $("dateSelect").innerHTML = state.dates.map((date) => `<option value="${date}">${date}</option>`).join("");
    state.date = state.dates[0] ?? null;
    if (state.date) await loadInstruments();
    else setStatus("没有找到数据", true);
  } catch (error) {
    setStatus(error.message, true);
  }
}

$("dateSelect").onchange = async (event) => { state.date = event.target.value; await loadInstruments(); };
$("instrumentSelect").onchange = async (event) => { state.instrument = event.target.value; await loadChart(); };
$("reloadButton").onclick = loadChart;
boot();
