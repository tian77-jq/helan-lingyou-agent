# WorkBuddy 兼容性风险与差距报告（V27.0 §八）

> 结论摘要（先给答案）
> - **可以**：评委拿到当前源码，在本机（Windows/macOS/Linux + Python 3.12+）用统一入口
>   启动，可复现与演示视频**一致的核心业务能力**（六 Agent 编排、文化/生态检索、真实
>   天气调用、行程与差量重规划、A5 审核、三道门禁、统一执行轨迹）。
> - **尚不能**：在**中国移动 WorkBuddy 客户端内**直接"一键复现"。唯一 P0 差距是——
>   **平台侧运行时接入未闭合**：需要人工在客户端把 `mcp-adapter/server.py` 添加为本地
>   stdio MCP Server（或向 `~/.workbuddy/mcp.json` 写入后点「信任」）、由平台分配工具权限，
>   并确认联名版客户端可用。该项**不可由仓库自行声明完成**，故标记 PENDING。
> - **V27.0 进展**：连接器配置生成器与平台级配置复现器已完成并实测（**7/7、24/24 PASS**），
>   即"配置本身正确、按该配置能真实拉起并调用"已由仓库自证；**剩下的是客户端 GUI 点「信任」
>   与平台分配权限这一人工动作**（见 5. P0-1 与 六. 最小修复）。
> - 直接修复措施见文末「六、唯一 P0 的最小修复」。

---

## 一、核查方法

- 官方文档逐条核对（2026-10-16 实取）：
  - 专家团 <https://open.workbuddy.cn/docs/expert-team>
  - 技能 <https://open.workbuddy.cn/docs/skill>
  - 连接器 <https://open.workbuddy.cn/docs/connector>
  - OpenAPI <https://open.workbuddy.cn/docs/openapi>
- 本地实测（真实执行，非静态检查）：`scripts/preflight.py`、`scripts/verify_contracts.py`、
  `scripts/verify_scenarios.py`、`scripts/verify_agent_execution.py`、`scripts/verify_clean_copy.py`。

---

## 二、逐条对照表

| # | 官方要求（原文要点） | 本工程现状 | 差距 | 等级 |
|---|---|---|---|---|
| 1 | 专家团目录必须含 **`.codebuddy-plugin/plugin.json`**、`agents/*.md`、`avatars/`、`settings.json` | `submission/02-prompts/expert-team/` 含 plugin.json/agents/skills/avatars | 官方"参赛包排除隐藏文件"与"插件必须含 `.codebuddy-plugin`"冲突 → 已用**分包**解决（见《提交分包说明》） | P1（已缓解） |
| 2 | **开发者不可自行添加 tools**，工具权限由系统统一分配 | 六 Agent 提示词未声明任何系统工具；业务能力经 Skill / MCP / 应用代码提供 | 无（合规） | — |
| 3 | 主理人创建团队、按 SOP 调度成员、**成员不得互相直连**、主理人**不得代写**成员产出 | A0 统一编排 + DAG（`contracts.DAG_SPEC`），消息全部经 A0 中转；`check_dag()` 强制无环 | 无（设计一致） | — |
| 4 | Skill 结构：`SKILL.md` + `references/` + `scripts/` + `templates/` | `expert-team/skills/helan-knowledge-qa/SKILL.md` + scripts（含引擎） | 无 | — |
| 5 | 连接器：**MCP＋Skill 或 CLI＋Skill，二者不可混用**；一个连接器只配一个 MCP Server | 本工程 MCP 为**项目自带本地 stdio 服务**（`mcp-adapter/server.py`），未提交连接器包 | 若要走"连接器市场"，需单独打包 `connector-meta.json + mcp.json + icon.svg + skills/`（当前未做） | P2 |
| 6 | MCP：远程用 HTTPS(SSE/streamableHttp)，本地进程可用 **stdio**；**单次响应建议 ≤30s** | 本地 stdio 已实测；`RECOMMENDED_TIMEOUT`/`DEFAULT_TIMEOUT_MS = 30000` 与之对齐 | 无（本地 stdio 合规） | — |
| 7 | 运行时依赖可声明托管 Node/Python（`runtime.type/version`），**不得依赖用户预装全局包** | `requirements-web.txt`（fastapi/uvicorn）+ 前端 npm 均显式声明；引擎为**纯标准库**；`preflight.py` 校验版本 | 无 | — |
| 8 | **不得硬编码固定盘符/用户目录**；不得把密钥写入包 | `scripts/preflight.py` 跨设备硬编码自审 **0 命中**；`环境变量样例` 无真实密钥 | 无 | — |
| 9 | OpenAPI：有 OAuth2.1 / 本地助理消息 / 云端任务 ACP | 仓库保留可选集成（`domain/workbuddy_api/client.py`），无凭证自动跳过 | **官方文档中未出现"指定调用已安装专家团"的接口或字段** → 不得假设 Web 可经 API 调用本专家团 | P1（认知纠正） |
| 10 | 插件版本：市场源/安装状态/实际会话**不是同一概念**，须防版本不同步 | 已有 `sync_check.py` + `verify_install.py` 六态验证；专家团 1.2.4 已发布就绪（市场源+快照+zip），平台安装/更新仍 **BLOCKED**（见下） | 平台侧安装/更新未完成 | P1（BLOCKED） |
| 11 | 平台侧：MCP Server 需在客户端添加、工具权限由平台分配 | 仓库已提供可运行 stdio 服务并本地实测通过；**V27.0 已产出连接器配置并按其原样拉起核对（24/24 PASS）** | **需人工在客户端点「信任」+ 平台分配权限** | **P0**（已收敛为单一 GUI 动作） |
| 12 | 比赛指定"中国移动 WorkBuddy"联名环境 | 仅确认 WorkBuddy 客户端 5.8.2 + `@tencent` 包；**未确认联名版与手机号登录** | 平台归属未确认 | **P0** |

---

## 三、三类执行的诚实边界（对应 §四）

| 类别 | 定义 | 本工程状态 |
|---|---|---|
| kind_1 WorkBuddy 原生成员调度 | 平台主理人创建团队、按 DAG 派发、成员回传 | **PENDING**（需平台侧配置，仓库未声称已完成） |
| kind_2 自有模型 Agent 独立推理 | 多个模型 Agent 各自推理/调用工具/交换结果 | **V27.0 实测已启用**：`domain/helan_agent/helan_llm/`；配置凭证后 A1/A2/A3 执行**真实模型调用**（HTTP 200、latency 3.6–12s、带 `request_id`/`response_sha256`/`prompt_digest`），逐 Agent 标注 `execution_mode=model`；**A4 仍为 tool_only、A5 恒为确定性**（防模型自证） |
| kind_3 确定性业务模块 | 普通 Python 确定性代码（A4 内含真实外部 HTTP） | **当前实际实现**；A4 真实调用 open-meteo 并留痕 |

> 明确**不**宣称"六个独立模型在线协作"。统一轨迹逐 Agent 标注 `execution_mode`，
> 缺证据即自检失败（`model_claims_have_evidence`）。

---

## 四、已实测通过的项目（证据可复核）

| 验收项 | 结果 | 证据路径 |
|---|---|---|
| 环境预检（跨设备） | PASS=11 WARN=1 BLOCK=0 | `evaluation/results/preflight_*.json` |
| 消息与 DAG 契约核验 | PASS 15/15（12 字段齐备、DAG 无环、账本可追溯、六类事件） | `evidence/acceptance/contract_report_*.json` |
| 六场景 A–F × 三入口 | PASS=15 FAIL=0 SKIPPED=3（18 格全覆盖，3 处 SKIPPED 均带理由；引擎入口 6/6） | `evidence/acceptance/acceptance_report_20261016_203458.json` |
| Web 产品门禁（HTTP，真实后端） | PASS 21/21 | `evidence/web/web_e2e_20261016_201645.json` |
| MCP 真实 `tools/call`（本地 stdio） | PASS 20/20（12 工具 + 错误路径 + 真实场景） | `evidence/mcp/mcp_tools_call_20261016_201618.json` |
| MCP 平台连接器配置复现 | PASS 24/24（按 `mcp.json` 原样拉起） | `evidence/mcp/mcp_platform_config_20261016_203906.json` |
| MCP 入口 ↔ Web 入口一致性 | PASS 13/13（自检 15 项全真） | `evidence/mcp/mcp_web_parity_20261016_201519.json` |
| 真实模型执行链（§五 七项） | PASS 7/7（4 次真实 HTTP 200） | `evidence/real_llm/real_llm_report_20261016_201556.json` |
| 40 例端到端回归 | PASS=40 PARTIAL=0 BLOCK=0（端到端 100%） | `evaluation/results/helan40_summary_20261016_201655.txt` |
| 干净副本 + 干净 venv 复现（§七） | **PASS**（临时副本内跑通预检+契约+六场景 engine/mcp/**web**；Web 服务在副本内真实启动，HTTP 200） | `evidence/acceptance/clean_copy_report_20261016_201359.json` |

> **解释器相关说明（§八 环境兼容性披露）**：预检与干净副本验证的结论与**所用 Python 解释器**有关。
> - 解释器已装 `fastapi`/`uvicorn`（V27.0 实测：**全新 venv，Python 3.13.14 + `pip install -r requirements-web.txt`**；
>   以及本机 CPython 3.12.9）→ 预检 BLOCK=0，干净副本验证 **PASS**（Web 入口在副本内真实启动并通过）。
> - 解释器未装 Web 依赖（如未安装的 3.13.x）→ 预检出现 2 项「Web 后端依赖」BLOCK，干净副本验证
>   返回 **PARTIAL（exit 3）** 并给出修复命令 `pip install -r requirements-web.txt`，**不会**误报为复现失败。
> 评委如需覆盖 Web 入口，请用已安装 `requirements-web.txt` 的解释器执行；**核心能力（引擎 + 本地 stdio MCP 入口）在任何 Python ≥3.10 上均可离线复现**。

---

## 五、风险清单（按等级）

### P0-1 平台侧运行时接入未闭合（V27.0 已收敛为**单一 GUI 动作**）
- 现象：本地 stdio MCP 实测通过，且 V27.0 已**按拟提交的 `mcp.json` 原样拉起并 24/24 核对通过**；
  但**尚未在 WorkBuddy 客户端内点击「信任」启用**，也未由平台分配工具权限。
- 已消除的前置不确定性：解释器路径 / 工程 cwd / 依赖 / Windows 路径转义 / stdio 收发 /
  错误输出 / 超时 / 工具执行位置 —— 均由 `scripts/gen_mcp_config.py`（自检 7/7）与
  `scripts/verify_mcp_platform_config.py --scenario`（24/24）**真实实测覆盖**。
- 剩余人工动作：客户端「连接器」入口对该 server 点「信任」（受企业策略 `member_custom_model`
  校验，默认 fail-open 允许）；平台分配工具权限；随后**新会话**确认工具可见。
- 影响：评委"在平台内复现"这一步无法由仓库自行完成。
- 最小修复：见第六节（已更新为"配置已就绪，只需信任 + 验证"三步）。

### P0-2 赛事平台归属未确认
- 现象：仅确认 WorkBuddy 5.8.2 与 `@tencent` 包，未见中国移动联名标识。
- 影响：无法确认"用比赛指定账号/客户端"这一前提。
- 最小修复：用户确认赛事指定平台与登录方式；若为联名版，需在联名客户端复测 §二 各条。

### P1-1 专家团 1.2.4 安装/更新 BLOCKED
- 现象：`scripts/verify_install.py` 结论 `INSTALLED_VERIFIED=BLOCKED`（安装缓存仍为 1.2.0）；客户端 `/plugin update` 为文本命令无效。
- 影响：**仅影响"平台原生成员调度"这条验收项**，不阻塞 Web 主交付。
- 处置：在该验收项显式标 BLOCKED，不无限阻塞主线；市场源 / 快照 / zip 已于 2026-10-19 22:54:58 就位（v1.2.4，基线提交 `6bf7d0b`，随包引擎含 22:50 未提交 WIP、哈希 ad1c73c），用户执行 `/plugin update helan-lingyou-ningxia@my-experts`（或插件管理页更新）+ 新建会话 + `cli.py --version` 回传后即可闭环。

### P1-2 OpenAPI 不能指定专家团
- 现象：官方文档无"指定调用已安装专家团"的接口/字段。
- 处置：不把 API 调用专家团作为参赛核心链路。

### P2-1 连接器包未产出
- 若要进入连接器市场，需另出 `connector-meta.json + mcp.json + icon.svg + skills/`。
- 当前以"项目自带 MCP 服务 + 复现说明"方式交付，不依赖市场。

### P2-2 专家团包内引擎副本与主工程存在版本分叉
- `submission/02-prompts/expert-team/skills/.../scripts/helan_agent/`是随包副本。
- 处置：不手改插件缓存；如需同步须走版本号递增 + validate/register（P2，独立排期）。

---

## 六、唯一 P0 的最小修复（V27.0：配置已就绪，只剩信任 + 验证）

**目标**：让评委在中国移动 WorkBuddy 内从提交源码启动并执行同等功能。

### 步骤 A（工程侧，已由仓库完成，可直接复核）
1. 生成连接器配置（**不写盘**，仅打印并自检）：
   `python scripts/gen_mcp_config.py --print --python "<你的 Python 解释器绝对路径>"`
   → 期望自检 **7/7 PASS**（解释器可执行 / 工程根存在 `mcp-adapter/server.py` / cwd 正确 /
   依赖可用 / stdio 收发正常 / 无盘符硬编码 / 无密钥）。
2. 写入用户级配置（可选，等价的 GUI 添加方式见步骤 B）：
   `python scripts/gen_mcp_config.py --write --python "<解释器>"`
   → 生成 `~/.workbuddy/mcp.json`（stdio 型，`timeout=60000`，`no_proxy=127.0.0.1,localhost`）。
3. 平台级配置自证（**按该配置原样拉起**并逐项核对）：
   `python scripts/verify_mcp_platform_config.py --scenario`
   → 期望 **24/24 PASS**（发现工具 / 核对 Schema / 真实只读 `tools/call` / 非 Mock 结果 /
   run_id 与执行状态 / 进程存活）。

### 步骤 B（平台侧人工，约 3 分钟）
4. 打开 WorkBuddy 客户端 →「专家 · 技能 · 连接器」→「连接器」→「自定义连接器」，
   对该 `helan-lingyou-ningxia` server 点 **「信任」** 启用（若企业策略 `member_custom_model`
   禁用了自定义连接器，需企业管理员放开）。
5. **新建会话**，确认工具列表中可见 `helan_plan` / `helan_execution_trace` 等 12 个工具，
   并执行一次 `helan_plan`，核对返回的 `run_id` / 门禁 / 审核结论。
   （普通开发会话不会自动加载新增连接器；须新会话。）

### 步骤 C（可选，专家团）
6. 如赛事要求使用专家团，在客户端安装/更新 `helan-lingyou-ningxia`，
   并**在新会话召唤专家团**中验证成员调度（普通会话派成员必报 not available，属平台预期语义）。

> 步骤 B 需**人工在平台侧**完成，仓库不代为声明"已通过"。
> 在此之前，平台内 MCP 实调用一律记 **PENDING**，**不并入**"项目全部通过"。

---

## 七、结论

- **评委拿到当前源码，是否已经能在中国移动 WorkBuddy 中执行出与视频同等的核心能力？**
  → **部分可以**：本机/任意干净环境可完整复现核心能力（V27.0 已实测：干净副本 + 全新 venv，
  Web 入口在副本内真实启动，HTTP 200）；**在 WorkBuddy 客户端内**尚缺平台侧运行时接入
  （P0-1，已收敛为客户端点「信任」+ 平台分配权限）与平台归属确认（P0-2）。
- **唯一最优先的 P0 差距** → **平台侧运行时接入未闭合**（客户端信任本地 stdio MCP Server
  并由平台分配工具权限）。**配置与"按配置拉起"均已由仓库实测自证**，剩余为单一人工 GUI 动作。
- **直接修复措施** → 第六节 A/B/C 三步（A 已完成，B 需人工）。
- **不得合并的验收结论**：`PLATFORM_MCP_VERIFIED`（平台内实调用）当前 **PENDING**，
  与 `LLM_MULTI_AGENT_VERIFIED`（**PASS**）、`WORKBUDDY_REPRODUCTION`（**PENDING**）
  三者分别记账，**不合并**为笼统"项目全部通过"。
