#!/usr/bin/env python3
"""
s09_memory.py - Memory System

Persistent, cross-session knowledge for the coding agent.

Storage:
    .memory/
      MEMORY.md          ← index (one line per memory, ≤200 lines)
      feedback_tabs.md    ← individual memory files (Markdown + YAML frontmatter)
      user_profile.md
      project_facts.md

Flow in agent_loop:
    1. Load MEMORY.md index into SYSTEM prompt (cheap, always present)
    2. Select relevant memories by filename/description → inject content
    3. Run compression pipeline from s08
    4. After each turn ends → extract new memories from original messages
    5. Periodically consolidate (Dream)

Builds on s08 (context compact). Usage:

    python s09_memory/code.py
    Needs: pip install openai python-dotenv + .env 中配置 LLM_API_KEY、
           LLM_BASE_URL 和 LLM_MODEL_ID
"""

import copy
import json
import os
import re
import subprocess
import time
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off') # 避免 readline 在 Windows 上报错
except ImportError:
    pass

from dotenv import load_dotenv
from openai import OpenAI

# 从 .env 读取 OpenAI 兼容服务配置；DeepSeek 等服务只需额外设置 LLM_BASE_URL。
load_dotenv(override=True)
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]


# ═══════════════════════════════════════════════════════════
#  NEW in s09: Memory System
# ═══════════════════════════════════════════════════════════

MEMORY_TYPES = ["user", "feedback", "project", "reference"]

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 Markdown 顶部的 YAML frontmatter，返回元数据和正文。
    整体逻辑是：
    1. 检查文本是否以 "---" 开头
    2. 分割文本为三部分：前言、元数据、正文
    3. 解析元数据为字典
    4. 返回元数据和正文
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2) # 分割为三部分：前言、元数据、正文
    if len(parts) < 3: # 如果分割后的部分少于三部分，说明没有正文，返回空字典和原始文本
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():# 解析元数据部分的每一行，提取键值对
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """将一条长期记忆写成独立 Markdown 文件，并立即重建轻量索引。"""
    # 文件名只从模型给出的 name 派生，不接受完整路径，避免写出 .memory 目录。
    slug = name.lower().replace(" ", "-").replace("/", "-") # slug的字面意思是“蜗牛”，在这里指的是一种简化的、适合用作文件名的字符串形式。它将名称转换为小写，并将空格和斜杠替换为连字符，以确保文件名安全且易于识别。
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename 
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    _rebuild_index()
    return filepath


def _rebuild_index():
    """从所有完整记忆文件生成 MEMORY.md；索引只保留名称和一句简介。"""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index() -> str:
    """读取每轮都放入 SYSTEM 的低成本索引，而非把所有正文常驻上下文。"""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """按索引中已有的文件名读取完整记忆，供本轮按需注入。"""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()


def list_memory_files() -> list[dict]:
    """读取记忆目录并标准化为供检索、提取、合并使用的字典列表。"""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """Select relevant memory filenames by matching recent conversation against
    memory names/descriptions. Uses a simple LLM call (or falls back to keyword
    matching on name+description)."""
    files = list_memory_files()
    if not files:
        return []

    # 只依据最近几条用户输入做检索，避免很早的旧任务把当前记忆选择带偏。
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    # 给模型的只是索引（名称 + 简介），模型先挑文件，再加载被选中的完整正文。
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = response.choices[0].message.content or ""
        # Extract JSON array from response
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        pass

    # 检索模型调用失败时仍可用简单关键词匹配；这不是语义检索，但可保证演示可继续运行。
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    """将本轮相关记忆包装为显式标签，供 agent 作为临时上下文使用。"""
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list):
    """从刚结束的对话提取新的长期事实；模型返回 [] 时不产生任何文件。"""
    # 只检查最近十条，控制提取任务成本，也减少把已过期工作误写入记忆的概率。
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # 已有简介一起发送，帮助模型判断“这是新事实”还是“已有记忆的重复表述”。
    existing = list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=800
        )
        text = response.choices[0].message.content or ""
        # Extract JSON array from response
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass


CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    """记忆数量达到阈值时合并重复项、删除过期项，类似离线整理（Dream）。"""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=3000
        )
        text = response.choices[0].message.content or ""
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # 只有模型成功产出可解析的合并结果后才删除旧文件；MEMORY.md 会由写入函数重建。
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        pass


# Build SYSTEM with memory index
def build_system() -> str:
    """把长期记忆的索引放入 system；完整正文仅在当前任务相关时才注入。"""
    index = read_memory_index()
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}\n"
        "Relevant memories are injected below. Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, extract it as a memory."
    )

SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s08 (skeleton): Basic tools
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # 将 ../ 和符号链接解析为真实路径后，再限制在当前工作区内。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    try:
        # 模型提出命令，Python 的 subprocess.run 才真正控制终端。
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired: return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines): lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e: return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path); file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content); return f"Wrote {len(content)} bytes to {path}"
    except Exception as e: return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text: return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e: return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e: return f"Error: {e}"

def extract_text(content) -> str:
    """OpenAI Chat Completions 的最终回答是 assistant message.content 字符串。"""
    return content if isinstance(content, str) else ""

# Subagent (simplified from s06-s07)
SUB_TOOLS = [
    # 子 Agent 有独立上下文和较小工具集，不能继续创建子 Agent。
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
]
SUB_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}

def spawn_subagent(description: str) -> str:
    """以新 messages[] 运行子任务，父 Agent 最终只收到它的文本摘要。"""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUB_SYSTEM}, *messages],
            tools=SUB_TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message
        # assistant 的 tool_calls 必须与后续 role="tool" 的 tool_call_id 成对保存在历史中。
        messages.append(assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            break
        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as error:
                output = f"Error: invalid tool arguments: {error}"
            else:
                handler = SUB_HANDLERS.get(name)
                output = handler(**args) if handler else f"Unknown: {name}"
                print(f"  \033[90m[sub] {name}: {str(output)[:100]}\033[0m")
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": output})
    result = extract_text(messages[-1].get("content"))
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg.get("content"))
                if result: break
        if not result: result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


# ═══════════════════════════════════════════════════════════
#  FROM s08 (skeleton): Compaction pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 50000
KEEP_RECENT = 3
PERSIST_THRESHOLD = 30000

def estimate_size(msgs):
    """教学版使用字符数近似 token 数，仅用于决定是否进入 L4 摘要。"""
    return len(str(msgs))

def _message_has_tool_use(msg):
    """OpenAI 中 assistant.tool_calls 表示其后必须保留对应的工具结果。"""
    return msg.get("role") == "assistant" and bool(msg.get("tool_calls"))

def _is_tool_result_message(msg):
    """OpenAI 的工具输出不是 block，而是一条独立 role='tool' 消息。"""
    return msg.get("role") == "tool"

def snip_compact(msgs, mx=50):
    """L1：裁剪中段消息，但不允许切断 assistant tool_calls 与 role='tool' 结果。"""
    if len(msgs) <= mx: return msgs
    head_end, tail_start = 3, len(msgs) - (mx - 3)
    # 头部若以工具调用结束，把所有紧随的工具结果一起保留。
    if head_end > 0 and _message_has_tool_use(msgs[head_end - 1]):
        while head_end < len(msgs) and _is_tool_result_message(msgs[head_end]):
            head_end += 1
    # 尾部若从某条工具结果切入，回退到整组结果及其前面的 assistant 调用。
    if tail_start < len(msgs) and _is_tool_result_message(msgs[tail_start]):
        while tail_start > 0 and _is_tool_result_message(msgs[tail_start - 1]):
            tail_start -= 1
        if tail_start > 0 and _message_has_tool_use(msgs[tail_start - 1]):
            tail_start -= 1
    if head_end >= tail_start:
        return msgs
    return msgs[:head_end] + [{"role": "user", "content": f"[snipped {tail_start - head_end} msgs]"}] + msgs[tail_start:]

def collect_tool_results(msgs):
    """收集 role='tool' 消息，供 L2 对较旧输出做内容级压缩。"""
    results = []
    for mi, msg in enumerate(msgs):
        if msg.get("role") == "tool":
            results.append((mi, msg))
    return results

def micro_compact(msgs):
    """L2：只缩短旧工具输出的 content，保留 role 和 tool_call_id 的结构。"""
    tr = collect_tool_results(msgs)
    if len(tr) <= KEEP_RECENT: return msgs
    for _, message in tr[:-KEEP_RECENT]:
        if len(str(message.get("content", ""))) > 120:
            message["content"] = "[Earlier tool result compacted.]"
    return msgs

def persist_large(tid, out):
    """L3 辅助函数：将超大输出落盘，并把路径与预览留在模型上下文中。"""
    if len(out) <= PERSIST_THRESHOLD: return out
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    p = TOOL_RESULTS_DIR / f"{tid}.txt"
    if not p.exists(): p.write_text(out)
    return f"<persisted-output>\nFull: {p}\nPreview:\n{out[:2000]}\n</persisted-output>"

def tool_result_budget(msgs, mx=200_000):
    """L3：控制当前轮连续工具结果的总大小，优先持久化最大的输出。"""
    start = len(msgs)
    while start > 0 and msgs[start - 1].get("role") == "tool":
        start -= 1
    results = [(i, msgs[i]) for i in range(start, len(msgs))]
    total = sum(len(str(message.get("content", ""))) for _, message in results)
    if total <= mx: return msgs
    for _, message in sorted(results, key=lambda pair: len(str(pair[1].get("content", ""))), reverse=True):
        if total <= mx: break
        c = str(message.get("content", ""))
        if len(c) <= PERSIST_THRESHOLD: continue
        message["content"] = persist_large(message.get("tool_call_id", "?"), c)
        total = sum(len(str(item.get("content", ""))) for _, item in results)
    return msgs

def write_transcript(msgs):
    """在 L4 摘要前写入完整历史，避免摘要后无法追溯原始细节。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with p.open("w") as f:
        for m in msgs: f.write(json.dumps(m, default=str) + "\n")
    return p

def summarize_history(msgs):
    """L4：用单独的 Chat Completions 请求生成可继续工作的交接摘要。"""
    conv = json.dumps(msgs, default=str)[:80000]
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this coding-agent conversation so work can continue.\n"
            "Preserve: 1. current goal, 2. key findings, 3. files changed, 4. remaining work, 5. user constraints.\n\n" + conv}],
        max_tokens=2000,
    )
    return response.choices[0].message.content or "(empty summary)"

def compact_history(msgs):
    """L4：以一条摘要替换完整历史，释放最多上下文空间。"""
    write_transcript(msgs)
    summary = summarize_history(msgs)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

def reactive_compact(msgs):
    """服务端仍提示上下文超长时的兜底策略：摘要旧部分并保留最新五条。"""
    write_transcript(msgs)
    tail_start = max(0, len(msgs) - 5)
    if tail_start < len(msgs) and _is_tool_result_message(msgs[tail_start]):
        while tail_start > 0 and _is_tool_result_message(msgs[tail_start - 1]):
            tail_start -= 1
        if tail_start > 0 and _message_has_tool_use(msgs[tail_start - 1]):
            tail_start -= 1
    summary = summarize_history(msgs[:tail_start])
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *msgs[tail_start:]]


# ═══════════════════════════════════════════════════════════
#  Tool Definitions (skeleton — fewer tools to focus on memory)
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # OpenAI function schema 只描述能力；模型生成 tool_calls 后再由 TOOL_HANDLERS 调度。
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace exact text in a file once.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
    }},
    {"type": "function", "function": {
        "name": "glob", "description": "Find files matching a glob pattern.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "task", "description": "Launch a subagent to handle a subtask.",
        "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
    }},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "task": spawn_subagent,
}


# ═══════════════════════════════════════════════════════════
#  agent_loop — s09: inject memories + extract after each turn
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1

def agent_loop(messages: list):
    """主循环：本轮检索记忆，压缩历史，执行工具，结束后提取新的长期记忆。"""
    reactive_retries = 0
    # 相关记忆只加到发送给模型的副本，避免它永久写回普通对话历史并反复膨胀。
    memories_content = load_memories(messages)
    memory_turn = len(messages) - 1 if messages and isinstance(messages[-1].get("content"), str) else None
    # 索引在每轮开始构建；本轮结束后如写入了新记忆，下一个用户回合会看到新索引。
    system = build_system()

    while True:
        # 提取记忆应基于未压缩原文。deepcopy 防止 L2/L3 原地修改工具输出后污染快照。
        pre_compress = copy.deepcopy(messages) # 深拷贝的逻辑是：在内存中创建一个新的对象，该对象具有与原始对象相同的数据，但它们在内存中的位置是不同的。

        # s08: compression pipeline (budget → snip → micro)
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)

        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        try:
            request_messages = messages
            if memories_content and memory_turn is not None and memory_turn < len(messages):
                # copy() 只复制列表；下面替换当前 user 消息字典，原 messages 不会含 memory 正文。
                request_messages = messages.copy()
                request_messages[memory_turn] = {
                    **messages[memory_turn],
                    "content": memories_content + "\n\n" + messages[memory_turn]["content"],
                }
            # system 每次临时拼入，不写进可压缩 history；这与 s08 的调用方式一致。
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system}, *request_messages],
                tools=TOOLS,
                max_tokens=8000,
            )
            reactive_retries = 0
        except Exception as e:
            if ("prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()) and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise

        assistant_message = response.choices[0].message
        # 原样保留 tool_calls，后面的 role="tool" 消息需要用 tool_call_id 回指它。
        messages.append(assistant_message.model_dump(exclude_none=True))
        if not assistant_message.tool_calls:
            # s09: extract from pre-compression snapshot for full fidelity
            extract_memories(pre_compress)
            consolidate_memories()
            return

        for tool_call in assistant_message.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")
            try:
                # function.arguments 是 JSON 字符串；错误也要回传给模型，方便下一轮修正。
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as error:
                output = f"Error: invalid tool arguments: {error}"
            else:
                handler = TOOL_HANDLERS.get(name)
                output = handler(**args) if handler else f"Unknown: {name}"
            print(str(output)[:200])
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })


if __name__ == "__main__":
    print("s09: Memory — persistent cross-session knowledge")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    while True:
        try: query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt): break
        if query.strip().lower() in ("q", "exit", ""): break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1].get("content")
        if response_content:
            print(response_content)
        print()
