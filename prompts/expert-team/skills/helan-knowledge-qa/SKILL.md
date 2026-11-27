---
name: helan-knowledge-qa
description: "贺兰有灵·灵游宁夏 专家团共享技能：调用包内自带的确定性引擎（零第三方依赖，随包分发），做宁夏文化/生态可信知识问答（BM25 + 证据门 + 引用溯源）、跨 Agent 行程编排闭环、实时天气查询与地图 POI/路线检索（配置腾讯位置服务 Key 时真实调用）。当用户询问宁夏文化、贺兰山生态、岩画、西夏、岩羊，或需要生成/修订宁夏行程、查询宁夏城市实时天气、检索景点/餐厅/驾车路线时使用。"
---

# 贺兰有灵·灵游宁夏 — 共享知识与规划技能

本技能是专家团六位成员共用的**唯一数据入口**。它不重新实现任何业务逻辑，
而是把《贺兰有灵》工程中**已实测可用**的确定性引擎**随包分发**（`scripts/` 目录），
任何成员在任何机器上召唤本专家团后即可调用，无需额外安装、无需源码树、无绝对路径依赖。

> 真实性前提：本技能只描述**已经跑通**的能力。未接入的数据域在本文件末尾「已知能力缺口」中明确列出，
> 任何成员不得以已接入的口径描述它们。

## 前置条件

| 项 | 值 |
|----|----|
| 引擎位置 | **本技能目录下 `scripts/helan_agent/`（随包分发，相对路径）** |
| 运行时 | Python 3.8+（`python` 或 `python3` 已在 PATH；WorkBuddy 托管运行时即满足） |
| 第三方依赖 | **无**（BM25 自研、零依赖，可离线运行） |
| 网络依赖 | 「实时天气」（open-meteo 公共 API，无需密钥）与「地图」（配置 `TENCENT_MAP_KEY` 后走腾讯位置服务）需要联网；无网/无 Key 时诚实降级 |
| 跨电脑复现 | 引擎代码全部按 `__file__` 相对定位，语料在 `scripts/corpus/`，任意路径解压即用 |

## 引擎定位（跨电脑关键步骤）

专家团安装后位于插件缓存中，路径随安装位置与版本号变化。成员执行前先用以下命令定位本技能自带引擎
（按 `cli.py` 路径特征匹配——旧版本缓存若无引擎会自动被排除；优先安装缓存、按版本号自然排序取最新）：

```bash
# 定位本技能自带引擎（Git Bash / Bash 工具可用）
HELAN_CLI=$(find "$HOME/.workbuddy/plugins" -path "*helan-knowledge-qa/scripts/helan_agent/cli.py" 2>/dev/null | grep "/cache/" | sort -V | tail -n 1)
[ -z "$HELAN_CLI" ] && HELAN_CLI=$(find "$HOME/.workbuddy/plugins" -path "*helan-knowledge-qa/scripts/helan_agent/cli.py" 2>/dev/null | sort -V | tail -n 1)
HELAN_MULTI=$(dirname "$HELAN_CLI")/multiagent/cli.py
```

> 若已在本工程源码树中（存在 `domain/helan_agent/cli.py`），可直接使用相对路径
> `domain/helan_agent/cli.py`，两种入口是同一套引擎。

## 能力 0：运行时版本自证（只读，无副作用）

任何成员、任何会话都可以用一条命令证明「当前执行的到底是哪个版本、内容与发布清单是否一致」：

```bash
python "$HELAN_CLI" --version
```

输出（JSON）关键字段：

| 字段 | 含义 |
|------|------|
| `package_version` | 从包根 `.codebuddy-plugin/plugin.json` 读到的版本（开发源码树中为 `null`） |
| `engine_root` | **当前实际执行的引擎目录**（证明来自哪个安装缓存版本目录） |
| `engine.sha256` / `corpus.sha256` | 对随包引擎/语料**实时计算**的组合哈希 |
| `manifest.status` | `MATCH` = 与包内 `release-manifest.json` 一致；`MISMATCH` = 内容已被改动（附 `mismatches`） |
| `manifest.manifest_git_commit` | 构建该包时的源码 Git SHA |

纪律：
1. 向用户报告「专家团版本」时，**必须以本命令输出为准**，不得以对话记忆或注册成功推断。
2. `manifest.status` 为 `MISMATCH` 时，说明包内容与其发布清单不一致，须如实上报，不得据此宣称版本一致。
3. 本命令只读：不写文件、不联网、不改状态，可安全在任何会话执行。

> 版本发布与验收流程见工程内 `docs/EXPERT_TEAM_SYNC_PLAYBOOK.md` §八（V21.0）。

## 能力 1：可信知识问答（文化 / 生态）

```bash
python "$HELAN_CLI" "<自然语言问题>" --pretty
```

- 可选参数：`--top_k N`（默认 5）、`--json`（强制 JSON）、`--pretty`（可读文本）。
- 退出码：`0` 正常 ｜ `2` 输入为空 ｜ `3` 检索或生成异常。
- 输出字段：`query`、`detected_topic`、`grounded_answer`、`citations[]`、`gate`。

`gate` 字段语义：

| 字段 | 含义 | 使用要求 |
|------|------|---------|
| `grounding` | 是否落到证据（`grounded` / 否则不足） | 非 grounded 时禁止作确定断言 |
| `confidence` | 置信度（如 `low`） | `low` 时须声明"依据有限" |
| `conflict_detected` | 是否存在来源冲突 | 为真时**必须**并列呈现差异，不得强行统一 |
| `realtime_flag` | 是否属动态信息 | 为真时输出必须标「待核实」 |
| `can_assert` | 是否允许断言 | 为假时只能给"仅供参考/需核实"表述 |

`citations[]` 每项含 `index`、`title`、`source_name`、`source_url`、`verified`。
**`verified=false` 即「待核实」，对外输出必须保留该标注。**

常用批量核验（多问题逐条取证）：

```bash
for q in "贺兰山岩画的历史文化价值" "西夏陵是世界遗产吗" "岩羊的生态特点"; do
  python "$HELAN_CLI" "$q" --pretty
done
```

## 能力 2：跨 Agent 行程编排闭环（约束 → 行程 → 实时数据 → 核验 → 门禁）

```bash
python "$HELAN_MULTI"                    # 跑通三轮演示并落盘证据
python "$HELAN_MULTI" --turn "第二天我还想去沙坡头，尽量不超预算。"
```

该命令真实执行 A0→A3→A4→A5（按意图含 A1/A2）链路，并落盘到
`<引擎所在技能目录>/evaluation/results/`（目录不存在时自动创建）：

- `cross_agent_trace_<time>.json` — 完整消息轨迹与各 Agent 产出
- `cross_agent_summary_<time>.txt` — 每轮 intent / 三层门禁 / A5 裁决

三层门禁（确定性规则，实现于 `scripts/helan_agent/multiagent/gates.py`）：

| 门禁 | 校验内容 |
|------|---------|
| Execution | 被调度 Agent 的执行状态；声明调用工具必须有 `tool_evidence` |
| Business Contract | 天数 / 预算 / 强度 / 兴趣覆盖等硬约束；轻松强度日均景点 ≤ 2 |
| Release | 前两者双通过 **且** A5 判 `PASS`；不得把「待核实」当已证实事实发布；**跨城行程**（≥2 城）在成本估算无可靠实时来源（`is_realtime != True`）时强制 `publish_ready=NOT_READY`（V32.2 预算诚实铁律：地图接通 ≠ 成本已核验） |

## 能力 3：实时天气（open-meteo，真实调用）

由能力 2 的链路自动触发（A4 阶段）。底层实现：
`scripts/helan_agent/multiagent/agents.py::_real_weather(city)`

1. `https://geocoding-api.open-meteo.com/v1/search?name=<城市>&count=1&language=zh`
2. `https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&current_weather=true`

工具证据格式（进入 `tool_evidence`，并受 Execution Gate 校验）：

- 成功：`WEATHER_API_OK:<城市>:<温度>C@<时间>`
- 失败：`WEATHER_API_FAIL:<城市>:<原因>`

**失败即诚实降级**：返回 `source = "未获取到实时数据，天气需以官方实时预报核验（待核实）"`，
禁止补一个看起来合理的温度。

> 已知限制：open-meteo 地理编码对部分中文城市名返回空，此时改用拼音（如 `Yinchuan`）重试；
> 仍失败则按上述降级口径处理。

## 能力 4：内容创作（小红书图文 / Vlog 分镜）

基于**本会话真实生成的行程与文化证据**产出内容，**不调用任何大模型**，属模板化确定性生成，
必须在输出中标注生成方式为 `template-based（依据真实行程，非模型生成）`。

入口（三种，业务实现同源 —— 均落在 `runtime.content(session_id, kind)`）：

- **同进程 API（引擎真源）**：`from runtime import AgentRuntime; rt = AgentRuntime(); rt.run(sid, q); rt.content(sid, "xiaohongshu"|"vlog")`
- **MCP 工具**：`helan_content`，入参 `{session_id, kind}`（kind ∈ `xiaohongshu`/`vlog`）
- **Web 入口**：`content_create` 意图（聊天流），内部同样调用 `runtime.content`

> 注：`multiagent/cli.py` 仅为跨 Agent 闭环演示入口（`python cli.py` / `--turn`），**不提供** `content` 子命令；内容创作须经由上述三种入口之一，且**先有行程**（`session_id` 存在且已完成规划），否则 `ok=false`。

纪律：

1. **先有行程，后有内容**：本会话尚无行程时如实返回 `ok=false` 并说明原因，**禁止虚构目的地与行程**后再生成内容。
2. **不改行程**：内容创作轮属于 `content_create` 意图，行程标记为 `carried_over`（沿用上一轮），**不得触发重排**。
3. 事实与引用口径与能力 1 完全一致；不得为文案效果新增无来源事实。
4. 用户说「改成 Vlog 分镜脚本」时，动作对象是**内容形态**而非行程元素，仍判内容创作（由
   `contracts.FORM_CONVERT_MARKERS` 唯一真源判定），不得被误判为行程修订而重建行程。

## 能力 5：地图与路线（腾讯位置服务 WebServiceAPI，V32.2）

由能力 2 的链路在 A4 阶段自动触发（`scripts/helan_agent/multiagent/agents.py::_real_maps`）；
MCP 工具 `helan_maps` 为能力自证（返回 READY/NEEDS_AUTH、坐标系 GCJ-02、POI/路线/距离/餐厅可用性），
`helan_places` 为 POI 检索入口。

1. **授权口径**：服务端 Key 只从环境变量 `TENCENT_MAP_KEY` 读取，**绝不**写入源码 / Git / 前端 / 聊天记录；
   无 Key → 所有方法诚实返回 `NEEDS_AUTH`，不发起请求、不返回任何伪造 POI / 路线 / 距离；
   Key 无效 → 上游错误（如 `biz_311`）结构化降级，不崩溃、不冒充成功。
2. **坐标系**：GCJ-02；驾车路线 / 距离矩阵以 `"lat,lng"` 坐标对入参（先对城市做地理编码）。
3. **工具证据格式**（进入 `tool_evidence`，受 Execution Gate 校验）：
   - `MAP_POI_OK:<城市>:<数量>` / `MAP_POI_FAIL:<城市>:<原因>`
   - `MAP_RESTAURANT`（餐厅 nearby 检索结果）
   - `MAP_ROUTE_OK:<起点>-><终点>` / `MAP_ROUTE_FAIL:<起点>-><终点>:<原因>` / `MAP_ROUTE_SKIP:<起点>-><终点>:no-coords`
   - `MAP_MATRIX`（多城距离矩阵）
4. **预算诚实铁律（V32.2）**：地图接通 ≠ 预算 / 成本已核验。跨城行程（≥2 城）在成本估算
   `is_realtime != True`（无可靠实时价格来源）时，发布门禁强制 `publish_ready=NOT_READY`，
   成员不得因「地图已接通」而宣称行程可发布。

## 输出与引用纪律（全体成员必须遵守）

1. 每一条关键事实都要能回溯到一条 citation；检索不到就写「未核实」。
2. 动态信息（门票、班次、天气、开放时间、房价）一律标「待核实」并给查询时间与来源。
3. 工具报错不得改写成成功；HTTP 200 不等于数据可用。
4. 来源冲突时并列呈现，禁止平均或合并成单一确定结论。
5. 不编造 `source_id`、页面引用、统计数值与授予年份。

## 已知能力缺口（未接入，禁止冒充）

| 数据域 | 现状 | 对外口径 |
|--------|------|---------|
| 地图 / 路线 / 距离 / 耗时 | **已接入**（需 `TENCENT_MAP_KEY`；未授权则 `NEEDS_AUTH`） | 有 Key 时给真实 POI / 路线并注明来源与 GCJ-02 坐标系；无 Key 标「待核实」 |
| 交通班次与票价 | 未接入 | 标「待核实」 |
| 住宿价格与房态 | 未接入 | 标「待核实」 |
| 餐饮人均消费 | 未接入 | 标「待核实」 |
| 景区门票与开放时间 | 未接入实时源 | 标「待核实」，建议官方渠道 |

## 随包文件清单（scripts/，相对本技能目录）

```text
scripts/
├── helan_agent/          # 六 Agent 确定性引擎（纯标准库，零第三方依赖）
│   ├── cli.py            # 能力 1 单轮问答入口；`--version` = 能力 0 版本自证
│   ├── release.py        # 版本自证：实时哈希 + 比对 release-manifest.json
│   ├── qa_engine.py / retriever.py / evidence_gate.py
│   ├── citation_policy.py    # 来源展示唯一真源（V30.0 四层发布语义）
│   ├── image_service.py      # 图片适配层（V31.3 Phase A；未授权即 NEEDS_AUTH，不伪造图）
│   └── multiagent/       # 能力 2 跨 Agent 闭环（A0-A5）
│       ├── cli.py / engine.py / orchestrator.py / agents.py
│       ├── contracts.py / state_store.py / gates.py / trace.py
│       ├── runtime.py    # 三入口（Web/MCP/CLI）唯一业务真源
│       ├── tencent_maps.py  # 腾讯位置服务适配器（V32.2；POI/地理编码/路线/距离，GCJ-02）
│       └── weather_probe*.py
├── corpus/               # 最小可分发文化知识集（带溯源）
│   ├── ningxia_knowledge.json   # 带溯源文化/生态语料（entries）
│   └── attractions.json         # 22 景点 + 17 美食结构化数据（图片字段已治理）
└── ../release-manifest.json     # 构建期发布清单（版本/Git SHA/引擎与语料哈希）
```

## 参考

- 架构基线：本工程 `AGENTS.md`（六 Agent 冻结，ADR-0001；随 submission 源码包分发）
- 消息与状态契约：`scripts/helan_agent/multiagent/contracts.py`
- 门禁规则：`scripts/helan_agent/multiagent/gates.py`
- 知识语料：`scripts/corpus/ningxia_knowledge.json`、`scripts/corpus/attractions.json`
