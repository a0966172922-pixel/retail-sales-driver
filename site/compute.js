// Retail Sales Driver 計算核心
// 所有序列都是季調名目銷售額（百萬美元）。Census 的季調彙總本來就是細項直接相加（1992 年以來誤差為 0），
// 所以任何彙總都用最細項目相加，貢獻度也可以直接相加：
//   貢獻（百分點）= (項目本期 − 項目基期) ÷ 整體零售基期 × 100
//   基期：月增率為上個月，年增率為去年同月。所有項目的貢獻加總 = 整體零售的變動率。

function sumSeries(arrs, T) {
  const out = new Float64Array(T);
  for (const a of arrs) for (let t = 0; t < T; t++) out[t] += a[t];
  return out;
}

function lagOf(mode) {
  return mode === "yoy" ? 12 : 1;
}

// 自身變動率（%）
function pctChange(series, t, lag) {
  if (t - lag < 0) return null;
  return (series[t] / series[t - lag] - 1) * 100;
}

// 對 base（整體零售）變動率的貢獻（百分點）
function contribution(series, base, t, lag) {
  if (t - lag < 0) return null;
  return (series[t] - series[t - lag]) / base[t - lag] * 100;
}

if (typeof module !== "undefined") module.exports = { sumSeries, lagOf, pctChange, contribution };
