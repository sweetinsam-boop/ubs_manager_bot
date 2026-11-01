"""운용 리포트 HTML. 외부 요청이 없는 단일 파일이라 텔레그램 문서로 받아 바로 열 수 있다.

디자인: Hallmark Coral (앱 화면). 표 우선, 포인트 컬러 하나, 숫자는 모노.
build_report_html()은 순수 함수(입력 dict → 문자열)이고, save_report()가 파일로 저장한다.
"""
from __future__ import annotations

import datetime
import html
from pathlib import Path

from storage.paths import STORAGE_ROOT
from v4_rules import KIND_LABEL

_e = html.escape
REPORT_DIR = STORAGE_ROOT / "reports"
_KEEP = 14

_KIND_LABEL = KIND_LABEL

_CSS = """/* Hallmark · shape: app screen · genre: modern-minimal · theme: Coral (deal 1/3, seed investment-bot-report-dashboard-de5c)
 * nav: none, single document · signature: the T track beside the price ladder · states: full, empty, error
 * axes: light/grotesk-sans/warm · posture: restrained · contrast: pass · v2.0.0 */
/* Hallmark · pre-emit critique: P4 H4 E3 S4 R4 V4 · weakest: E3, a static report has no interaction beyond focus, so states are full, empty and error only */
:root {
  color-scheme: light;
  --space-3xs: 2px; --space-2xs: 4px; --space-xs: 8px; --space-sm: 12px; --space-md: 16px; --space-lg: 24px; --space-xl: 40px; --space-2xl: 64px;
  --text-xs: 0.75rem; --text-sm: 0.875rem; --text-md: 1rem; --text-lg: 1.2rem; --text-xl: 1.44rem; --text-2xl: 1.728rem;
  --text-display: 1.728rem; --text-section: 1.44rem; --text-title: 1.2rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1); --dur-micro: 120ms; --dur-short: 220ms;
  --color-error: oklch(52% 0.19 27); --color-success: oklch(50% 0.12 150);
  --container: 1120px; --gutter: 40px; --gap-columns: 32px;
  --color-paper: oklch(97% 0.006 55); --color-paper-2: oklch(94.5% 0.007 55); --color-paper-3: oklch(91.5% 0.009 55);
  --color-ink: oklch(21% 0.012 38); --color-ink-2: oklch(29% 0.013 40); --color-muted: oklch(47% 0.013 45);
  --color-rule: oklch(87% 0.009 55); --color-rule-2: oklch(81% 0.011 55);
  --color-accent: oklch(74% 0.125 30); --color-accent-ink: oklch(21% 0.012 38); --color-accent-2: oklch(54% 0.165 29); --color-focus: oklch(54% 0.17 30);
  --color-sheet: oklch(98.8% 0.0045 80);
  --font-display: "Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", ui-sans-serif, system-ui, sans-serif;
  --font-body: var(--font-display);
  --font-outlier: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  --radius-card: 8px; --radius-input: 6px; --radius-pill: 999px; --rule-hair: 1px;
  --tracking-display: -0.02em; --display-weight: 600;
}
*, *::before, *::after { box-sizing: border-box; }
html, body { overflow-x: clip; }
body { margin: 0; background: var(--color-paper); color: var(--color-ink); font-family: var(--font-body); font-size: var(--text-md); line-height: 1.55; -webkit-font-smoothing: antialiased; }
::selection { background: color-mix(in oklab, var(--color-accent) 22%, transparent); }
:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; }
html { scrollbar-color: var(--color-muted) var(--color-paper); }
a { color: inherit; text-underline-offset: 0.16em; }
a:hover { color: var(--color-accent-2); } a:active { color: var(--color-ink); }
h1, h2, h3 { margin: 0; font-weight: var(--display-weight); letter-spacing: var(--tracking-display); line-height: 1.15; overflow-wrap: break-word; min-width: 0; text-wrap: balance; }
h1 { font-size: var(--text-display); } h2 { font-size: var(--text-section); } h3 { font-size: var(--text-title); }
p { margin: 0; }
svg { display: block; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; scroll-behavior: auto !important; } }

.container { width: 100%; max-width: var(--container); margin-inline: auto; padding-inline: var(--gutter); }
.num { font-family: var(--font-outlier); font-variant-numeric: tabular-nums; }
.muted { color: var(--color-muted); }
.small { font-size: var(--text-sm); }
.pos { color: var(--color-success); } .neg { color: var(--color-error); }

.bar { border-bottom: var(--rule-hair) solid var(--color-rule); padding-block: var(--space-lg); }
.bar .container { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: var(--space-xs) var(--space-lg); }

.stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-bottom: var(--rule-hair) solid var(--color-rule); background: var(--color-sheet); }
.stat { padding: var(--space-md) var(--space-lg); border-left: var(--rule-hair) solid var(--color-rule); min-width: 0; }
.stat:first-child { border-left: 0; padding-left: 0; }
.stat .figure { display: block; font-family: var(--font-outlier); font-variant-numeric: tabular-nums; font-size: var(--text-lg); line-height: 1.3; overflow-wrap: anywhere; }
.stat .label { font-size: var(--text-sm); color: var(--color-muted); }

.ticker { padding-block: var(--space-xl); border-bottom: var(--rule-hair) solid var(--color-rule); }
.ticker-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--space-xs) var(--space-lg); margin-bottom: var(--space-lg); }
.ticker-head .sub { color: var(--color-muted); font-size: var(--text-sm); }
.mode { display: inline-block; border: var(--rule-hair) solid var(--color-rule-2); border-radius: var(--radius-pill); padding: var(--space-3xs) var(--space-sm); font-size: var(--text-sm); }
.mode.rev { border-color: var(--color-accent-2); color: var(--color-accent-2); }
.cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--space-xl) var(--gap-columns); }
.cols .wide { grid-column: 1 / -1; }
.block h3 { margin-bottom: var(--space-sm); }
.facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: var(--space-2xs) var(--space-lg); margin: var(--space-md) 0 0; font-size: var(--text-sm); }
.facts dt { color: var(--color-muted); } .facts dd { margin: 0; text-align: right; font-family: var(--font-outlier); font-variant-numeric: tabular-nums; }

.track { position: relative; height: 14px; background: var(--color-paper-3); border-radius: var(--radius-input); margin: var(--space-lg) 0 var(--space-xl); }
.track-fill { position: absolute; inset: 0 auto 0 0; width: var(--fill); max-width: 100%; background: var(--color-accent); border-radius: var(--radius-input); }
.track-tick { position: absolute; top: -5px; bottom: -5px; left: var(--at); width: 1px; background: var(--color-ink-2); }
.track-tick span { position: absolute; top: 100%; margin-top: var(--space-2xs); transform: translateX(-50%); font-size: var(--text-xs); color: var(--color-muted); white-space: nowrap; }
.track-tick.end span { transform: translateX(-100%); }
.track-tick.start span { transform: none; }

.ladder { display: grid; gap: var(--space-2xs); }
.rung { display: grid; grid-template-columns: 6rem 5.5rem 4.5rem minmax(0, 1fr); align-items: center; gap: var(--space-sm); font-size: var(--text-sm); padding-block: var(--space-2xs); border-bottom: var(--rule-hair) solid var(--color-rule); }
.rung .num:nth-child(2) { text-align: right; }
.rung .num:nth-child(3) { text-align: right; color: var(--color-muted); }
.rung-track { position: relative; height: 14px; background: var(--color-paper-2); border-radius: var(--radius-input); }
.rung-track i { position: absolute; top: -2px; bottom: -2px; left: var(--at); width: 3px; background: var(--color-ink-2); }
.rung.now { font-weight: 600; } .rung.now .rung-track i { background: var(--color-accent-2); }

table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th { text-align: left; font-weight: 500; color: var(--color-muted); padding: var(--space-xs) var(--space-sm); border-bottom: var(--rule-hair) solid var(--color-rule-2); }
td { padding: var(--space-xs) var(--space-sm); border-bottom: var(--rule-hair) solid var(--color-rule); vertical-align: top; }
th.r, td.r { text-align: right; }
td.r, td.num { font-family: var(--font-outlier); font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }

.section { padding-block: var(--space-xl); border-bottom: var(--rule-hair) solid var(--color-rule); }
.section > .container > h2 { margin-bottom: var(--space-md); }
.empty { padding: var(--space-lg); border: var(--rule-hair) dashed var(--color-rule-2); border-radius: var(--radius-card); color: var(--color-muted); font-size: var(--text-sm); }
.error { padding: var(--space-md); border-left: 2px solid var(--color-error); color: var(--color-error); font-size: var(--text-sm); }
.notice { padding: var(--space-sm) var(--space-md); border-left: 2px solid var(--color-accent-2); background: var(--color-sheet); font-size: var(--text-sm); margin-bottom: var(--space-md); }

.chart { width: 100%; height: auto; }
.chart .grid { stroke: var(--color-rule); stroke-width: 1; }
.chart .axis { fill: var(--color-muted); font-family: var(--font-outlier); font-size: 11px; }
.chart .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart .s-equity { stroke: var(--color-accent-2); }
.chart .s-voo { stroke: var(--color-muted); }
.chart .s-qqq { stroke: var(--color-ink-2); stroke-dasharray: 5 4; }
.legend { display: flex; flex-wrap: wrap; gap: var(--space-xs) var(--space-lg); font-size: var(--text-sm); color: var(--color-muted); margin: var(--space-xs) 0; }
.legend span::before { content: ""; display: inline-block; width: 18px; height: 0; border-top: 2px solid; vertical-align: middle; margin-right: var(--space-xs); }
.legend .s-equity { color: var(--color-accent-2); } .legend .s-voo { color: var(--color-muted); } .legend .s-qqq { color: var(--color-ink-2); }

footer { padding-block: var(--space-xl); color: var(--color-muted); font-size: var(--text-sm); }
footer p + p { margin-top: var(--space-xs); }

@media (max-width: 60rem) { :root { --gutter: 24px; } .cols { grid-template-columns: minmax(0, 1fr); } .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } .stat:nth-child(odd) { border-left: 0; padding-left: 0; } .stat:last-child { grid-column: 1 / -1; } }
@media (max-width: 40rem) { :root { --gutter: 16px; } .rung { grid-template-columns: 5rem 5rem minmax(0, 1fr); } .rung .num:nth-child(3) { display: none; } }
"""


def _usd(v, d=2) -> str:
    return f"${v:,.{d}f}"


def _signed(v, d=2) -> str:
    return f"{v:+,.{d}f}"


def _cls(v) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _empty(msg: str) -> str:
    return f'<p class="empty">{_e(msg)}</p>'


def _mode_label(t: dict) -> tuple[str, bool]:
    if t["mode"] == "first":
        return "새 사이클 첫 매수", False
    if t["mode"] == "reverse":
        return "소진 후 리버스모드", True
    return ("일반모드 전반전" if t.get("first_half") else "일반모드 후반전"), False


def _stats(summary: dict, realized: float) -> str:
    cost, value = summary["total_cost"], summary["total_value"]
    pnl = value - cost
    pnl_rt = pnl / cost * 100 if cost > 0 else 0.0
    cash = summary["current_cash"]
    items = [
        ("계좌 총 평가금액", _usd(value + cash), ""),
        ("주식 평가금액", _usd(value), ""),
        ("달러 예수금", _usd(cash), ""),
        ("평가손익", f"{_signed(pnl)} ({_signed(pnl_rt)}%)" if cost > 0 else "보유 없음", _cls(pnl)),
        ("누적 실현수익", _usd(realized), _cls(realized)),
    ]
    cells = "".join(
        f'<div class="stat"><span class="figure {c}">{_e(v)}</span><span class="label">{_e(lbl)}</span></div>'
        for lbl, v, c in items
    )
    return f'<div class="stats container">{cells}</div>'


def _track(t: dict) -> str:
    n = t["n"] or 1.0
    fill = max(0.0, min(t["t"] / n, 1.0)) * 100
    ticks = [(0.0, "0", "start"), (50.0, f"{n / 2:g}", ""), ((n - 1) / n * 100, f"{n - 1:g} 초과 시 소진", "end")]
    tick_html = "".join(
        f'<span class="track-tick {c}" style="--at:{at:.2f}%"><span>{_e(lbl)}</span></span>' for at, lbl, c in ticks
    )
    return (f'<div class="track" role="img" aria-label="T {t["t"]:.2f} / {n:g}">'
            f'<div class="track-fill" style="--fill:{fill:.2f}%"></div>{tick_html}</div>')


def _ladder(t: dict) -> str:
    rungs = [("현재가", t["now_p"], True)]
    if t["qty"] > 0:
        rungs.append(("평단", t["avg"], False))
        if t.get("star_point"):
            rungs.append(("별지점", t["star_point"], False))
        if t.get("target"):
            rungs.append((f"목표 +{t['target_p']:g}%", t["target"], False))
    rungs = [r for r in rungs if r[1] and r[1] > 0]
    if len(rungs) < 2:
        return _empty("보유 수량이 생기면 평단, 별지점, 목표가가 표시됩니다.")
    lo, hi = min(r[1] for r in rungs), max(r[1] for r in rungs)
    span = (hi - lo) or 1.0
    rows = []
    for label, price, now in sorted(rungs, key=lambda r: -r[1]):
        at = (price - lo) / span * 96 + 1
        vs = (price / t["avg"] - 1) * 100 if t["avg"] > 0 else 0.0
        rows.append(
            f'<div class="rung {"now" if now else ""}"><span>{_e(label)}</span><span class="num">{_usd(price)}</span>'
            f'<span class="num">{_signed(vs, 1)}%</span><span class="rung-track"><i style="--at:{at:.1f}%"></i></span></div>'
        )
    return f'<div class="ladder">{"".join(rows)}</div>'


def _orders_table(t: dict) -> str:
    orders = t.get("orders") or []
    if not orders:
        return _empty("오늘 낼 주문이 없습니다.")
    rows = []
    grid = [o for o in orders if o["kind"] in ("grid", "rev_grid")]
    for o in orders:
        if o in grid:
            continue
        moc = o["type"] == "MOC"
        rows.append(f'<tr><td>{_e(_KIND_LABEL.get(o["kind"], o["kind"]))}</td><td class="r">{o["qty"]}</td>'
                    f'<td class="r">{"시장가" if moc else _usd(o["price"])}</td>'
                    f'<td class="r">{"-" if moc else _usd(o["qty"] * o["price"])}</td></tr>')
    if grid:
        lo, hi = min(g["price"] for g in grid), max(g["price"] for g in grid)
        rows.append(f'<tr><td>{_e(_KIND_LABEL.get(grid[0]["kind"], "줍줍"))} {len(grid)}건</td><td class="r">{len(grid)}</td>'
                    f'<td class="r">{_usd(lo)} ~ {_usd(hi)}</td><td class="r">{_usd(sum(g["price"] for g in grid))}</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>주문</th><th class="r">수량</th><th class="r">가격</th>'
            f'<th class="r">금액</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _ticker_section(t: dict) -> str:
    label, rev = _mode_label(t)
    pnl = f'{_signed(t["pnl_amt"])} ({_signed(t["pnl_rt"])}%)' if t["qty"] > 0 else "보유 없음"
    notice = ""
    if t.get("t_warn"):
        notice = (f'<p class="notice">저장된 T {t["t"]:.2f}와 보유 원가로 추정한 T {t["implied_t"]:.2f}가 크게 다릅니다. '
                  f'증권사 앱의 실제 진행과 다르면 텔레그램에서 /set_t {_e(t["ticker"])} 로 맞추세요.</p>')
    state = "주문 전송됨" if t["locked"] else "오늘 주문 전"
    if t["unfilled_cnt"]:
        state += f", 당일 미체결 {t['unfilled_qty']}주"
    facts = (
        f'<dl class="facts"><dt>보유 수량</dt><dd>{t["qty"]}주</dd>'
        f'<dt>평가손익</dt><dd class="{_cls(t["pnl_amt"])}">{_e(pnl)}</dd>'
        f'<dt>{"쿼터매수 금액 (잔금 ÷ 4)" if rev else "1회 매수금 (잔금 ÷ (N-T))"}</dt><dd>{_usd(t["one_time"])}</dd>'
        f'<dt>잔금</dt><dd>{_usd(t["pool"])} / 원금 {_usd(t["seed"], 0)}</dd>'
        f'<dt>오늘 상태</dt><dd>{_e(state)}</dd></dl>'
    )
    tk = _e(t["ticker"])
    return f"""<section class="ticker" aria-labelledby="tk-{tk}"><div class="container">
<div class="ticker-head"><h2 id="tk-{tk}">{tk}</h2>
<span class="sub">{t['cycle_count']}회차, 시작 {_e(str(t['start_date']))}, {t['n']:g}분할</span>
<span class="mode {'rev' if rev else ''}">{_e(label)}</span></div>
{notice}
<div class="cols"><div class="block"><h3>진행도 T {t['t']:.2f} / {t['n']:g}</h3>{_track(t)}{facts}</div>
<div class="block"><h3>가격 사다리</h3>{_ladder(t)}</div>
<div class="block wide"><h3>오늘 주문 계획</h3>{_orders_table(t)}</div></div>
</div></section>"""


def _chart(perf: list) -> str:
    pts = [p for p in perf if p.get("equity")][-90:]
    if len(pts) < 2:
        return _empty("기록이 2일 이상 쌓이면 계좌 추이가 표시됩니다.")

    def idx(key):
        vals = [p.get(key) for p in pts]
        base = next((v for v in vals if v), None)
        return [(v / base * 100 if v and base else None) for v in vals]

    series = [("s-equity", "계좌", idx("equity")), ("s-voo", "미국 대형주 지수", idx("benchmark")),
              ("s-qqq", "나스닥 지수", idx("nasdaq"))]
    allv = [v for _, _, vs in series for v in vs if v]
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    W, H, L, R, T, B = 960, 260, 56, 12, 12, 28

    def px(i):
        return L + (W - L - R) * i / (len(pts) - 1)

    def py(v):
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    grid = ""
    for k in range(4):
        v = lo + (hi - lo) * k / 3
        grid += (f'<line class="grid" x1="{L}" x2="{W - R}" y1="{py(v):.1f}" y2="{py(v):.1f}"/>'
                 f'<text class="axis" x="{L - 8}" y="{py(v) + 4:.1f}" text-anchor="end">{v:.1f}</text>')
    lines = ""
    for cls, _, vs in series:
        seg = [f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vs) if v]
        if len(seg) >= 2:
            lines += f'<polyline class="line {cls}" points="{" ".join(seg)}"/>'
    xl = (f'<text class="axis" x="{L}" y="{H - 8}">{_e(pts[0]["date"])}</text>'
          f'<text class="axis" x="{W - R}" y="{H - 8}" text-anchor="end">{_e(pts[-1]["date"])}</text>')
    legend = "".join(f'<span class="{c}">{_e(n)}</span>' for c, n, vs in series if any(vs))
    return (f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="계좌와 지수 추이 (시작일=100)">{grid}{lines}{xl}</svg>'
            f'<div class="legend">{legend}</div>'
            '<p class="small muted">시작일을 100으로 맞춘 지수입니다. 이전 버전 기록에는 예수금이 포함되지 않았을 수 있습니다.</p>')


def _fills(orders: list) -> str:
    if not orders:
        return _empty("최근 3일간 체결 내역이 없습니다.")
    rows = []
    for o in orders[:12]:
        d = o["ord_dt"]
        dt = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else d
        rows.append(f'<tr><td class="num">{_e(dt)}</td><td>{_e(o["ticker"])}</td><td>{_e(o["side_label"])}</td>'
                    f'<td class="r">{o["ccld_qty"]}</td><td class="r">{_usd(o["avg_fill_pr"])}</td><td>{_e(o["tags"])}</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>날짜</th><th>종목</th><th>구분</th><th class="r">수량</th>'
            f'<th class="r">체결가</th><th>방식</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _profit(cumul: dict, tickers: list) -> str:
    hist = [h for h in (cumul.get("history") or []) if isinstance(h, dict)]
    by: dict[str, float] = {}
    for h in hist:
        by[h.get("ticker", "?")] = by.get(h.get("ticker", "?"), 0.0) + float(h.get("profit") or 0)
    names = list(dict.fromkeys(list(tickers) + sorted(by)))
    total = {n: float(cumul.get(n, by.get(n, 0.0)) or 0.0) for n in names}
    if not hist and not any(abs(v) > 1e-9 for v in total.values()):
        return _empty("아직 확정된 실현수익이 없습니다. 첫 익절이 체결되면 여기에 쌓입니다.")
    head = "".join(f'<tr><td>{_e(n)}</td><td class="r {_cls(v)}">{_usd(v)}</td></tr>' for n, v in total.items())
    recent = "".join(
        f'<tr><td class="num">{_e(str(h.get("date", ""))[:10])}</td><td>{_e(str(h.get("ticker", "")))}</td>'
        f'<td class="r">{h.get("sell_qty", 0)}</td><td class="r">{_usd(float(h.get("sell_price") or 0))}</td>'
        f'<td class="r {_cls(float(h.get("profit") or 0))}">{_signed(float(h.get("profit") or 0))}</td></tr>'
        for h in reversed(hist[-8:]))
    recent_tbl = ('<div class="table-wrap"><table><thead><tr><th>날짜</th><th>종목</th><th class="r">수량</th>'
                  f'<th class="r">매도가</th><th class="r">실현손익</th></tr></thead><tbody>{recent}</tbody></table></div>') if recent else ""
    return ('<div class="cols"><div class="table-wrap"><table><thead><tr><th>종목</th><th class="r">누적 실현수익</th></tr></thead>'
            f'<tbody>{head}</tbody></table></div><div>{recent_tbl}</div></div>')


def _cycles(cycles: dict) -> str:
    rows = []
    for tk, c in cycles.items():
        for h in (c.get("history") or [])[-4:]:
            rows.append(f'<tr><td>{_e(tk)}</td><td class="r">{h.get("cycle_num", "-")}</td>'
                        f'<td class="num">{_e(str(h.get("start_date", "-")))}</td><td class="num">{_e(str(h.get("end_date", "-")))}</td></tr>')
        a = c.get("active")
        if a:
            rows.append(f'<tr><td>{_e(tk)}</td><td class="r">{a.get("cycle_num", "-")}</td>'
                        f'<td class="num">{_e(str(a.get("start_date", "-")))}</td><td>진행 중</td></tr>')
    if not rows:
        return _empty("사이클 기록이 아직 없습니다.")
    return ('<div class="table-wrap"><table><thead><tr><th>종목</th><th class="r">회차</th><th>시작</th><th>종료</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def build_report_html(summary: dict, perf: list, cumul: dict, cycles: dict, now_kst: str, error: str = "") -> str:
    realized = sum(float(v) for k, v in cumul.items()
                   if k not in ("history", "processed_orders") and isinstance(v, (int, float)))
    tickers = [t["ticker"] for t in summary.get("tickers", [])]
    body_tickers = "".join(_ticker_section(t) for t in summary.get("tickers", [])) or (
        '<section class="section"><div class="container">'
        + _empty("등록된 종목이 없습니다. 텔레그램에서 /add_target 으로 추가하세요.") + '</div></section>')
    err = f'<div class="container"><p class="error">{_e(error)}</p></div>' if error else ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>무한매수 운용 리포트 {_e(now_kst)}</title>
<meta name="description" content="무한매수법 V4.0 봇의 T 진행도, 가격 사다리, 오늘 주문 계획, 체결과 실현수익을 한 장에 정리한 리포트">
<meta name="color-scheme" content="light">
<style>
{_CSS}</style></head><body>
<header class="bar"><div class="container"><h1>무한매수 운용 리포트</h1>
<p class="small muted">기준 {_e(now_kst)} KST, 미국 {_e(summary.get('us_date_display', ''))}, {_e(summary.get('market_session', ''))}</p></div></header>
{err}
{_stats(summary, realized)}
{body_tickers}
<section class="section"><div class="container"><h2>계좌 추이</h2>{_chart(perf)}</div></section>
<section class="section"><div class="container"><h2>최근 체결</h2>{_fills(summary.get('recent_orders') or [])}</div></section>
<section class="section"><div class="container"><h2>실현수익</h2>{_profit(cumul, tickers)}</div></section>
<section class="section"><div class="container"><h2>사이클 기록</h2>{_cycles(cycles)}</div></section>
<footer><div class="container"><p>이 문서는 봇이 저장한 상태와 증권사 조회값으로 만들었습니다. T와 잔금은 봇 기록 기준이라 증권사 실제 잔고와 어긋날 수 있으니 주기적으로 대조하세요.</p>
<p>투자 판단과 손익의 책임은 본인에게 있습니다.</p></div></footer>
</body></html>
"""


def save_report(html_text: str, now: datetime.datetime | None = None) -> Path:
    """storage/reports에 저장하고 오래된 파일은 정리한다 (최근 14개 유지)."""
    now = now or datetime.datetime.now()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report-{now.strftime('%Y%m%d-%H%M')}.html"
    path.write_text(html_text, encoding="utf-8")
    for old in sorted(REPORT_DIR.glob("report-*.html"))[:-_KEEP]:
        old.unlink(missing_ok=True)
    return path


def render_current() -> Path:
    """현재 상태로 리포트를 만들어 저장 (네트워크 조회 포함, 스레드에서 호출)."""
    import pytz
    import utils
    from trade_service import TradeService

    now = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    error = ""
    try:
        summary = TradeService.get_portfolio_summary()
    except Exception as e:  # 일부 조회가 실패해도 가능한 부분만 리포트
        summary = {"tickers": [], "recent_orders": [], "current_cash": 0.0, "total_cost": 0.0, "total_value": 0.0}
        error = f"현황 조회 중 오류가 발생했습니다: {e}"
    text = build_report_html(summary, utils.load_performance_log(), utils.load_cumul_profit(),
                             utils.load_cycle_state(), now.strftime("%Y-%m-%d %H:%M"), error)
    return save_report(text, now)
