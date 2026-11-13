"""生成自包含的离线 HTML 报告（Plotly，内嵌 plotly.js，不依赖 CDN）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .analysis import SessionData, write_merged_csv
from .storage import fmt_duration, fmt_local, local_tz

# 调色板（深色）
BG = "#0e1014"
PANEL = "#151922"
GRID = "#252a35"
TEXT = "#e7eaef"
MUTED = "#8b95a5"
C_CPU_TEMP = "#ff7a59"
C_GPU_TEMP = "#ffd166"
C_CPU_PWR = "#4dabf7"
C_GPU_PWR = "#9775fa"
C_ALL_PWR = "#ffa94d"
C_SYS_PWR = "#adb5bd"
C_ECPU = "#63e6be"
C_PCPU = "#4dabf7"
C_GPU_FREQ = "#9775fa"
C_USAGE = "#ffd166"
C_EVENT = "#f06595"
C_FPS = "#69db7c"
C_FPS_LOW = "#a9e34b"

POWER_KEYS: tuple[str, ...] = (
    "cpu_power_w",
    "gpu_power_w",
    "all_power_w",
    "sys_power_w",
    "ane_power_w",
    "ram_power_w",
)
POWER_NAMES: dict[str, str] = {
    "cpu_power_w": "CPU Power",
    "gpu_power_w": "GPU Power",
    "all_power_w": "All Power",
    "sys_power_w": "Sys Power",
    "ane_power_w": "ANE Power",
    "ram_power_w": "RAM Power",
}

LEVEL_COLORS: dict[str, str] = {
    "Nominal": "#2f9e44",
    "Moderate": "#f59f00",
    "Heavy": "#e8590c",
    "Trapping": "#c92a2a",
    "Unknown": "#868e96",
}
LEVEL_ORDER = ["Nominal", "Moderate", "Heavy", "Trapping", "Unknown"]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _fmt(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        text = f"{value:,.0f}"
    else:
        text = f"{value:.{digits}f}"
    return f"{text}{unit}"


def _series_with_gaps(
    samples: list, key: str, interval_s: float
) -> tuple[list[datetime | None], list[float | None]]:
    """缺失处插入 None，让曲线断开而不是被连成直线。"""
    xs: list[datetime | None] = []
    ys: list[float | None] = []
    prev: datetime | None = None
    threshold = interval_s * 1.8
    for sample in samples:
        ts = sample.ts.astimezone(local_tz()).replace(tzinfo=None)
        if prev is not None and (ts - prev).total_seconds() > threshold:
            xs.append(prev)
            ys.append(None)
        xs.append(ts)
        ys.append(sample.get(key))
        prev = ts
    return xs, ys


def _local_naive(dt: datetime) -> datetime:
    return dt.astimezone(local_tz()).replace(tzinfo=None)


def _thermal_missing_text(data: SessionData) -> str:
    """Thermal 面板空着时，直接说清是哪种原因缺数据，而不是一句笼统的「无数据」。"""
    import html as html_mod

    thermal_meta = (data.metadata or {}).get("thermal") or {}
    if not thermal_meta.get("enabled"):
        return (
            "<b>本次实验未采集 Thermal Pressure</b><br>"
            "<span style='font-size:11px'>录制时用了 --no-thermal。"
            "需要该指标时去掉这个参数重录，首次会询问一次 sudo 密码。</span>"
        )
    error = thermal_meta.get("error")
    if error:
        return (
            f"<b>Thermal Pressure 采集失败</b><br>"
            f"<span style='font-size:11px'>{html_mod.escape(str(error))}</span><br>"
            "<span style='font-size:11px'>可用 <code>mtl doctor --thermal</code> 单独验证授权与解析</span>"
        )
    raw = data.paths.powermetrics_raw
    if raw.exists() and raw.stat().st_size == 0:
        return (
            "<b>powermetrics 没有返回任何样本</b><br>"
            "<span style='font-size:11px'>进程已启动但输出为空，原始日志见 powermetrics.err.log</span>"
        )
    return (
        "<b>已启用 Thermal 采集，但未解析到有效样本</b><br>"
        "<span style='font-size:11px'>原始输出已完整保留在 powermetrics.log</span>"
    )


def _fps_missing_text(data: SessionData) -> str:
    """帧率面板空着时，说清是「没开 --game」还是「开了但没抓到」。"""
    import html as html_mod

    game = (data.metadata or {}).get("game") or {}
    if not game.get("enabled"):
        return (
            "<b>本次实验未采集游戏帧率</b><br>"
            "<span style='font-size:11px'>录制时没有加 --game。"
            "该参数会打开 Metal HUD 的日志开关并读取系统日志里的逐秒帧率。</span>"
        )
    error = game.get("error")
    if error:
        return (
            "<b>帧率采集失败</b><br>"
            f"<span style='font-size:11px'>{html_mod.escape(str(error))}</span><br>"
            "<span style='font-size:11px'>可用 <code>mtl doctor --game</code> 单独验证</span>"
        )
    return (
        "<b>没有捕获到 Metal HUD 的帧率数据</b><br>"
        "<span style='font-size:11px'>HUD 只对 Metal 呈现的应用生效，且游戏必须在设置环境变量<b>之后</b>启动；"
        "菜单界面等场景也可能不产生日志。原始的 log stream 输出见 hud.log</span>"
    )


# --------------------------------------------------------------------------- #
# 图表
# --------------------------------------------------------------------------- #
def build_figure(data: SessionData) -> go.Figure:
    interval_s = data.interval_s
    macmon = data.macmon
    thermal = data.thermal_sortable()
    events = data.events

    fig = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.062,
        row_heights=[0.23, 0.23, 0.13, 0.21, 0.20],
        subplot_titles=(
            "温度 Temperature",
            "功耗 Power",
            "macOS Thermal Pressure",
            "频率与负载 Frequency / Load",
            "帧率 FPS (Metal HUD)",
        ),
        specs=[[{}], [{}], [{}], [{"secondary_y": True}], [{}]],
    )

    # ---- 面板 1：温度 ----
    xs, ys = _series_with_gaps(macmon, "cpu_temp_c", interval_s)
    fig.add_trace(
        go.Scatter(
            x=xs, y=ys, name="CPU 温度 (°C)", mode="lines",
            line=dict(color=C_CPU_TEMP, width=1.8),
            hovertemplate="CPU %{y:.1f} °C<extra></extra>", connectgaps=False,
        ),
        row=1, col=1,
    )
    xs, ys = _series_with_gaps(macmon, "gpu_temp_c", interval_s)
    fig.add_trace(
        go.Scatter(
            x=xs, y=ys, name="GPU 温度 (°C)", mode="lines",
            line=dict(color=C_GPU_TEMP, width=1.8),
            hovertemplate="GPU %{y:.1f} °C<extra></extra>", connectgaps=False,
        ),
        row=1, col=1,
    )
    fig.update_yaxes(title_text="°C", row=1, col=1, gridcolor=GRID, zeroline=False)

    # 为顶部事件标签预留垂直空间（用数据坐标，避免依赖 domain 引用）
    temps_all = [
        t for s in macmon for t in (s.get("cpu_temp_c"), s.get("gpu_temp_c")) if t is not None
    ]
    label_ys: list[float] = []
    if temps_all:
        lo, hi = min(temps_all), max(temps_all)
        extent = max(5.0, hi - lo)
        top = hi + extent * 0.26
        fig.update_yaxes(range=[lo - extent * 0.06, top], row=1, col=1)
        label_ys = [top - extent * 0.05, top - extent * 0.19]

    # ---- 面板 2：功耗 ----
    power_style = {
        "cpu_power_w": (C_CPU_PWR, "solid", 1.8),
        "gpu_power_w": (C_GPU_PWR, "solid", 1.8),
        "all_power_w": (C_ALL_PWR, "solid", 2.2),
        "sys_power_w": (C_SYS_PWR, "dot", 1.4),
    }
    for key, (color, dash, width) in power_style.items():
        xs, ys = _series_with_gaps(macmon, key, interval_s)
        fig.add_trace(
            go.Scatter(
                x=xs, y=ys, name=POWER_NAMES[key], mode="lines",
                line=dict(color=color, width=width, dash=dash),
                hovertemplate=f"{POWER_NAMES[key]} %{{y:.2f}} W<extra></extra>",
                connectgaps=False,
            ),
            row=2, col=1,
        )
    fig.update_yaxes(title_text="W", row=2, col=1, gridcolor=GRID, zeroline=False)

    # ---- 面板 3：Thermal Pressure（色带 + 阶梯线）----
    if thermal:
        for index, sample in enumerate(thermal):
            start = _local_naive(sample.ts)
            end = (
                _local_naive(thermal[index + 1].ts)
                if index + 1 < len(thermal)
                else _local_naive(data.ended_at or sample.ts)
            )
            if end <= start:
                continue
            fig.add_vrect(
                x0=start, x1=end,
                fillcolor=LEVEL_COLORS.get(sample.level, LEVEL_COLORS["Unknown"]),
                opacity=0.30, line_width=0, layer="below", row=3, col=1,
            )
        fig.add_trace(
            go.Scatter(
                x=[_local_naive(s.ts) for s in thermal],
                y=[s.rank for s in thermal],
                name="Thermal Pressure", mode="lines",
                line=dict(color="#f8f9fa", width=1.2, shape="hv"),
                customdata=[[s.level, s.raw_level] for s in thermal],
                hovertemplate="%{customdata[0]}<extra></extra>",
                connectgaps=False,
            ),
            row=3, col=1,
        )
    fig.update_yaxes(
        title_text="状态", row=3, col=1,
        tickmode="array", tickvals=[0, 1, 2, 3, 4],
        ticktext=LEVEL_ORDER, range=[-0.6, 4.6],
        gridcolor=GRID, zeroline=False,
    )

    # ---- 面板 4：频率与负载 ----
    for key, color, label in (
        ("ecpu_freq_mhz", C_ECPU, "E 核频率 (MHz)"),
        ("pcpu_freq_mhz", C_PCPU, "S 核频率 (MHz)"),
        ("gpu_freq_mhz", C_GPU_FREQ, "GPU 频率 (MHz)"),
    ):
        xs, ys = _series_with_gaps(macmon, key, interval_s)
        fig.add_trace(
            go.Scatter(
                x=xs, y=ys, name=label, mode="lines",
                line=dict(color=color, width=1.5),
                hovertemplate=f"{label} %{{y:.0f}}<extra></extra>",
                connectgaps=False,
            ),
            row=4, col=1, secondary_y=False,
        )
    for key, color, label in (
        ("cpu_usage_pct", C_USAGE, "CPU 使用率 (%)"),
        ("gpu_usage_pct", "#f783ac", "GPU 使用率 (%)"),
    ):
        xs, ys = _series_with_gaps(macmon, key, interval_s)
        fig.add_trace(
            go.Scatter(
                x=xs, y=ys, name=label, mode="lines",
                line=dict(color=color, width=1.2, dash="dot"),
                hovertemplate=f"{label} %{{y:.0f}}%<extra></extra>",
                connectgaps=False,
            ),
            row=4, col=1, secondary_y=True,
        )
    fig.update_yaxes(title_text="MHz", row=4, col=1, secondary_y=False, gridcolor=GRID)
    fig.update_yaxes(
        title_text="%", row=4, col=1, secondary_y=True, range=[0, 105],
        showgrid=False, zeroline=False,
    )

    # ---- 面板 5：帧率（Metal HUD）----
    fps_samples = data.fps
    if fps_samples:
        xs = [_local_naive(sample["ts"]) for sample in fps_samples]
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=[sample.get("fps") for sample in fps_samples],
                name="帧率 (FPS)", mode="lines",
                line=dict(color=C_FPS, width=1.8),
                customdata=[[sample.get("process") or "-",
                             sample.get("interval_ms"),
                             sample.get("gpu_ms")] for sample in fps_samples],
                hovertemplate=(
                    "%{y:.1f} FPS<br>帧间隔 %{customdata[1]:.2f} ms<br>"
                    "GPU %{customdata[2]:.2f} ms<extra>%{customdata[0]}</extra>"
                ),
                connectgaps=False,
            ),
            row=5, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=[sample.get("fps_1pct") for sample in fps_samples],
                name="1% low (FPS)", mode="lines",
                line=dict(color=C_FPS_LOW, width=1.2, dash="dot"),
                hovertemplate="1% low %{y:.1f} FPS<extra></extra>",
                connectgaps=False,
            ),
            row=5, col=1,
        )
        fps_values = [s["fps"] for s in fps_samples if s.get("fps") is not None]
        if fps_values:
            top = max(fps_values) * 1.12
            fig.update_yaxes(range=[0, top], row=5, col=1)
    fig.update_yaxes(title_text="FPS", row=5, col=1, gridcolor=GRID, zeroline=False)

    if not fps_samples:
        fig.add_annotation(
            x=0.5, y=0.11, xref="paper", yref="paper", showarrow=False,
            text=_fps_missing_text(data),
            align="center",
            font=dict(color=MUTED, size=13),
        )

    # ---- 事件竖线 ----
    span_s = 1.0
    if len(macmon) >= 2:
        span_s = max(1.0, (macmon[-1].ts - macmon[0].ts).total_seconds())
    elif thermal:
        span_s = max(1.0, (thermal[-1].ts - thermal[0].ts).total_seconds())
    elif len(data.fps) >= 2:
        span_s = max(1.0, (data.fps[-1]["ts"] - data.fps[0]["ts"]).total_seconds())
    prev_frac: float | None = None
    level = 0
    for index, event in enumerate(events):
        x = _local_naive(event.ts)
        fig.add_vline(
            x=x, line=dict(color=C_EVENT, width=1.2, dash="dash"),
            row="all", col=1, opacity=0.85,
        )
        if index >= 6 or not macmon or not label_ys:
            continue
        frac = (event.ts - macmon[0].ts).total_seconds() / span_s
        if prev_frac is not None and frac - prev_frac < 0.05:
            level = 1  # 挨得太近就下一行，避免文字叠住
        else:
            level = 0
        prev_frac = frac
        label = event.event if len(event.event) <= 14 else event.event[:13] + "…"
        fig.add_annotation(
            x=x, y=label_ys[min(level, len(label_ys) - 1)], xref="x", yref="y",
            xanchor="left", yanchor="top", xshift=4,
            text=label, showarrow=False,
            font=dict(size=10, color="#ffd7e6"),
            bgcolor="rgba(14,16,20,0.72)",
            bordercolor=C_EVENT, borderwidth=1, borderpad=2,
        )

    if events:
        # 便于悬停查看事件的隐形锚点（放在面板顶部，不压曲线）
        y_anchor = label_ys[0] if label_ys else 0
        fig.add_trace(
            go.Scatter(
                x=[_local_naive(e.ts) for e in events],
                y=[y_anchor for _ in events],
                mode="markers",
                marker=dict(size=9, symbol="diamond", color="rgba(240,101,149,0.001)"),
                name="事件",
                text=[
                    f"{e.event}{('（' + e.note + '）') if e.note else ''}<br>"
                    f"{fmt_local(e.ts)}"
                    for e in events
                ],
                hovertemplate="%{text}<extra></extra>",
                showlegend=False,
            ),
            row=1, col=1,
        )

    if not thermal:
        # 第 3 行（Thermal）在 paper 坐标里大约 0.40~0.51
        fig.add_annotation(
            x=0.5, y=0.455, xref="paper", yref="paper", showarrow=False,
            text=_thermal_missing_text(data),
            align="center",
            font=dict(color=MUTED, size=13),
        )

    fig.update_layout(
        template=None,
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(family='-apple-system, "SF Pro Text", "PingFang SC", sans-serif',
                  color=TEXT, size=12),
        height=1240,
        margin=dict(l=72, r=72, t=120, b=64),
        hovermode="x unified",
        dragmode="pan",
        legend=dict(
            orientation="h", yanchor="bottom", y=1.045, x=0,
            bgcolor="rgba(0,0,0,0)", font=dict(size=11),
        ),
        bargap=0,
    )
    fig.update_xaxes(
        gridcolor=GRID, showspikes=True, spikemode="across", spikethickness=1,
        spikecolor=MUTED, spikedash="dot", rangeslider_visible=False,
    )
    fig.update_xaxes(title_text="本机时间", row=5, col=1)
    return fig


# --------------------------------------------------------------------------- #
# 静态片段
# --------------------------------------------------------------------------- #
CSS = """
:root {
  --bg: #0e1014;
  --panel: #151922;
  --panel-2: #1b2029;
  --border: #262c38;
  --text: #e7eaef;
  --muted: #8b95a5;
  --accent: #4dabf7;
  --warn: #ffa94d;
  --danger: #ff6b6b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1440px; margin: 0 auto; padding: 32px 28px 64px; }
header { margin-bottom: 22px; }
h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: -0.2px; }
.sub { color: var(--muted); font-size: 13px; line-height: 1.7; }
.sub code {
  background: var(--panel-2); border: 1px solid var(--border);
  padding: 1px 6px; border-radius: 5px; font-size: 12px;
}
.kpis {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
  gap: 12px; margin: 20px 0 26px;
}
.kpi {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px;
}
.kpi .label { color: var(--muted); font-size: 11px; letter-spacing: .4px; text-transform: uppercase; }
.kpi .value { font-size: 21px; font-weight: 600; margin-top: 6px; font-variant-numeric: tabular-nums; }
.kpi .hint { color: var(--muted); font-size: 11px; margin-top: 3px; }
.card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; padding: 18px 20px; margin-bottom: 18px;
}
.card h2 { font-size: 14px; margin: 0 0 14px; font-weight: 600; letter-spacing: .3px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border); }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .3px; }
tbody tr:last-child td { border-bottom: none; }
td.num { font-variant-numeric: tabular-nums; }
.tag {
  display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 11px; border: 1px solid var(--border);
}
.warn {
  background: rgba(255,169,77,.08); border: 1px solid rgba(255,169,77,.35);
  color: #ffd8a8; border-radius: 12px; padding: 14px 18px; margin-bottom: 18px; font-size: 12.5px;
}
.warn ul { margin: 8px 0 0; padding-left: 20px; }
.warn li { margin: 3px 0; }
.notes { color: var(--muted); font-size: 12px; line-height: 1.85; }
.notes b { color: var(--text); font-weight: 600; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: middle; }
footer { color: var(--muted); font-size: 11px; margin-top: 26px; text-align: center; }
.plotly .modebar { background: transparent !important; }
.js-plotly-plot .plotly .modebar-btn path { fill: #8b95a5 !important; }
"""


def _clean_version(value: object) -> str:
    """macmon --version 输出形如 `macmon 0.11.0`，展示时去掉重复前缀。"""
    text = str(value or "").strip()
    return text[len("macmon ") :] if text.lower().startswith("macmon ") else (text or "?")


def _kpi(label: str, value: str, hint: str = "") -> str:
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    return f'<div class="kpi"><div class="label">{label}</div><div class="value">{value}</div>{hint_html}</div>'


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="num">{c}</td>' for c in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _range_text(summary: dict[str, Any]) -> str:
    start = summary.get("started_at")
    end = summary.get("ended_at")
    if not start or not end:
        return "时间范围未知"
    return f"{fmt_local(start)} → {fmt_local(end)}"


def render_html(data: SessionData, summary: dict[str, Any], plot_html: str) -> str:
    meta = data.metadata
    temps = summary["temperatures"]
    power = summary["power"]
    thermal = summary["thermal"]
    integrity = summary["integrity"]
    soc = meta.get("soc") or {}
    host = meta.get("host") or {}

    cpu_t = temps.get("cpu") or {}
    gpu_t = temps.get("gpu") or {}
    all_p = power.get("all_power_w") or {}
    cpu_p = power.get("cpu_power_w") or {}
    gpu_p = power.get("gpu_power_w") or {}

    dominant = "-"
    if thermal.get("by_level"):
        dominant = max(thermal["by_level"].items(), key=lambda kv: kv[1])[0]

    kpi_items = [
        _kpi("实验时长", fmt_duration(summary.get("span_seconds")),
             f"采样间隔 {summary['interval_ms']/1000:.1f}s"),
        _kpi("CPU 温度", _fmt(cpu_t.get("mean"), 1, " °C"),
             f"最高 {_fmt(cpu_t.get('max'), 1, ' °C')} / 最低 {_fmt(cpu_t.get('min'), 1, ' °C')}"),
        _kpi("GPU 温度", _fmt(gpu_t.get("mean"), 1, " °C"),
             f"最高 {_fmt(gpu_t.get('max'), 1, ' °C')}"),
        _kpi("CPU 功耗", _fmt(cpu_p.get("mean"), 2, " W"),
             f"峰值 {_fmt(cpu_p.get('max'), 2, ' W')}"),
        _kpi("GPU 功耗", _fmt(gpu_p.get("mean"), 2, " W"),
             f"峰值 {_fmt(gpu_p.get('max'), 2, ' W')}"),
        _kpi("All Power", _fmt(all_p.get("mean"), 2, " W"),
             f"能量 ≈ {_fmt(all_p.get('energy_wh'), 2, ' Wh')}"),
        _kpi("Thermal 主导状态", dominant,
             f"切换 {thermal.get('changes', 0)} 次"),
        _kpi("采样点", f"{integrity['macmon']['count']}",
             f"缺失 {_fmt(integrity['macmon'].get('missing_pct'), 1, '%')}"),
    ]
    fps_summary = summary.get("fps") or {}
    if fps_summary.get("fps_samples"):
        kpi_items.insert(
            6,
            _kpi("平均帧率", _fmt(fps_summary.get("fps_avg"), 1, " FPS"),
                 f"1% low {_fmt(fps_summary.get('fps_1pct_low'), 1)} · "
                 f"最低 {_fmt(fps_summary.get('fps_min'), 1)}"),
        )
    kpis = "".join(kpi_items)

    # 温度表
    def temp_row(label: str, stats: dict[str, float] | None) -> list[str]:
        if not stats:
            return [label, "-", "-", "-", "-"]
        return [
            label,
            _fmt(stats.get("min"), 1, " °C"),
            _fmt(stats.get("mean"), 1, " °C"),
            _fmt(stats.get("max"), 1, " °C"),
            _fmt(stats.get("p95"), 1, " °C"),
        ]

    temp_table = _table(
        ["指标", "最低", "平均", "最高", "P95"],
        [temp_row("CPU", temps.get("cpu")), temp_row("GPU", temps.get("gpu"))],
    )

    power_rows = []
    for key in POWER_KEYS:
        stats = power.get(key)
        if not stats:
            power_rows.append([POWER_NAMES[key], "-", "-", "-", "-"])
            continue
        power_rows.append(
            [
                POWER_NAMES[key],
                _fmt(stats.get("mean"), 2, " W"),
                _fmt(stats.get("max"), 2, " W"),
                _fmt(stats.get("p95"), 2, " W"),
                _fmt(stats.get("energy_wh"), 2, " Wh"),
            ]
        )
    power_table = _table(["指标", "平均", "峰值", "P95", "估计能量"], power_rows)

    # thermal 表
    total_thermal = thermal.get("total_s") or 0.0
    thermal_rows = []
    raw_labels = thermal.get("by_raw") or {}
    for level in LEVEL_ORDER:
        dur = thermal.get("by_level", {}).get(level, 0.0)
        if not dur:
            continue
        pct = 100.0 * dur / total_thermal if total_thermal else 0.0
        color = LEVEL_COLORS.get(level, MUTED)
        name = f'<span class="swatch" style="background:{color}"></span>{level}'
        if level == "Unknown" and raw_labels:
            detail = "、".join(f"{k}" for k in raw_labels)
            name += f' <span style="color:{MUTED};font-size:11px">（{detail}）</span>'
        thermal_rows.append([name, fmt_duration(dur), f"{pct:.1f}%"])
    if not thermal_rows:
        thermal_rows = [["无数据", "-", "-"]]
    thermal_table = _table(["状态", "累计时长", "占比"], thermal_rows)

    # 采样完整性
    integrity_rows = []
    for key, label in (("macmon", "macmon"), ("thermal", "powermetrics")):
        info = integrity[key]
        integrity_rows.append(
            [
                label,
                str(info.get("count", 0)),
                str(info.get("expected", 0) or "-"),
                str(info.get("missing", 0)),
                _fmt(info.get("missing_pct"), 1, "%"),
                _fmt(info.get("longest_gap_s"), 1, " s"),
            ]
        )
    integrity_table = _table(
        ["数据源", "实际样本", "理论样本", "缺失", "缺失率", "最长空档"], integrity_rows
    )

    # 事件
    if data.events:
        event_rows = [
            [
                fmt_local(e.ts, "%H:%M:%S"),
                f"{fmt_duration(e.elapsed_seconds)}",
                e.event,
                e.note or "-",
            ]
            for e in data.events
        ]
        event_table = _table(["时间", "距开始", "事件", "备注"], event_rows)
    else:
        event_table = '<div class="notes">本次实验没有人工事件。用 <code>mtl mark "事件名"</code> 可在采集中添加。</div>'

    warnings = summary.get("warnings") or []
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = f'<div class="warn"><b>需要注意</b><ul>{items}</ul></div>'

    chips = []
    if soc.get("chip_name"):
        chips.append(soc["chip_name"])
    if soc.get("mac_model"):
        chips.append(soc["mac_model"])
    if soc.get("ecpu_cores") and soc.get("pcpu_cores"):
        chips.append(f"{soc['ecpu_cores']}E + {soc['pcpu_cores']}S 核")
    if soc.get("gpu_cores"):
        chips.append(f"{soc['gpu_cores']} 核 GPU")
    if soc.get("memory_gb"):
        chips.append(f"{soc['memory_gb']} GB")
    host_chips = " · ".join(chips) if chips else "SoC 信息未采集"

    tools = meta.get("tools") or {}
    thermal_meta = meta.get("thermal") or {}
    thermal_state = "已启用" if thermal_meta.get("enabled") else "未启用"
    if thermal_meta.get("error"):
        thermal_state += f"（{thermal_meta['error']}）"

    game_state = ""
    game_meta_top = meta.get("game") or {}
    if game_meta_top.get("enabled"):
        process = game_meta_top.get("process")
        game_state = f' · 帧率采集 已启用{"（" + process + "）" if process else ""}'
        if game_meta_top.get("error"):
            game_state = f" · 帧率采集 失败（{game_meta_top['error']}）"

    sub = (
        f'{host_chips} · macOS {host.get("os_version", "?")} · 本机时间 {_range_text(summary)}<br>'
        f'会话 <code>{data.paths.name}</code> · 采样间隔 '
        f'<code>{summary["interval_ms"]/1000:.1f}s</code> · '
        f'macmon <code>{_clean_version(tools.get("macmon"))}</code> · '
        f'Thermal 采集 {thermal_state}{game_state}'
    )

    notes = """
<div class="notes">
  <b>单位与口径</b>：温度 °C；功耗 W（macmon 原值，未做单位换算）；频率 MHz。
  <b>All Power</b> 是 macmon 的独立整体指标，不等于 CPU+GPU 功耗之和，图中不做叠加。
  <b>Sys Power</b> 为 SoC 侧系统功率，数值可能与 All Power 不同，仅作参考。<br>
  <b>温度口径</b>：macmon 提供的是 CPU/GPU 传感器平均值，不是最热点温度，绝对值偏低属正常。<br>
  <b>时间对齐</b>：macmon 与 powermetrics 是两个独立进程，图上按各自的真实时间戳对齐；
  Thermal 面板的色带只覆盖真实采样区间。merged.csv 中的 <code>thermal_pressure</code>
  为 as-of 取值（取不晚于该行的最近一次真实样本），其年龄见 <code>thermal_pressure_age_s</code>，
  <code>thermal_pressure_raw</code> 列只在 powermetrics 真实样本行上出现。<br>
  <b>缺失</b>：曲线中的断口即采样缺失，未做插值或补点。
</div>
"""

    fps_card = ""
    if fps_summary.get("fps_samples"):
        fps_rows = [
            ["平均帧率", _fmt(fps_summary.get("fps_avg"), 1, " FPS")],
            ["1% low", _fmt(fps_summary.get("fps_1pct_low"), 1, " FPS")],
            ["最低 / 最高", f"{_fmt(fps_summary.get('fps_min'), 1)} / "
                            f"{_fmt(fps_summary.get('fps_max'), 1)} FPS"],
            ["识别进程", "、".join(fps_summary.get("processes") or []) or "-"],
            ["采样条数", str(fps_summary.get("fps_samples"))],
        ]
        fps_card = (
            '<div class="card"><h2>帧率统计（Metal HUD）</h2>'
            + _table(["指标", "数值"], fps_rows)
            + "</div>"
        )
    game_meta = meta.get("game") or {}

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mac Thermal Lab · {summary['name']}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{summary['name']}</h1>
    <div class="sub">{sub}</div>
  </header>
  <div class="kpis">{kpis}</div>
  {warn_html}
  <div class="card" style="padding:8px 8px 0">
    {plot_html}
  </div>
  <div class="grid2">
    <div class="card"><h2>温度统计</h2>{temp_table}</div>
    <div class="card"><h2>功耗统计</h2>{power_table}</div>
  </div>
  {fps_card}
  <div class="grid2">
    <div class="card"><h2>Thermal Pressure 分布</h2>{thermal_table}</div>
    <div class="card"><h2>采样完整性</h2>{integrity_table}</div>
  </div>
  <div class="card"><h2>人工事件</h2>{event_table}</div>
  <div class="card"><h2>说明</h2>{notes}</div>
  <footer>Mac Thermal Lab · {fmt_local(summary.get('ended_at'))} 生成 · 完全离线自包含</footer>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def generate_report(paths, figure: go.Figure | None = None) -> Path:
    from .analysis import load_session, summary as build_summary

    data = load_session(paths)
    summary = build_summary(data)
    if figure is None:
        figure = build_figure(data)

    plot_html = figure.to_html(
        full_html=False,
        include_plotlyjs=True,
        div_id="mtl-plot",
        config={
            "responsive": True,
            "displaylogo": False,
            "scrollZoom": True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "toImageButtonOptions": {"format": "png", "scale": 2, "filename": paths.name},
        },
    )
    html = render_html(data, summary, plot_html)
    tmp = paths.dashboard.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(paths.dashboard)
    write_merged_csv(data)
    return paths.dashboard
