# -*- coding: utf-8 -*-
"""
唯一状态存储 StateStore（冻结版 v1.0）

职责（对照任务书「二、编排与通信」）：
- 保存全局约束、行程、会话轮次、各 Agent 状态、审核与门禁结果。
- 所有写操作必须递增 state_version，并追加执行日志（append-only）。
- 子 Agent 只能通过 A0 下发的消息间接读取/请求变更，不得越权直接改写。
- 支持快照与持久化（JSON），保证可复现、可审计。
"""
import json
import os
import time
from typing import Any, Dict, List, Optional


class StateStore:
    def __init__(self, session_id: str = "default", persist_path: Optional[str] = None):
        self.session_id = session_id
        self.persist_path = persist_path
        self._state: Dict[str, Any] = {
            "session_id": session_id,
            "run_id": "",
            "state_version": 0,
            "user_constraints": {},
            "itinerary": {},
            "conversation_turns": [],
            "agent_status": {},
            "verification": {},
            "gates": {},
            "realtime_data": {},
            "execution_mode": "",
            # V26.0 §五：轻量任务账本（任务级可追溯 + 幂等 + 重试 + 失败传播）
            "task_ledger": [],
        }
        self._log: List[Dict[str, Any]] = []  # 执行日志（append-only）
        if persist_path and os.path.exists(persist_path):
            self._load()

    # ---------- 读取 ----------
    def get(self, key: str, default=None):
        return self._state.get(key, default)

    @property
    def state_version(self) -> int:
        return self._state["state_version"]

    @property
    def constraints(self) -> Dict[str, Any]:
        return self._state["user_constraints"]

    @property
    def itinerary(self) -> Dict[str, Any]:
        return self._state["itinerary"]

    @property
    def log(self) -> List[Dict[str, Any]]:
        return self._log

    @property
    def task_ledger(self) -> List[Dict[str, Any]]:
        """任务账本：每个 Agent 任务一条，含 parent/dedup/attempts/status/error_code。"""
        return self._state.setdefault("task_ledger", [])

    # ---------- 任务账本（V26.0 §五：幂等 / 超时 / 重试 / 失败传播） ----------
    def record_task(self, *, task_id: str, parent_task_id: str, agent_id: str,
                    run_id: str, status: str, execution_mode: str = "",
                    attempts: int = 1, timeout_ms: int = 0, error_code: str = "",
                    tool_call_id: str = "", detail: Dict[str, Any] = None) -> Dict[str, Any]:
        """登记/更新一条任务账本记录（同 task_id+agent_id 幂等覆盖，保留首次创建时间）。"""
        ledger = self.task_ledger
        for row in ledger:
            if row["task_id"] == task_id and row["agent_id"] == agent_id:
                row.update({
                    "status": status,
                    "attempts": attempts,
                    "error_code": error_code,
                    "execution_mode": execution_mode or row.get("execution_mode", ""),
                    "tool_call_id": tool_call_id or row.get("tool_call_id", ""),
                    "updated_at": time.time(),
                })
                if detail:
                    row.setdefault("detail", {}).update(detail)
                self._persist()
                return row
        row = {
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "dedup_key": "%s|%s|%s" % (run_id, task_id, agent_id),
            "status": status,
            "execution_mode": execution_mode,
            "attempts": attempts,
            "timeout_ms": timeout_ms,
            "error_code": error_code,
            "tool_call_id": tool_call_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "detail": detail or {},
        }
        ledger.append(row)
        self.append_log("TASK", agent_id,
                        {"task_id": task_id, "status": status, "attempts": attempts,
                         "error_code": error_code}, state_version=self.state_version)
        self._persist()
        return row

    def mark_downstream_skipped(self, parent_task_id: str, reason_code: str, by: str = "A0"):
        """失败传播：把依赖失败任务的后续任务统一标记为 SKIPPED（不静默丢弃）。"""
        n = 0
        for row in self.task_ledger:
            if row.get("parent_task_id") == parent_task_id and row.get("status") == "PENDING":
                row["status"] = "SKIPPED"
                row["error_code"] = reason_code
                row["updated_at"] = time.time()
                n += 1
        if n:
            self.append_log("TASK", by, {"op": "propagate_skip", "count": n,
                                         "error_code": reason_code},
                            state_version=self.state_version)
            self._persist()
        return n

    # ---------- 运行标识（V26.0） ----------
    def set_run(self, run_id: str, by: str = "A0", execution_mode: str = ""):
        """登记本轮运行 ID 与整体执行模式（不做 state_version 递增，属元数据）。"""
        self._state["run_id"] = run_id
        if execution_mode:
            self._state["execution_mode"] = execution_mode
        self.append_log("RUN", by, {"run_id": run_id, "execution_mode": self._state.get("execution_mode", "")},
                        state_version=self.state_version)
        return run_id

    # ---------- 写入（必须递增版本） ----------
    def _bump(self) -> int:
        self._state["state_version"] += 1
        return self._state["state_version"]

    def set_constraints(self, constraints: Dict[str, Any], by: str = "A3"):
        self._state["user_constraints"] = constraints
        v = self._bump()
        self.append_log("STATE", by, {"op": "set_constraints", "keys": list(constraints.keys())}, state_version=v)
        self._persist()
        return v

    def set_itinerary(self, itinerary: Dict[str, Any], by: str = "A3"):
        self._state["itinerary"] = itinerary
        v = self._bump()
        self.append_log("STATE", by, {"op": "set_itinerary", "days": itinerary.get("days")}, state_version=v)
        self._persist()
        return v

    def set_realtime(self, key: str, value: Any, by: str = "A4"):
        self._state["realtime_data"][key] = value
        v = self._bump()
        self.append_log("STATE", by, {"op": "set_realtime", "key": key}, state_version=v)
        self._persist()
        return v

    def set_agent_status(self, agent: str, status: Dict[str, Any]):
        self._state["agent_status"][agent] = status
        self.append_log("AGENT", agent, {"status": status.get("status"), "task": status.get("task_id")}, state_version=self.state_version)

    def set_verification(self, result: Dict[str, Any], by: str = "A5"):
        self._state["verification"] = result
        v = self._bump()
        self.append_log("VERIFY", by, {"verdict": result.get("verdict")}, state_version=v)
        self._persist()
        return v

    def set_gates(self, gates: Dict[str, Any], by: str = "A0"):
        self._state["gates"] = gates
        v = self._bump()
        self.append_log("GATE", by, {k: g.get("pass") for k, g in gates.items()}, state_version=v)
        self._persist()
        return v

    def add_turn(self, role: str, text: str):
        self._state["conversation_turns"].append({"role": role, "text": text, "ts": time.time()})
        self.append_log("TURN", role, {"len": len(text)}, state_version=self.state_version)
        self._persist()

    # ---------- 日志 ----------
    def append_log(self, kind: str, actor: str, detail: Dict[str, Any], state_version: int = None):
        self._log.append({
            "ts": time.time(),
            "kind": kind,
            "actor": actor,
            "state_version": state_version if state_version is not None else self.state_version,
            "detail": detail,
        })

    # ---------- 快照/持久化 ----------
    def snapshot(self) -> Dict[str, Any]:
        return {"state": self._state, "log": self._log}

    def _persist(self):
        if not self.persist_path:
            return
        os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, ensure_ascii=False, indent=2)

    def _load(self):
        with open(self.persist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._state = data.get("state", self._state)
        self._log = data.get("log", [])
