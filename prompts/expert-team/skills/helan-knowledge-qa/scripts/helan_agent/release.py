# -*- coding: utf-8 -*-
"""发布版本信息与运行时自证（release traceability）。

用途（对应 V21.0 可追溯版本专项）：
  专家团随包分发后，任何会话都可以用一条无副作用命令回答：
    "当前执行的到底是哪个版本？内容与发布清单是否一致？"

  信息全部来自**实际执行环境**，不依赖任何外部服务：
    1. `engine_root`   —— 本模块所在目录（证明当前从哪个安装缓存目录执行）；
    2. `engine_sha256` —— 对引擎全部 .py 文件实时计算的组合哈希；
    3. `corpus_sha256` —— 对随包语料全部 .json 实时计算的组合哈希；
    4. `package_version` —— 向上定位包根 `.codebuddy-plugin/plugin.json` 的 version；
    5. `manifest`      —— 读取包内 `release-manifest.json`（构建时生成），
                         逐项比对实际哈希，输出 `manifest_match`。

设计约束：
  - 只读：不写任何文件、不发任何网络请求、无第三方依赖；
  - 开发源码树中同样可运行（找不到包根时 `package_version=None`，
    `manifest` 标记 `dev-tree`，其余字段照常输出）；
  - 哈希口径与 `scripts/release_expert.py` / `scripts/sync_check.py` 完全一致
    （排序后的 `relpath\0sha256\n` 行做 SHA-256），保证三处可互证。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
# 包布局：<pkg>/skills/helan-knowledge-qa/scripts/helan_agent/release.py
#   dirname#1=helan_agent  dirname#2=scripts  dirname#3=helan-knowledge-qa（技能目录）
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST_NAME = "release-manifest.json"
MANIFEST_PATH = os.path.join(_SKILL_DIR, MANIFEST_NAME)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _combined_hash(root: str, suffixes: tuple[str, ...]) -> dict:
    """对目录内指定后缀文件计算组合哈希（排序后逐行 relpath\\0hash）。"""
    lines = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if not fn.endswith(suffixes):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            lines.append("%s\0%s" % (rel, _sha256_file(p)))
    lines.sort()
    h = hashlib.sha256()
    for line in lines:
        h.update((line + "\n").encode("utf-8"))
    return {"files": len(lines), "sha256": h.hexdigest()}


def _find_package_root() -> str | None:
    """从本模块向上寻找含 `.codebuddy-plugin/plugin.json` 的包根（最多 6 层）。"""
    cur = _ENGINE_DIR
    for _ in range(6):
        cand = os.path.join(cur, ".codebuddy-plugin", "plugin.json")
        if os.path.isfile(cand):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _corpus_dir() -> str:
    cand = os.path.join(os.path.dirname(_ENGINE_DIR), "corpus")
    if os.path.isdir(cand):
        return cand
    return os.path.join(os.path.dirname(os.path.dirname(_ENGINE_DIR)), "corpus")


def build_version_info() -> dict:
    """汇总运行时版本证据（只读）。"""
    pkg_root = _find_package_root()
    pj_version = None
    pj_path = None
    if pkg_root:
        pj_path = os.path.join(pkg_root, ".codebuddy-plugin", "plugin.json")
        try:
            with open(pj_path, encoding="utf-8") as f:
                pj_version = (json.load(f) or {}).get("version")
        except (OSError, json.JSONDecodeError):
            pj_version = None

    engine = _combined_hash(_ENGINE_DIR, (".py",))
    corpus = _combined_hash(_corpus_dir(), (".json",))

    info = {
        "package": "helan-lingyou-ningxia",
        "package_version": pj_version,
        "plugin_json": pj_path,
        "engine_root": _ENGINE_DIR,
        "engine": engine,
        "corpus": corpus,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 与构建期发布清单比对
    if not os.path.isfile(MANIFEST_PATH):
        info["manifest"] = {"status": "dev-tree" if pkg_root is None else "MISSING",
                            "manifest_path": MANIFEST_PATH}
        return info
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        info["manifest"] = {"status": "UNREADABLE", "error": str(e)}
        return info

    checks = {
        "package_version": (man.get("version"), pj_version),
        "engine_sha256": ((man.get("engine") or {}).get("sha256"), engine["sha256"]),
        "engine_files": ((man.get("engine") or {}).get("files"), engine["files"]),
        "corpus_sha256": ((man.get("corpus") or {}).get("sha256"), corpus["sha256"]),
        "corpus_files": ((man.get("corpus") or {}).get("files"), corpus["files"]),
    }
    mismatches = [k for k, (exp, act) in checks.items() if exp != act]
    info["manifest"] = {
        "status": "MATCH" if not mismatches else "MISMATCH",
        "manifest_path": MANIFEST_PATH,
        "manifest_version": man.get("version"),
        "manifest_git_commit": man.get("git_commit"),
        "manifest_built_at": man.get("built_at"),
        "mismatches": mismatches,
        "checks": {k: {"expected": e, "actual": a} for k, (e, a) in checks.items()},
    }
    return info
