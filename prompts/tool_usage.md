# Tool Usage Rules — 工具调用规范

## 统一工具契约
所有问答类工具后端均为 `domain/helan_agent/` 的 Python 引擎（纯标准库 BM25 + 三层证据门），通过 CLI 调用：

```
python domain/helan_agent/cli.py "<用户查询>" --pretty
```

## 调用纪律
1. **先检索后回答**：任何宁夏事实必须先调用工具，再组织语言。
2. **使用工具返回的证据**：回答中的事实须能在 `grounded_answer` / `citations` 中找到依据。
3. **保留引用**：最终回答必须呈现来源（机构/媒体）与核验状态。
4. **忠实于证据门**：
   - `realtime_flag=true` → 动态信息标注"待核实"。
   - `conflict_detected=true` → 呈现差异，不下单一确定结论。
   - `grounding=insufficient_evidence` → 如实说明无法核实。
5. **不伪造成功**：工具异常（exit code 3）时，明确告知"检索异常"，不得用模型生成内容冒充工具结果。

## 错误处理
- 空查询（exit 2）：提示用户补充问题。
- 工具异常（exit 3）：降级回答 + 建议官方渠道。
- 超时（>20s）：中断并给出受控回答。

## 安全
- 严禁将 Token/密钥写入代码或提交材料。
- 严禁未经授权上传私人/企业敏感资料。
