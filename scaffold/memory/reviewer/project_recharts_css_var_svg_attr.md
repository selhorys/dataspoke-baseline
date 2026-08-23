---
name: recharts-css-var-svg-attr
description: var() DOES resolve in SVG presentation attributes in modern Chromium, so Recharts stroke=hsl(var(--token)) works
metadata:
  type: project
---

Recharts applies `stroke`/`fill` as SVG **presentation attributes** (Curve.js → `createElement("path", {...svgPropertiesNoEvents(props)})`), not inline style. Common lore says CSS `var()` does not resolve in SVG presentation attributes — but that lore is stale.

**Why:** Empirically verified in Chromium 1223 (Playwright): `<path stroke="hsl(var(--brand))">` with `--brand` on `:root` computes to the resolved rgb (same as `style="stroke:hsl(var(--brand))"` and as the hex literal). Custom props inherit to the path, so token-derived chart colors also track `.dark`.

**How to apply:** Do NOT flag `stroke="hsl(var(--…))"` / `CHART_COLORS = ["hsl(var(--…))"]` in Recharts as a broken-wiring defect. It renders correctly. Canvas-rendered graphs (react-force-graph, 2d context) are different — there `var()` truly won't resolve, so hex literals there are legitimate, not an inconsistency. Verify engine empirically before claiming a token-wiring regression; jsdom can't catch real-browser rendering here either way.
