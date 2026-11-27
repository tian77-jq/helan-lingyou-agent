# WorkBuddy 平台复现说明（V41.9）

> 适用：评委在 WorkBuddy 专家团中复现《贺兰有灵·智游宁夏》。
> 主复现入口仍是本地 Web；本文件说明可选的 WorkBuddy 专家团入口。

## 1. 安装专家团

WorkBuddy → 专家团 / 插件 → 导入本地专家团包：

```text
submission/07-deployment/helan-lingyou-ningxia.zip
```

包版本：`1.3.0`，内置 V41.9 引擎与语料（27 个 py + 2 个语料 JSON）。

导入后新建会话，选择 `helan-lingyou-ningxia` 六人团，确认主理人为
`helan-lingyou-ningxia-team-lead`。

## 2. 平台侧人工步骤

以下动作不会随源码自动完成，必须由评委在 WorkBuddy 中手动完成：

1. 导入/启用专家团；
2. 允许专家团调用技能 `helan-knowledge-qa`；
3. 如需地图/图片等第三方能力，在平台侧单独授权；
4. 平台侧成员调度/权限如未生效，以专家团界面实际状态为准。

## 3. 最小验收对话

```text
一家四口去宁夏三天，预算6500，想看贺兰山文化，行程轻松，
顺便把沙坡头换成金沙岛。
```

检查：

- 能给出按天行程与预算区间；
- 能指出动态价格/班次待核实；
- 能把沙坡头替换为金沙岛；
- 过敏/忌口只记录，不强行改行程；
- 代付款/敏感凭据请求被拒绝。

## 4. 技能引擎自检

在安装缓存中定位技能后执行：

```bash
python <技能>/scripts/helan_agent/cli.py --version
python <技能>/scripts/helan_agent/multiagent/cli.py --turn "一家四口去宁夏三天，预算5000，想看贺兰山文化"
```

`--version` 应显示：

```text
package_version = 1.3.0
manifest.status = MATCH
```

## 5. 能力边界（必须如实说明）

- 该 WorkBuddy 入口是**专家团 + 随包确定性引擎**；
- 未配置模型端点/凭据时，技能引擎走 deterministic，不是真实模型自主规划；
- 专家团自身的模型能力由 WorkBuddy 平台提供，平台侧调度/授权状态以实际界面为准；
- 不能把“专家团已安装”表述为“平台云端 Agent 已自动调度”。
