# 贺兰有灵·灵游宁夏（Helan Spirit · Wander Ningxia）

六位专家协同的宁夏智慧文旅专家团：宁夏文化与贺兰山生态可信问答、个性化行程规划、多轮修订，全程证据可追溯。

## 类型

Team 型（多角色协作团队，expertType = `team`）

## 团队成员

| 角色 | 成员 ID | 名字 | 对应项目架构 |
|------|---------|------|-------------|
| 主理人（首席调度官） | `helan-lingyou-ningxia-team-lead` | 宁知微 | A0 Orchestrator |
| 宁夏文化专家 | `helan-culture-expert` | 夏文渊 | A1 Culture |
| 贺兰山生态专家 | `helan-ecology-expert` | 岳岩青 | A2 Ecology |
| 智慧文旅规划专家 | `helan-travel-planner` | 骆知程 | A3 TravelPlanner |
| 文旅数据服务专家 | `helan-travel-data` | 沙晴川 | A4 TravelData |
| 可信核验专家 | `helan-verifier` | 甄有据 | A5 Verifier |

## 功能

1. **文化/生态可信问答**：经 BM25 检索 + 证据门返回带引用与核验状态的答案，冲突不强行统一。
2. **行程规划**：确定性约束提取 → 单日单城按天行程 → 预算/天数/强度硬约束校验。
3. **多轮修订**：保留未被明确取消的旧硬约束，输出「变化项 / 未变项 / 变更理由」。
4. **实时天气**：open-meteo 真实查询；失败诚实降级为「待核实」并留工具证据。
5. **门禁裁决**：Execution / Business Contract / Release 三层确定性门禁，核验专家判 PASS 才发布；跨城行程在成本无可靠实时来源时强制 `publish_ready=NOT_READY`。
6. **地图与路线**：配置 `TENCENT_MAP_KEY` 后由引擎真实检索 POI / 餐厅 / 驾车路线 / 距离矩阵（GCJ-02，V32.2 已实测）；未配置时诚实返回 `NEEDS_AUTH`，不伪造数据。

## 共享技能

- `skills/helan-knowledge-qa` — **随包分发**的确定性引擎（零第三方依赖，纯 Python 标准库）。
  引擎代码位于 `skills/helan-knowledge-qa/scripts/helan_agent/`，文化语料位于 `scripts/corpus/`，
  任意机器解压即用、无绝对路径依赖。调用契约详见该目录下 `SKILL.md`。

## 使用示例

- 一家四口去宁夏玩三天，预算 5000 元，想了解贺兰山文化，安排住宿、交通、特色美食和亲子研学，行程不要太累。
- 贺兰山岩画有什么历史文化价值？请给出可追溯的来源。
- 第二天我还想去沙坡头，尽量不超预算，帮我修订行程。

## 头像

头像位于 `avatars/`，由 AI 生成，统一漫画插画风格。如需替换为自定义头像：
- 格式：PNG（推荐）或 JPG ｜ 尺寸：512×512 px ｜ 单张不超过 500KB

| 文件 | 用途 |
|------|------|
| `avatars/team.png` | 团队整体头像 |
| `avatars/helan-lingyou-ningxia-team-lead.png` | 主理人 宁知微 |
| `avatars/helan-culture-expert.png` | 夏文渊 |
| `avatars/helan-ecology-expert.png` | 岳岩青 |
| `avatars/helan-travel-planner.png` | 骆知程 |
| `avatars/helan-travel-data.png` | 沙晴川 |
| `avatars/helan-verifier.png` | 甄有据 |

## 安装位置

```
$WORKBUDDY_CONFIG_DIR/plugins/marketplaces/my-experts/plugins/helan-lingyou-ningxia/
（未设置 WORKBUDDY_CONFIG_DIR 时为 ~/.workbuddy/plugins/marketplaces/my-experts/plugins/）
```

注册使其在 WorkBuddy 中可见：

```bash
python scripts/register_expert.py <expert-dir>
```

## 打包分享

```bash
python scripts/package_expert.py <expert-dir> [output-dir]
```

## 边界与免责

- 地图 POI / 地理编码 / 驾车路线：配置 `TENCENT_MAP_KEY` 时真实接入（V32.2 已实测，GCJ-02）；未配置时诚实降级为 `NEEDS_AUTH`，不伪造数据。交通班次与票价 / 住宿价格与房态 / 餐饮人均消费 / 景区门票与开放时间**尚未接入**，一律以「待核实」呈现。
- 本专家团提供文化知识服务与候选旅游方案，**不代表景区、旅行社或政府发布的正式安排**。
- 架构基线：工程 `AGENTS.md`（六 Agent 冻结，ADR-0001；随 submission 源码包分发）。
