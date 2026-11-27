# -*- coding: utf-8 -*-
"""
贺兰有灵 · 可信知识问答 CLI（工具契约入口）

供 WorkBuddy Skill / MCP 适配层 / 测试 runner 调用。

用法：
  python cli.py "贺兰山岩羊有哪些生态特点"
  python cli.py "西夏陵是世界遗产吗" --pretty
  python cli.py "沙坡头门票多少钱" --json
  python cli.py --version          # 运行时版本自证（只读，无副作用）

输出：JSON（默认）或可读文本（--pretty）。
退出码：0 正常；2 输入为空；3 检索异常。
"""
import argparse
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from qa_engine import answer


def main():
    ap = argparse.ArgumentParser(description="贺兰有灵 可信知识问答")
    ap.add_argument("query", nargs="?", default=None, help="用户自然语言查询")
    ap.add_argument("--top_k", type=int, default=5, help="召回数量")
    ap.add_argument("--json", action="store_true", help="强制 JSON 输出")
    ap.add_argument("--pretty", action="store_true", help="可读文本输出")
    ap.add_argument("--version", action="store_true",
                    help="输出运行时版本自证信息（只读：包版本/引擎哈希/清单比对）")
    args = ap.parse_args()

    if args.version:
        from release import build_version_info
        print(json.dumps(build_version_info(), ensure_ascii=False, indent=2))
        return 0

    if not args.query or not args.query.strip():
        print(json.dumps({"error": "empty_query", "status": "failed"}, ensure_ascii=False))
        return 2

    try:
        res = answer(args.query.strip(), top_k=args.top_k)
    except Exception as e:  # 工具异常不得伪装成功
        print(json.dumps({
            "error": "tool_error",
            "message": str(e),
            "status": "failed",
            "recommendation": "检索/生成环节异常，请稍后重试或改用官方渠道核实。",
        }, ensure_ascii=False))
        return 3

    if args.pretty and not args.json:
        print("【查询】", res["query"])
        print("【主题】", res["detected_topic"])
        g = res["gate"]
        print("【证据门】grounding=%s | 置信=%s | 冲突=%s | 动态信息=%s | 可断言=%s" % (
            g["grounding"], g["confidence"], g["conflict_detected"], g["realtime_flag"], g["can_assert"]))
        print("【回答】")
        print(res["grounded_answer"])
        print("\n【引用】")
        for c in res["citations"]:
            print(f"  [{c['index']}] {c['title']} — {c['source_name']} ({'已核验' if c['verified'] else '待核实'})")
            if c["source_url"]:
                print(f"       {c['source_url']}")
        if g["warnings"]:
            print("\n【警示】")
            for w in g["warnings"]:
                print("  -", w)
        return 0

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
