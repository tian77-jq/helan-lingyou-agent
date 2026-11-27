# 专家团配置包 · 贺兰有灵·灵游宁夏

> 本目录是 **WorkBuddy 原生专家团（Team）** 的配置与提示词存档，与 `../`（system / orchestrator /
> culture / ecology / verifier / tool_usage 单点提示词）互补：`../` 是模型侧行为准则，
> 本目录是**平台侧的成员注册与调度配置**。
>
> 冻结架构对照：随源码包分发的 `AGENTS.md`（ADR-0001 六 Agent 冻结）
>
> **当前版本：V1.2.4**（2026-10-19 22:54:58 构建，基线提交 `6bf7d0b`）。V1.2.4 变更（V32.2，自 V1.2.1 起累计）：
> ① 引擎新增 **腾讯位置服务适配器** `multiagent/tencent_maps.py`——配置 `TENCENT_MAP_KEY` 后真实检索
> POI / 餐厅 / 驾车路线 / 距离矩阵（GCJ-02；已实测：西夏陵与贺兰山岩画 POI、银川↔固原↔吴忠真实驾车路线、
> 真实餐厅）；无 Key → `NEEDS_AUTH`、无效 Key → 上游 `biz_311` 结构化降级，不崩溃、零伪造；
> SKILL.md 新增「能力 5：地图与路线」及 `MAP_*` 工具证据口径，A4 / A3 / 主理人三份成员提示词的能力现状表同步更新。
> ② 发布门禁 **预算成本诚实修正**（`gates.py` item 1b）：跨城行程（≥2 城）且成本估算无可靠实时来源
> （`is_realtime != True`）→ 强制 `publish_ready=NOT_READY`——地图接通 ≠ 预算已核验
> （判据：`tests/test_release_gate_semantics.py` 10/10）。
> ③ 图片服务九字段契约（`image_service.py` + Web `GET /api/images`）：未授权来源（Pexels/Unsplash
> `NEEDS_AUTH`、Wikimedia 网络阻断、本地语料缺失）时显式标注「图片待接入」，不渲染占位图。
> ④ 引擎规模 16 → 23 个 py、2168 → 7340 行；随包引擎 / 语料与仓库工作树逐字节一致
> （`release-manifest.json` 引擎哈希 ad1c73c MATCH；40 例回归 40/40 不破；**含 22:50 写入的
> 未提交 WIP `image_service.py`——WikipediaPageImageProvider，谱系与哈希差异见
> `06-evidence/release_record_1.2.4-build1.json` 的 note**）。
> ⑤ V1.2.1 机制（运行时版本自证 / `release-manifest.json` / R-3 settings.json 恢复）全部保留。
>
> **V1.2.1 变更（保留）**：随包引擎新增 **运行时版本自证**——`scripts/helan_agent/release.py` + `cli.py --version`
> （只读输出：包版本、实际执行目录 `engine_root`、引擎/语料实时哈希、与包内
> `release-manifest.json` 的逐项比对 `manifest.status`）；包内新增 `release-manifest.json`
> （版本 / 构建时 Git SHA / 引擎与语料组合哈希），与 `scripts/release_expert.py`、
> `scripts/sync_check.py` 三处哈希口径完全一致；SKILL.md 新增「能力 0：运行时版本自证」及纪律
> （报告版本必须以 `--version` 输出为准，不得凭对话记忆或注册成功推断）；修复市场源
> settings.json 被平台预览流程清理的问题（team 类型 validate 必需项，缺则官方校验直接报错）。
> 三层同步自检：随包引擎 vs 仓库引擎 MATCH、发布清单 MATCH（`../../06-evidence/version_sync_20261015_233121.md`）。
>
> **V1.2.0 变更（保留）**：**引擎与文化语料随包分发**
> （`skills/helan-knowledge-qa/scripts/`：2168 行引擎 + 2 语料 JSON，零第三方依赖）；SKILL.md 与六份 Agent MD
> 命令契约全部改为**包内相对定位**（自定位命令：按 `cli.py` 路径特征 find → 缓存优先 → `sort -V` 取最新版本），
> **彻底消除 `D:\SilverCup-Agent` 绝对路径依赖**；头像压缩至 512px/111-175KB（官方 ≤500KB 合规）；
> zip 10.8MB→1186.7KB。V1.1.0 变更（保留）：主理人「生态能力按需调用策略 / 创作闭环路由 / 成员调度语义」、
> A3「工具主备关系」、A4 能力现状、settings.json 补入市场源与安装缓存。
> 三层同步自检 10/10 MATCH（`../../06-evidence/version_sync_20261015_212004.md`）。
> **注意：提示词变更须新建会话召唤专家团后生效**（运行中会话不拾取新 MD）。

## 一、文件清单

| 文件 | 作用 | 对应项目架构 |
|------|------|-------------|
| `plugin.json` | 专家团注册配置（expertType=team、agentName、teamInfo、members、展示字段） | 专家团元数据 |
| `settings.json` | 默认主 Agent（`helan-lingyou-ningxia-team-lead`） | A0 入口 |
| `agents/helan-lingyou-ningxia-team-lead.md` | 主理人：意图识别、SOP 调度、消息中转、门禁裁决 | **A0 Orchestrator** |
| `agents/helan-culture-expert.md` | 宁夏文化 / 岩画 / 西夏 / RAG 溯源 | **A1 Culture** |
| `agents/helan-ecology-expert.md` | 贺兰山生态 / 岩羊 / 自然科普 | **A2 Ecology** |
| `agents/helan-travel-planner.md` | 约束提取 / 行程 / 预算 / 多轮修订 | **A3 TravelPlanner** |
| `agents/helan-travel-data.md` | 实时天气 / 数据服务 / 诚实降级 | **A4 TravelData** |
| `agents/helan-verifier.md` | 来源 / 约束 / 工具真实性 / 幻觉检测 | **A5 Verifier** |
| `skills/helan-knowledge-qa/SKILL.md` | 共享技能：封装随包分发的确定性引擎（零第三方依赖） | 共享能力入口 |
| `skills/helan-knowledge-qa/scripts/` | **随包引擎 + 语料**（helan_agent/ 23 个 py + corpus/ 2 JSON） | 跨电脑复现关键 |
| `PACKAGE_README.md` | 专家包说明（与安装包内 README.md 同源） | — |

## 二、成员与 Agent ID

| 角色 | 花名 | Agent ID（调度时使用） |
|------|------|----------------------|
| 主理人（首席调度官） | 宁知微 | `helan-lingyou-ningxia-team-lead` |
| 宁夏文化专家 | 夏文渊 | `helan-culture-expert` |
| 贺兰山生态专家 | 岳岩青 | `helan-ecology-expert` |
| 智慧文旅规划专家 | 骆知程 | `helan-travel-planner` |
| 文旅数据服务专家 | 沙晴川 | `helan-travel-data` |
| 可信核验专家 | 甄有据 | `helan-verifier` |

## 三、安装位置与注册

专家**必须**位于专家目录才能被检测到（路径随操作系统与用户名变化，以下为结构示意）：

```
<WorkBuddy 配置目录>/.workbuddy/plugins/marketplaces/my-experts/plugins/helan-lingyou-ningxia/
```

> Windows 默认配置目录为 `C:\Users\<用户名>`；官方表述为 `$WORKBUDDY_CONFIG_DIR`。
> **V1.2.0 起运行时不依赖任何具体绝对路径**——SKILL.md 与 Agent MD 的命令按包内 `cli.py`
> 路径特征自动定位（缓存优先 + 版本自然排序取最新）。

注册（写入 `my-experts/.codebuddy-plugin/marketplace.json`）：

```bash
python scripts/validate_expert.py  <expert-dir>
python scripts/register_expert.py  <expert-dir>
```

## 四、召唤与使用

1. 打开 WorkBuddy 左侧「专家 / 专家中心 → 我的专家」，选择《贺兰有灵·灵游宁夏》。
2. 也可用市场内任意入口直接发起对话（`defaultInitPrompt` 已内置首轮示例）。
3. 只需单一维度时可直接点名成员（如"让文化专家讲岩画"），主理人会走直调路由表。

## 五、验证状态（严格区分，勿混为一谈）

| 层级 | 状态 | 依据 |
|------|------|------|
| 1. 文件生成 | ✅ 成功 | 包内 6 份 Agent MD + plugin.json + settings.json + SKILL.md + 7 张头像齐全 |
| 2. 专家包校验 | ✅ 通过 | `validate_expert.py` 返回 `✅ Expert package is valid!`，0 error / 0 warning |
| 3. 专家团注册 | ✅ 成功 | `marketplace.json` 已写入 `helan-lingyou-ningxia` 条目并回读确认 |
| 4. 成员真实调度 | ✅ **已验证（两轮正式会话：2026-10-15 18:51–19:05 运行时 1.0.0；2026-10-15 21:15–22:45 运行时 V1.2.0 包）** | 第二轮为 V1.2.0 随包引擎的正式复验：主会话 253 行 + **7 份子代理 transcript**（A1/A2/A3/A4/A5 五成员真实执行，A4、A5 各两轮），成员实际执行自定位命令命中 1.2.0 缓存包并落盘 `cross_agent_trace_*` 三份 trace；A5 发现封山禁牧年份错误并判 `PASS_WITH_CAUTION + 1 项 P0`，P0 修正经核验已真正落入交付稿正文（非仅记录）。详见 `../../06-evidence/formal_run_V19_20261015.md`。另注意：普通开发会话派成员返回 not available 为平台预期语义（开发会话不注册成员 agentType），非包缺陷 |
| 5. 共享 Skill 调用 | ✅ 引擎实测通过 | V1.2.0 起引擎随包分发；隔离环境（zip 解压独立路径 + 模拟 HOME 自定位）与正式会话双路实测通过，见 `../../06-evidence/isolated_env_test_V15_20261015.md` 与 `formal_run_V19_20261015.md` |
| 7. 运行时版本自证（V1.2.1 起机制，当前包版本 1.2.4） | ⏳ **待新会话回传** | 包内 `cli.py --version` 可输出包版本 / 实际执行目录 / 引擎与语料实时哈希 / 与 `release-manifest.json` 的比对结果；**目前安装缓存仍为 1.2.0**（市场源 / 快照 / zip 已就位 1.2.4），需先由平台官方更新（`/plugin update helan-lingyou-ningxia@my-experts` 或插件管理页）并**新建会话**后运行，才能证明运行时确实是 1.2.4 |
| 6. 连接器/MCP | ⚠️ 按需连接（不声明硬依赖） | 账号已绑定 tencent-map / tc-chengxin / yanxu 三个官方连接器（V17.0 首调：地图地理编码、同程景点查询真实返回；yanxu 工具链通但公众号列表为空）；plugin.json **不声明** `dependencies.connectors`——官方规范明示仅运行必需才声明，避免评委召唤前被授权弹窗阻塞；已绑定连接器不声明亦可调 |

## 六、真实边界（不得夸大）

- 实时数据分层：**已实证**——文化/生态 RAG（引擎）、天气（open-meteo）、同程票务景点查询（tc-chengxin）、地图地理编码（tencent-map 连接器）；**未接入/待授权**——腾讯位置服务 Key（tencentmap-map-assistant Skill 通道）、小红书渲染（REDFOX_API_KEY）、公众号发布（需绑定微信公众平台）、交通班次与住宿价格的实时比价。
- 未接入数据一律呈现为「待核实」，不得编造。
- 本配置包为**候选旅游方案与文化知识服务**，不代表景区、旅行社或政府发布的正式安排。
- **演示视频准入（永久验收规则）**：录屏须与可提交版本一致。V1.2.0 的正式会话复验已于 2026-10-15 21:15 完成（见第 4 行证据）；**V1.2.4 的运行时版本自证尚未回传**（INSTALLED=BLOCKED：安装缓存仍为 1.2.0），因此当前可录制版本仍为 **V1.2.0**——待 1.2.4 经 `/plugin update` + 新建会话完成 `--version` 证据回传后，方可按 1.2.4 录制。
