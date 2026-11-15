# Mac Thermal Lab

macOS Apple Silicon 本地热状态与功耗实验记录工具。命令行入口 `mtl`。

它做一件事：**把 macmon 的硬件指标和 macOS 的 Thermal Pressure 按真实时间戳记在同一根时间轴上**，
结束后生成一张可离线打开、可缩放悬停的 HTML 报告。适合做「游戏续航 / 烤机散热 / 外置风扇对比」这类实验。

- 纯本地，不联网、不上传、不建数据库。
- 原始数据一律保留（JSONL / 原始日志），报告随时可重新生成。
- 报告自包含（plotly.js 内嵌），断网可用。

目标设备：MacBook Air M5 / 16GB / macOS 26，同样适用于 M1~M5 系列。

---

## 安装

本项目**只用 [uv](https://docs.astral.sh/uv/)** 管理：不提供 `requirements.txt`，
不支持 `pip install`，也不需要手动 `python -m venv`。依赖、Python 版本、锁文件、
全局命令安装全部由 uv 负责。

```bash
git clone https://github.com/Jeasin/MacThermalLab.git ~/.local/src/MacThermalLab
cd ~/.local/src/MacThermalLab
uv sync                                  # 按 uv.lock 建环境（含 Python 3.13）
uv tool install --editable .             # 把 mtl 装进 ~/.local/bin
```

`--editable` 让你既能像普通命令一样用 `mtl`，改源码又立即生效（不必重装）。
只想跑一次、不想装到全局，可以用 `uv run mtl ...`。
卸载：`uv tool uninstall mac-thermal-lab`。

依赖：`brew install macmon`。`powermetrics` 系统自带，但需要 root。

> 已经装在 `~/.local/src/MacThermalLab` 的旧副本，直接 `git init` + 关联远程也一样能用，
> 项目对源码位置没有假设。

安装后确认环境：

```bash
mtl doctor            # 检查 macmon / powermetrics / plotly / 数据目录
mtl doctor --thermal  # 实跑一次 powermetrics，验证 Thermal 解析（会要 sudo）
```

---

## 快速开始

```bash
mtl start 游戏测试          # 开始采集（默认 2s 一次）
mtl mark 游戏启动            # 打一个事件标记（不用加引号）
mtl mark 外接电源断开
mtl mark 桌面风扇 开到最大    # 中间有空格也行，会拼成一句
mtl status                  # 看实时状态
mtl stop                    # 结束，生成报告，返回路径
```

要连帧率一起记，加 `--game`（详见[帧率](#帧率游戏-fps)）：

```bash
mtl start 王国之泪 --game          # 自动识别进程
mtl start 王国之泪 --game Ryujinx  # 或指定进程名
```

`mtl stop` 之后直接用浏览器打开打印出来的 `dashboard.html`，或：

```bash
mtl report --open           # 重新生成最近一次实验的报告并打开
```

---

## 命令

| 命令 | 说明 |
| --- | --- |
| `mtl start [名称] [--game [进程]]` | 开始新实验。名称留空则用时间戳 |
| `mtl stop [会话] [--force]` | 停止采集、整理数据、生成报告 |
| `mtl status [会话]` | 采集状态、时长、样本数、子进程、已落盘大小 |
| `mtl mark 事件 [-n 备注]` | 在当前实验时间轴加事件（空格不必加引号） |
| `mtl report [会话] [--open]` | 重新生成 HTML 报告 |
| `mtl recover-fps [会话] [--process 名] [--force]` | 从系统日志历史补回帧率（过滤词写错时救场） |
| `mtl clean [会话...] [--dry-run] [--purge] [--restore]` | 原始日志挪去缓存，只留能重建报告的数据 |
| `mtl list` | 列出历史实验（含 HUD 进程名） |
| `mtl doctor [--thermal] [--game]` | 环境自检；`--thermal` 实跑 powermetrics，`--game` 实测帧率链路 |
| `mtl paths [--init-config]` | 显示 XDG 目录布局；`--init-config` 写配置模板 |
| `mtl migrate [--dry-run]` | 把旧目录里的实验迁到 XDG 数据目录 |

常用选项：

```bash
mtl start 测试 -i 1000          # 采样间隔 1s（也接受 500ms / 2s，默认 2000ms）
mtl start 测试 --no-thermal     # 不采集 Thermal Pressure，完全免 sudo
mtl start 测试 --game           # 同时记录游戏帧率（Metal HUD）
mtl --data-dir ~/thermal start  # 临时换数据目录（也可 export MTL_DATA_DIR）
```

规则：

- 同一时间只允许一个活动实验，重复 `start` 会被拒绝。
- `stop` 只终止自己启动的进程（按 PID + 进程组校验），不会 `pkill` 掉你其它的监控进程。
- **`stop` 不会认错会话**：带了名字但指到别的（比如已经结束的）会话时，直接拒绝并告诉你当前在跑的是哪个；
  只重出报告请用 `mtl report <会话>`。已经结束的会话要再收尾一次得显式加 `--force`。
- 终端意外关闭或进程被杀，原始数据仍在；`mtl status` 会提示，`mtl stop` 可从已有数据补出报告。

---

## 数据布局（XDG）

应用标识 `mac-thermal-lab`，源码在 `~/.local/src/MacThermalLab` 只放代码，**数据不在仓库里**。

| 用途 | 路径 | 内容 |
| --- | --- | --- |
| 配置 | `$XDG_CONFIG_HOME/mac-thermal-lab/config.toml` | 可选的 `recordings_dir` 等 |
| 实验数据 | `$XDG_DATA_HOME/mac-thermal-lab/recordings/<会话>/` | 原始日志、CSV、metadata、events、报告 |
| 运行状态 | `$XDG_STATE_HOME/mac-thermal-lab/sessions/<会话>/` | `state.json`、`collector.log` |
| 运行期 | `$XDG_RUNTIME_DIR/mac-thermal-lab/` | `<会话>.lock`、`<会话>.pid` |
| 缓存 | `$XDG_CACHE_HOME/mac-thermal-lab/` | 预留，实验数据不会放这里 |

未设置环境变量时的默认值：`~/.config`、`~/.local/share`、`~/.local/state`、`~/.cache`。
macOS 一般不设 `XDG_RUNTIME_DIR`，此时回退到 `$TMPDIR/mac-thermal-lab-runtime-<uid>`（权限 0700）。

查看实际生效的路径：

```bash
mtl paths
```

### 单个实验目录

```
~/.local/share/mac-thermal-lab/recordings/2026-11-11_195302_游戏测试/
├── metadata.json              实验元数据（设备、SoC、间隔、状态、错误）
├── macmon.jsonl               原始输出，一行一个样本，未改动
├── metrics.jsonl              精简版 macmon 采样（mtl clean 生成，字段一致、体积约 1/4）
├── powermetrics.log           原始输出，逐行未改动
├── powermetrics.err.log       powermetrics 的 stderr
├── powermetrics.samples.jsonl 采集时顺带解析的缓存（用于补缺失时间戳）
├── hud.log                    log stream 的原始 ndjson（仅在 --game 时产生）
├── hud.err.log                log stream 的 stderr
├── fps.jsonl                  帧率样本（fps / 窗口帧数 / 平均帧间隔 / 内存 MB）
├── events.jsonl               人工事件
├── merged.csv                 三路数据按时间戳对齐后的并集
└── dashboard.html             离线可交互报告（自包含：plotly 与数据都内嵌）
```

### 体积与归档

两小时的游戏实测能到 120 MB，大头是原始日志：`macmon.jsonl` 每行都重发一份
SoC 频率表和逐核数组，`hud.log` 每行还带 `backtrace` 之类的日志元数据。

```bash
mtl clean                    # 精简 macmon → 刷新报告 → 原始日志挪去缓存
mtl clean --dry-run          # 只看能省多少
mtl clean --purge            # 删掉缓存里的原始归档
mtl clean --restore          # 把原始日志搬回会话目录
```

归档落在 `~/.cache/mac-thermal-lab/raw/<会话>/`（缓存目录，系统可回收）。
会话目录只留 `dashboard.html` + 精简数据，`mtl report` 仍能重建报告，
因为 `metrics.jsonl` 与 `macmon.jsonl` 字段完全一致、数值逐位相同。

`dashboard.html` 是自包含的：plotly 与所有曲线数据都内嵌在这一个文件里，
没有外部引用。只想留报告的话，`mtl clean --purge` 之后把这个 html 单独拷走也能看。

`state.json` / `collector.log` / 锁文件不在实验目录里，分别在 STATE 与 RUNTIME 下，
所以把整个 recordings 目录拷走或挂到网盘，不会带上运行期垃圾。

### 自定义数据目录

```bash
mtl paths --init-config      # 生成 ~/.config/mac-thermal-lab/config.toml
```

```toml
# 支持 ~ 与环境变量；相对路径相对于该配置文件所在目录
recordings_dir = "~/Documents/MacThermalLab"
```

优先级：`--data-dir` > `MTL_DATA_DIR` > `config.toml` > XDG 默认。

### 从旧目录迁移

旧版本把数据放在源码目录下（`~/.local/src/MacThermalLab/recordings`）。迁移是显式命令，
不会偷偷搬动文件：

```bash
mtl migrate --dry-run   # 先看会迁移哪些
mtl migrate             # 执行
```

安全性：

- 目标已有同名会话 → 跳过，**绝不覆盖**。
- 先整目录复制到 `.migrating-*` 临时目录，校验文件数与字节总数一致后才改名落位。
- 校验通过才删除源目录；校验失败则源数据原样保留。
- 旧目录里还有别的东西（含 `.DS_Store` 之外的未识别文件）时，源目录保留不删。

迁移前 `mtl list` 会把旧目录里的实验也列出来（标 `(旧目录)`），`mtl report` 同样能读到。

### macmon.jsonl

就是 `macmon pipe -s 0 -i <ms> --soc-info` 的原样输出，一行一个 JSON。
报告阶段重新解析它，所以**改解析逻辑后老数据也能重新出报告**。

关键字段（macmon 0.11.0 实测）：

| 字段 | 含义 | 单位 |
| --- | --- | --- |
| `temp.cpu_temp_avg` / `temp.gpu_temp_avg` | CPU/GPU 传感器**平均**温度 | °C |
| `cpu_power` / `gpu_power` / `all_power` | CPU / GPU / 整体功耗 | W |
| `sys_power` | SoC 侧系统功率（**不等于** `all_power`） | W |
| `ane_power` / `ram_power` / `gpu_ram_power` | ANE / 内存 / GPU 显存功耗 | W |
| `ecpu_freq_mhz` / `pcpu_freq_mhz` | E 核 / S 核频率（**S 核前缀是 `pcpu_`**） | MHz |
| `gpu_freq_mhz` | GPU 频率 | MHz |
| `cpu_scaled_ratio` / `gpu_active_ratio` | 使用率，0~1 比值 | — |
| `memory.*` | 内存压力、RAM/Swap 使用量 | 字节 |
| `timestamp` | 采样时刻，**UTC**，带偏移 | ISO 8601 |
| `soc.*` | 芯片型号、核心数等（只取一次写入 metadata） | — |

### merged.csv 的列

并集时间轴：macmon 的每一行 + powermetrics 的每一行各自成行，按 `row_ts_utc` 排序。

| 列 | 说明 |
| --- | --- |
| `row_ts_utc` / `row_ts_local` / `elapsed_s` | 该行的真实时刻与距实验开始的秒数 |
| `source` | `macmon` 或 `powermetrics` |
| `cpu_temp_c` … `swap_used_gb` | 规范化后的 macmon 指标（非该源的行留空） |
| `thermal_pressure` | as-of 取值：不晚于该行的最近一次真实 Thermal 样本 |
| `thermal_pressure_raw` | **仅**在 powermetrics 真实样本行上出现原始状态文本 |
| `thermal_pressure_age_s` | as-of 数据年龄，超龄时留空（默认阈值为 4 倍采样间隔或 10s） |
| `fps` | as-of 取向最近一条 Metal HUD 帧率；`source=metal-hud` 的行是本行自己的值 |
| `fps_1pct` | 1% low；逐帧间隔拿不到时留空（汇总层用窗口近似，见下） |
| `fps_age_s` | 帧率的 as-of 年龄（HUD 每秒一条，阈值默认为 2 倍采样间隔或 4s） |
| `game_process` | 该帧率来自哪个进程（HUD 日志里的进程名） |

`thermal_pressure` 是「取最近一次已知状态」，不是插值，也不会凭空造样本；
判断可信度请一并看 `thermal_pressure_age_s`。

---

## 帧率（游戏 FPS）

加 `--game` 后，`mtl` 会打开 macOS 自带的 **Metal Performance HUD 日志**，
再把系统日志里的逐秒帧率抓下来，和温度/功耗放到同一根时间轴上。

```bash
mtl start 王国之泪 --game                  # 自动识别是哪个进程
mtl start 王国之泪 --game OcarinaOfTime    # 指定进程名（子串匹配，忽略大小写/空格/标点）
```

`--game` 的值只是**过滤词**，不是系统日志的硬条件：匹配在程序里做，写错了也
还能看到「其实有别的进程在写 HUD」，并且攒够 3 行就会自动改用那个进程，
metadata 里记为 `filter_fallback`，`mtl stop` 和报告都会写明白。

原理：Apple 的 HUD 在 `MTL_HUD_ENABLED=1` 时显示浮层，再加上
`MTL_HUD_LOG_ENABLED=1` 后，每秒往统一日志写一行

```
metal-HUD: 首帧号,预计丢帧,进程内存MB,逐帧(间隔ms,GPU时间ms)…
```

`mtl` 用 `log stream` 读这一行并解析成 `fps.jsonl`——不需要录屏权限，也不做 OCR。

两个实测坑（已在代码里处理）：

- 行尾那段逐帧明细是一个 `%s` 参数，系统日志会把它整段脱敏成 `<private>`，
  所以逐帧间隔通常拿不到。帧率改由**帧号差分**算：两条日志的帧号差 ÷ 真实时间差，
  一个点对应一个采样窗口（约 0.5s，含 20–60 帧）。
  逐帧间隔缺失时 `1% low` 退化为「按帧数加权的最慢 1% 帧」近似值，报告里会标「≈」。
- `log stream` 会把同一条记录投递**两份**（一份 `subsystem` 为空、一份
  `com.apple.metal.hud`）。解析器按帧号去重，否则每秒会多一条算不出帧率的空记录，
  报告里的折线会被空值切成碎片、一条线都看不见。
- 早期版本把 `--game` 的词拼进日志谓词（`processImagePath CONTAINS "ott"`），
  词写错就是**一行都收不到**，而且看不出为什么——1114 那次 41 分钟的录制
  就这样白跑了。现在谓词只筛 `metal-HUD`，进程过滤回到解析器。

### 三条必须遵守的使用条件

1. **游戏必须在设置环境变量之后启动**。`mtl start --game` 用 `launchctl setenv`
   设用户域变量，只对之后新启动的 GUI 应用生效。先开游戏再 `start` 是抓不到的。
2. **只对 Metal 呈现的应用有效**。原生 Metal 游戏、MoltenVK / CrossOver / 模拟器
   这类最终走 `CAMetalLayer` 的程序一般都有；纯 OpenGL 或某些包装层没有。
3. **`mtl stop` 会还原环境变量**，把 HUD 关掉。如果异常退出导致没还原，
   `launchctl unsetenv MTL_HUD_ENABLED` 手动关。留着的话 HUD 会出现在所有 GUI 应用上。

### 抓不到数据时

```bash
mtl doctor --game      # 实测一次：开关环境变量 → log stream 3s → 解析，然后还原
```

- 输出「没有捕获到 metal-HUD 日志行」且此刻没有游戏在跑，是正常结果。
- 报告里帧率面板会直接写明原因（没开 `--game` / 环境变量设不上 / 没抓到 HUD 行 /
  收到了 HUD 但与过滤词不符，并列出实际进程名）。
- `mtl status` 会实时给出「谁在写 HUD」：过滤词一条都没匹配上时立刻告诉你实际进程名。
- 原始 `log stream` 输出都在 `hud.log`，可以自己翻。
- 已经录完才发现帧率是空的？系统日志还留着那段时间的 HUD 行，可以补回来
  （受系统日志保留期限制，越早越好）：

```bash
mtl recover-fps 2026-11-14_190552 --process OcarinaOfTime
```

> **实测情况**：已在《塞尔达传说 王国之泪》（`totk-native`）上跑通 17 分钟，
> 抓到 1564 条帧率样本（约 59.7 FPS 平均）。解析逻辑另有合成样本测试覆盖
> 格式容错、重复投递去重、帧号归零重建基线等分支。
> 换游戏/换机型时，第一次仍建议先跑 `mtl doctor --game` 确认链路。

---

## 报告

`dashboard.html` 是完整的自包含页面（plotly.js 内嵌，约 5MB），断网可用。

- **KPI 卡**：时长、CPU/GPU 温度、CPU/GPU 功耗、All Power、主导 Thermal 状态、采样点（加了 `--game` 还有平均帧率）。
- **面板 1 温度**：CPU / GPU 平均温度（°C）。
- **面板 2 功耗**：CPU / GPU / All Power（实线），Sys Power（点线，单独标注口径）。
- **面板 3 Thermal Pressure**：状态色带 + 阶梯线，Nominal / Moderate / Heavy / Trapping / Unknown。
- **面板 4 频率与负载**：E/S/GPU 频率（左轴 MHz）+ CPU/GPU 使用率（右轴 %）。
- **面板 5 帧率**：帧率曲线（实线）+ 1% low（点线；逐帧间隔被日志脱敏时改为一条
  「≈ 窗口近似」的水平参考线），悬停显示该点覆盖的窗口时长、帧数与平均帧间隔。
- **事件**：贯穿所有面板的虚线，顶部带标签，悬停显示备注。
- 时间轴统一，支持框选缩放、拖动平移、`x unified` 悬停；缺失处曲线断开，不插值。

---

## 口径与注意事项

- **温度是平均值**。macmon 给的是传感器均值，不是最热点，绝对值比 iStat 之类偏低是正常的；
  比较同一台机器不同实验的相对变化才有意义。
- **`all_power` 不要和 CPU+GPU 相加**。它是独立指标。`sys_power` 又是另一个数（实测整机侧可达 33W，
  而 `all_power` 只有 15W），不要混用。
- **功耗单位是 W，不要除 1000**。
- **macmon 的 `cpu_usage_pct` 字段实际是 0~1 比值**（没有乘 100）。本项目统一从 ratio 计算百分比。
- **两个数据源独立采样**。macmon 与 powermetrics 是两个进程，启动时刻、间隔都有偏差，
  所以只按时间戳对齐，不按行号合并，也不假设行数相等。
- **powermetrics 需要 root**。`mtl start` 会在前台请求一次授权（`sudo -v`），
  或直接要一次密码并通过管道传给子进程；**密码不落盘**。不想给权限就用 `--no-thermal`，
  其它指标照常记录。

### 报告里 Thermal 面板是空的？

先看面板中央的说明，它直接写了原因：

| 提示 | 含义 | 处理 |
| --- | --- | --- |
| 本次实验未采集 Thermal Pressure | 录制时用了 `--no-thermal` | 去掉该参数重录一次 |
| Thermal Pressure 采集失败 | sudo 未授权 / 密码错误 / 被系统拒绝 | `mtl doctor --thermal` 单独验证 |
| powermetrics 没有返回任何样本 | 进程起来了但输出为空 | 看会话目录里的 `powermetrics.err.log` |
| 未解析到有效样本 | 有原始输出但格式未识别 | 把 `powermetrics.log` 前半段发出来 |

已经在用旧会话时，改完 `mtl report <会话名>` 就能重新生成，不用重新录。

---

## 开发

```bash
uv sync                       # 建环境
uv run pytest                 # 76 个用例
uv run mtl --help
uv run mtl start 调试 -i 1000 # 跑源码，不必先 tool install
```

加依赖用 `uv add <pkg>`（会同步更新 `pyproject.toml` 与 `uv.lock`，两者都要提交）；
升级用 `uv lock --upgrade && uv sync`。**请不要用 `pip install` 改项目环境**，
否则 `uv.lock` 会和实际环境脱节。

```
src/mac_thermal_lab/
├── cli.py           命令行与进程编排
├── collector.py     采集守护进程（双源、优雅停止、崩溃自愈）
├── macmon.py        macmon 解析与单位归一
├── powermetrics.py  Thermal Pressure 解析（含未知状态保留）
├── paths.py         XDG 目录解析与 config.toml
├── migrate.py       旧目录迁移（复制—校验—落位—清理）
├── storage.py       会话路径、时间戳、flock 存活判定
├── fps.py           Metal HUD 帧率解析（log stream）
├── analysis.py      时间戳对齐、统计
├── events.py        事件标记
└── report.py        Plotly 报告
```

测试固件 `tests/fixtures/macmon_samples.jsonl` 是真实的 M5 输出；
`powermetrics.log` 是按官方格式构造的样本（含一个未收录状态 `Serious`，用于验证不丢弃未知状态）。
