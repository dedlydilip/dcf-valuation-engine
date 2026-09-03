"""Dashboard generation module for multi-company valuation.

Renders both a rich terminal summary table and an interactive standalone HTML
dashboard equipped with dynamic scenario sliders, economic moat diagnostics,
reverse DCF expectations, and Margin of Safety buy targets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_dashboard_html(data: dict[str, Any], output_path: Path) -> Path:
    """Write an interactive standalone HTML dashboard with embedded data."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    json_payload = json.dumps(data, indent=2)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>DCF & Valuation Engine Dashboard</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    body {{
      background-color: #0b0f19;
      color: #f1f5f9;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 0.2rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
    }}
    .badge-green {{ background: rgba(34, 197, 94, 0.15); color: #22c55e; border: 1px solid rgba(34, 197, 94, 0.3); }}
    .badge-amber {{ background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); }}
    .badge-red {{ background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }}
    .badge-blue {{ background: rgba(59, 130, 246, 0.15); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.3); }}
  </style>
</head>
<body class="antialiased p-4 sm:p-8 min-h-screen">
  <div class="max-w-6xl mx-auto space-y-6">

    <!-- Header -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <div class="flex items-center gap-2.5">
          <span class="w-3 h-3 rounded-full bg-emerald-500 animate-pulse"></span>
          <h1 class="text-2xl font-bold tracking-tight text-white">DCF Valuation Engine Dashboard</h1>
        </div>
        <p class="text-slate-400 text-sm mt-1">Multi-lens valuation: Intrinsic DCF, Economic Moat, Reverse Expectations, and Margin of Safety.</p>
      </div>
      <div class="flex items-center gap-2 text-xs text-slate-400 bg-slate-950 px-3 py-1.5 rounded-lg border border-slate-800 font-mono">
        <span>Audited Engine • 261 Tests • Standalone Report</span>
      </div>
    </div>

    <!-- Comparative Table -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-xl">
      <div class="p-5 border-b border-slate-800 flex justify-between items-center">
        <div>
          <h2 class="text-lg font-semibold text-white">Cross-Company Valuation Summary</h2>
          <p class="text-xs sm:text-sm text-slate-400">Click on any company row to inspect in-depth diagnostics.</p>
        </div>
        <span class="text-xs font-mono text-slate-400 bg-slate-950 px-2.5 py-1 rounded border border-slate-800" id="tickerCountBadge"></span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead class="bg-slate-950 text-slate-400 text-xs uppercase font-semibold border-b border-slate-800">
            <tr>
              <th class="py-3.5 px-4">Ticker</th>
              <th class="py-3.5 px-4">Market Price</th>
              <th class="py-3.5 px-4">Base DCF</th>
              <th class="py-3.5 px-4">Blended Fair Value</th>
              <th class="py-3.5 px-4">WACC</th>
              <th class="py-3.5 px-4">ROIC (Moat)</th>
              <th class="py-3.5 px-4">Implied 5-Yr CAGR</th>
              <th class="py-3.5 px-4">Verdict</th>
            </tr>
          </thead>
          <tbody id="summaryTableBody" class="divide-y divide-slate-800/80 font-mono text-xs sm:text-sm">
          </tbody>
        </table>
      </div>
    </div>

    <!-- Detailed Company Inspector -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

      <!-- Left Column: Company Selector & DCF Range Visualizer -->
      <div class="lg:col-span-1 space-y-6">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
          <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-400">Select Company</h3>
          <div class="grid grid-cols-5 gap-1.5" id="tickerSelector"></div>

          <div class="pt-3 border-t border-slate-800 space-y-3">
            <div class="flex justify-between items-baseline">
              <span class="text-xs text-slate-400">Market Price</span>
              <span class="text-xl font-bold font-mono text-white" id="dispPrice">$0.00</span>
            </div>
            <div class="flex justify-between items-baseline">
              <span class="text-xs text-slate-400">DCF Base Value</span>
              <span class="text-base font-semibold font-mono text-blue-400" id="dispBaseValue">$0.00</span>
            </div>
            <div class="flex justify-between items-baseline">
              <span class="text-xs text-slate-400">Discount to Expected Value</span>
              <span class="text-sm font-bold font-mono" id="dispDiscount">0.0%</span>
            </div>
          </div>

          <!-- Scenario Range -->
          <div class="pt-3 border-t border-slate-800 space-y-2">
            <div class="flex justify-between text-xs text-slate-400 font-mono">
              <span>Bear: <strong class="text-white" id="dispBear">$0</strong></span>
              <span>Base: <strong class="text-white" id="dispBase">$0</strong></span>
              <span>Bull: <strong class="text-white" id="dispBull">$0</strong></span>
            </div>
            <p class="text-[11px] text-slate-400 text-center pt-1" id="dispValuationComment"></p>
          </div>
        </div>

        <!-- Interactive Scenario Blender Simulator -->
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
          <div class="flex justify-between items-center">
            <h3 class="text-xs font-semibold uppercase tracking-wider text-slate-400">Dynamic Scenario Weights</h3>
            <button onclick="resetWeights()" class="text-xs text-blue-400 hover:underline">Reset (25/50/25)</button>
          </div>
          <div class="space-y-3 text-xs">
            <div>
              <div class="flex justify-between mb-1">
                <span>Bear Case Weight:</span>
                <span class="font-mono font-bold text-white" id="labelBearWeight">25%</span>
              </div>
              <input type="range" id="sliderBear" min="0" max="100" value="25" oninput="updateScenarioWeights()" class="w-full h-1.5 bg-slate-950 rounded-lg appearance-none cursor-pointer">
            </div>
            <div>
              <div class="flex justify-between mb-1">
                <span>Base Case Weight:</span>
                <span class="font-mono font-bold text-white" id="labelBaseWeight">50%</span>
              </div>
              <input type="range" id="sliderBase" min="0" max="100" value="50" oninput="updateScenarioWeights()" class="w-full h-1.5 bg-slate-950 rounded-lg appearance-none cursor-pointer">
            </div>
            <div>
              <div class="flex justify-between mb-1">
                <span>Bull Case Weight:</span>
                <span class="font-mono font-bold text-white" id="labelBullWeight">25%</span>
              </div>
              <input type="range" id="sliderBull" min="0" max="100" value="25" oninput="updateScenarioWeights()" class="w-full h-1.5 bg-slate-950 rounded-lg appearance-none cursor-pointer">
            </div>
          </div>
          <div class="bg-slate-950 p-3 rounded-xl border border-slate-800 text-xs space-y-1 font-mono">
            <div class="flex justify-between">
              <span class="text-slate-400">Dynamic Expected Value:</span>
              <span class="font-bold text-emerald-400 text-sm" id="dynExpectedValue">$0.00</span>
            </div>
            <div class="flex justify-between">
              <span class="text-slate-400">Margin of Safety vs Tape:</span>
              <span class="font-bold" id="dynMarginOfSafety">0.0%</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Right Columns: Moat, Reverse DCF, and Buy Targets -->
      <div class="lg:col-span-2 space-y-6">

        <!-- 2 Grid Cards -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">

          <!-- Economic Moat Card -->
          <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-3">
            <div class="flex justify-between items-start">
              <div>
                <span class="text-xs uppercase font-semibold tracking-wider text-slate-400">Capital Efficiency</span>
                <h4 class="text-base font-bold text-white mt-0.5">Economic Moat (ROIC)</h4>
              </div>
              <span id="badgeMoat" class="badge badge-green">Wide Moat</span>
            </div>
            <div class="grid grid-cols-2 gap-2 pt-2 text-xs font-mono">
              <div class="bg-slate-950 p-2.5 rounded-lg border border-slate-800">
                <span class="text-slate-400 block text-[10px]">Return on Capital (ROIC)</span>
                <span class="font-bold text-sm text-white" id="dispRoic">0.0%</span>
              </div>
              <div class="bg-slate-950 p-2.5 rounded-lg border border-slate-800">
                <span class="text-slate-400 block text-[10px]">Cost of Capital (WACC)</span>
                <span class="font-bold text-sm text-white" id="dispWacc">0.0%</span>
              </div>
            </div>
            <div class="bg-slate-950 p-2.5 rounded-lg border border-slate-800 text-xs font-mono flex justify-between">
              <span class="text-slate-400">Economic Spread (ROIC - WACC):</span>
              <span class="font-bold text-emerald-400" id="dispSpread">+0.0%</span>
            </div>
            <p class="text-xs text-slate-400" id="dispMoatDiag"></p>
          </div>

          <!-- Reverse DCF Card -->
          <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-3">
            <div class="flex justify-between items-start">
              <div>
                <span class="text-xs uppercase font-semibold tracking-wider text-slate-400">Market Pricing</span>
                <h4 class="text-base font-bold text-white mt-0.5">Reverse DCF Expectations</h4>
              </div>
              <span class="badge badge-blue">Implied Targets</span>
            </div>
            <div class="space-y-2 text-xs font-mono">
              <div class="flex justify-between bg-slate-950 p-2 rounded-lg border border-slate-800">
                <span class="text-slate-400">Implied 5-Yr Revenue CAGR:</span>
                <span class="font-bold text-white" id="dispRevCAGR">0.0%</span>
              </div>
              <div class="flex justify-between bg-slate-950 p-2 rounded-lg border border-slate-800">
                <span class="text-slate-400">Implied Operating Margin:</span>
                <span class="font-bold text-white" id="dispImpliedMargin">0.0%</span>
              </div>
              <div class="flex justify-between bg-slate-950 p-2 rounded-lg border border-slate-800">
                <span class="text-slate-400">Implied Perpetuity Growth:</span>
                <span class="font-bold text-white" id="dispImpliedG">0.0%</span>
              </div>
            </div>
            <p class="text-xs text-slate-400" id="dispRevDiag"></p>
          </div>

        </div>

        <!-- Margin of Safety & Target Buy Prices Card -->
        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
          <div class="flex justify-between items-center">
            <div>
              <span class="text-xs uppercase font-semibold tracking-wider text-slate-400">Warren Buffett / Ben Graham Hurdle</span>
              <h4 class="text-base font-bold text-white mt-0.5">Target Buy Prices (Margin of Safety)</h4>
            </div>
            <span class="text-xs text-slate-400">Discount to Blended Fair Value</span>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 font-mono text-xs">
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-1">
              <span class="text-slate-400 text-[11px] block">15% Discount (Wide Moat)</span>
              <span class="text-base font-bold text-emerald-400" id="target15">$0.00</span>
              <span class="text-[10px] text-slate-400 block">Conservative Entry</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-1">
              <span class="text-slate-400 text-[11px] block">25% Discount (Standard)</span>
              <span class="text-base font-bold text-blue-400" id="target25">$0.00</span>
              <span class="text-[10px] text-slate-400 block">Defensive Value</span>
            </div>
            <div class="bg-slate-950 p-3.5 rounded-xl border border-slate-800 space-y-1">
              <span class="text-slate-400 text-[11px] block">35% Discount (Deep Value)</span>
              <span class="text-base font-bold text-amber-400" id="target35">$0.00</span>
              <span class="text-[10px] text-slate-400 block">High Uncertainty</span>
            </div>
          </div>

          <div class="p-3 bg-slate-950 rounded-xl border border-slate-800 flex justify-between items-center text-xs font-mono">
            <span class="text-slate-400">Risk / Reward Asymmetry (Bull upside vs. Bear downside):</span>
            <span class="font-bold text-white" id="dispAsymmetry">0.0x</span>
          </div>
        </div>

      </div>

    </div>

  </div>

  <script>
    const data = {json_payload};
    let selectedTicker = Object.keys(data)[0] || "";

    function initTable() {{
      const tbody = document.getElementById("summaryTableBody");
      document.getElementById("tickerCountBadge").textContent = `${{Object.keys(data).length}} Tickers`;
      tbody.innerHTML = "";
      for (const [ticker, item] of Object.entries(data)) {{
        const tr = document.createElement("tr");
        tr.className = "hover:bg-slate-950 cursor-pointer transition-colors";
        tr.onclick = () => selectTicker(ticker);

        const spread = item.moat ? item.moat.spread : 0;
        const spreadColor = spread >= 0 ? "text-emerald-400" : "text-red-400";
        const verdict = item.blended ? item.blended.verdict : "Neutral";
        const verdictBadge = verdict.includes("Fairly")
          ? '<span class="badge badge-green">Fairly Valued</span>'
          : verdict.includes("Extreme")
          ? '<span class="badge badge-red">Growth Premium</span>'
          : '<span class="badge badge-amber">Demanding</span>';

        const cagrText = item.reverse_dcf && item.reverse_dcf.implied_rev_growth != null
          ? `${{(item.reverse_dcf.implied_rev_growth * 100).toFixed(1)}}%`
          : (item.reverse_dcf ? item.reverse_dcf.implied_rev_status : "n/a");

        tr.innerHTML = `
          <td class="py-3 px-4 font-bold text-white">${{ticker}}</td>
          <td class="py-3 px-4">$${{item.current_price ? item.current_price.toFixed(2) : '0.00'}}</td>
          <td class="py-3 px-4 font-semibold text-blue-400">$${{item.base_value ? item.base_value.toFixed(2) : '0.00'}}</td>
          <td class="py-3 px-4 font-semibold text-emerald-400">$${{item.blended ? item.blended.expected_value.toFixed(2) : '0.00'}}</td>
          <td class="py-3 px-4">${{(item.wacc * 100).toFixed(1)}}%</td>
          <td class="py-3 px-4 ${{spreadColor}}">${{item.moat ? (item.moat.roic * 100).toFixed(1) : 0}}% (${{spread >= 0 ? '+' : ''}}${{(spread * 100).toFixed(1)}}%)</td>
          <td class="py-3 px-4">${{cagrText}}</td>
          <td class="py-3 px-4">${{verdictBadge}}</td>
        `;
        tbody.appendChild(tr);
      }}
    }}

    function initButtons() {{
      const container = document.getElementById("tickerSelector");
      container.innerHTML = "";
      for (const ticker of Object.keys(data)) {{
        const btn = document.createElement("button");
        btn.textContent = ticker;
        btn.id = "btn-" + ticker;
        btn.className = "py-1.5 px-2 rounded-lg text-xs font-mono font-bold border transition-all " +
          (ticker === selectedTicker
            ? "bg-blue-600 text-white border-blue-500 shadow-lg"
            : "bg-slate-950 text-slate-400 border-slate-800 hover:text-white");
        btn.onclick = () => selectTicker(ticker);
        container.appendChild(btn);
      }}
    }}

    function selectTicker(ticker) {{
      selectedTicker = ticker;
      initButtons();
      renderDetails();
    }}

    function renderDetails() {{
      const item = data[selectedTicker];
      if (!item) return;

      document.getElementById("dispPrice").textContent = `$${{item.current_price ? item.current_price.toFixed(2) : '0.00'}}`;
      document.getElementById("dispBaseValue").textContent = `$${{item.base_value ? item.base_value.toFixed(2) : '0.00'}}`;

      const discount = item.blended ? item.blended.discount_to_expected : 0;
      const discElem = document.getElementById("dispDiscount");
      discElem.textContent = `${{discount >= 0 ? '+' : ''}}${{(discount * 100).toFixed(1)}}%`;
      discElem.className = "text-sm font-bold font-mono " + (discount >= 0 ? "text-emerald-400" : "text-red-400");

      document.getElementById("dispBear").textContent = `$${{item.bear_value ? item.bear_value.toFixed(2) : '0.00'}}`;
      document.getElementById("dispBase").textContent = `$${{item.base_value ? item.base_value.toFixed(2) : '0.00'}}`;
      document.getElementById("dispBull").textContent = `$${{item.bull_value ? item.bull_value.toFixed(2) : '0.00'}}`;

      // Moat Card
      if (item.moat) {{
        const moatBadge = document.getElementById("badgeMoat");
        moatBadge.textContent = item.moat.rating;
        moatBadge.className = "badge " + (item.moat.spread >= 0.05 ? "badge-green" : item.moat.spread >= 0 ? "badge-blue" : "badge-red");
        document.getElementById("dispRoic").textContent = `${{(item.moat.roic * 100).toFixed(1)}}%`;
        document.getElementById("dispWacc").textContent = `${{(item.wacc * 100).toFixed(1)}}%`;
        const spreadElem = document.getElementById("dispSpread");
        spreadElem.textContent = `${{item.moat.spread >= 0 ? '+' : ''}}${{(item.moat.spread * 100).toFixed(1)}}%`;
        spreadElem.className = "font-bold " + (item.moat.spread >= 0 ? "text-emerald-400" : "text-red-400");
        document.getElementById("dispMoatDiag").textContent = item.moat.diagnostics ? item.moat.diagnostics[0] : "";
      }}

      // Reverse DCF Card
      if (item.reverse_dcf) {{
        document.getElementById("dispRevCAGR").textContent = item.reverse_dcf.implied_rev_growth != null
          ? `${{(item.reverse_dcf.implied_rev_growth * 100).toFixed(2)}}%`
          : item.reverse_dcf.implied_rev_status;
        document.getElementById("dispImpliedMargin").textContent = item.reverse_dcf.implied_margin != null
          ? `${{(item.reverse_dcf.implied_margin * 100).toFixed(2)}}%`
          : item.reverse_dcf.implied_margin_status;
        document.getElementById("dispImpliedG").textContent = item.reverse_dcf.implied_perpetuity_g != null
          ? `${{(item.reverse_dcf.implied_perpetuity_g * 100).toFixed(2)}}%`
          : item.reverse_dcf.implied_growth_status;
        document.getElementById("dispRevDiag").textContent = item.reverse_dcf.warnings && item.reverse_dcf.warnings[0] ? item.reverse_dcf.warnings[0] : "Market pricing reflects steady growth expectations.";
      }}

      // Buy Targets Card
      if (item.blended && item.blended.target_buys) {{
        const buys = Object.values(item.blended.target_buys);
        document.getElementById("target15").textContent = `$${{buys[0] ? buys[0].toFixed(2) : '0.00'}}`;
        document.getElementById("target25").textContent = `$${{buys[1] ? buys[1].toFixed(2) : '0.00'}}`;
        document.getElementById("target35").textContent = `$${{buys[2] ? buys[2].toFixed(2) : '0.00'}}`;
        document.getElementById("dispAsymmetry").textContent = item.blended.asymmetry_ratio != null
          ? (item.blended.asymmetry_ratio === Infinity ? "Pure Upside" : `${{item.blended.asymmetry_ratio.toFixed(2)}}x`)
          : "n/a";
      }}

      document.getElementById("dispValuationComment").textContent = `${{selectedTicker}} trades at $${{item.current_price.toFixed(2)}} vs. Base DCF of $${{item.base_value.toFixed(2)}}.`;

      updateScenarioWeights();
    }}

    function updateScenarioWeights() {{
      let wBear = parseFloat(document.getElementById("sliderBear").value);
      let wBase = parseFloat(document.getElementById("sliderBase").value);
      let wBull = parseFloat(document.getElementById("sliderBull").value);

      const total = wBear + wBase + wBull;
      if (total === 0) return;

      const normBear = wBear / total;
      const normBase = wBase / total;
      const normBull = wBull / total;

      document.getElementById("labelBearWeight").textContent = `${{(normBear * 100).toFixed(0)}}%`;
      document.getElementById("labelBaseWeight").textContent = `${{(normBase * 100).toFixed(0)}}%`;
      document.getElementById("labelBullWeight").textContent = `${{(normBull * 100).toFixed(0)}}%`;

      const item = data[selectedTicker];
      if (!item) return;

      const dynExpected = (normBear * (item.bear_value || 0)) + (normBase * (item.base_value || 0)) + (normBull * (item.bull_value || 0));
      const dynMargin = dynExpected > 0 ? 1.0 - (item.current_price / dynExpected) : -1.0;

      document.getElementById("dynExpectedValue").textContent = `$${{dynExpected.toFixed(2)}}`;
      const marginElem = document.getElementById("dynMarginOfSafety");
      marginElem.textContent = `${{(dynMargin * 100).toFixed(1)}}%`;
      marginElem.className = "font-mono font-bold " + (dynMargin >= 0 ? "text-emerald-400" : "text-red-400");
    }}

    function resetWeights() {{
      document.getElementById("sliderBear").value = 25;
      document.getElementById("sliderBase").value = 50;
      document.getElementById("sliderBull").value = 25;
      updateScenarioWeights();
    }}

    if (Object.keys(data).length > 0) {{
      initTable();
      initButtons();
      renderDetails();
    }}
  </script>
</body>
</html>
"""
    output_path.write_text(html_content, encoding="utf-8")
    return output_path
