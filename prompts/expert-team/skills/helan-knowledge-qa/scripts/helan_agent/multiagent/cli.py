# -*- coding: utf-8 -*-
"""
CLI 入口：演示 A0→A1→A3→A5（及 A2/A4）真实跨 Agent 闭环。

运行：
  python cli.py            # 跑通两轮演示并落盘证据
  python cli.py --turn "..."  # 追加一轮对话
"""
import sys
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))  # 让 qa_engine 等可被导入
RESULT_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "evaluation", "results"))
os.makedirs(RESULT_DIR, exist_ok=True)

from engine import Session


def main():
    sess = Session(session_id="cross_agent_demo", persist_dir=RESULT_DIR)
    turns = [
        "一家四口去宁夏玩三天，预算5000元，想了解贺兰山文化，希望安排住宿、交通、特色美食及亲子研学，行程不要太累。",
        "第二天我还想去沙坡头，尽量不超预算。",
        # 第三轮：文化依据追问（纯文化问答，应走简明格式，不套用旅行报告结构）
        "为什么推荐这些景点？有没有可靠的文化资料？",
    ]
    runs = []
    for t in turns:
        print("\n" + "=" * 70)
        print("用户输入：", t)
        print("=" * 70)
        res = sess.ask(t)
        report = sess.final_report(res)
        print(report)
        print("\n[task_id=%s | state_version=%d | elapsed=%sms | release_pass=%s]" % (
            res["task_id"], res["state_version"], res["elapsed_ms"], res["release_pass"]))
        runs.append(res)

    # 落盘完整 trace 与门禁证据
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trace_path = os.path.join(RESULT_DIR, f"cross_agent_trace_{stamp}.json")
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)
    sum_path = os.path.join(RESULT_DIR, f"cross_agent_summary_{stamp}.txt")
    with open(sum_path, "w", encoding="utf-8") as f:
        for i, res in enumerate(runs, 1):
            f.write(f"== 第{i}轮 task_id={res['task_id']} intent={res['intent']} release={res['release_pass']} ==\n")
            g = res["gates"]
            f.write(f"Execution={g['execution']['pass']} Business={g['business']['pass']} Release={g['release']['pass']}\n")
            a5 = res["results"].get("A5", {})
            f.write(f"A5 verdict={a5.get('verdict')} issues={len(a5.get('issues', []))}\n")
            f.write("\n")
    print("\n[证据已落盘]\n  trace: %s\n  summary: %s" % (trace_path, sum_path))
    return runs


if __name__ == "__main__":
    main()
