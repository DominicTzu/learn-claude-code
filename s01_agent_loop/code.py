#!/usr/bin/env python3
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

Usage:
    pip install openai python-dotenv
    OPENAI_API_KEY=... python s01_agent_loop/code.py
"""

import json
import os
# subprocess 用来让 Python 启动一个真实的系统进程。
# 这里它就是 agent “操作终端”的入口：模型不能直接碰终端，
# 它只能请求调用 bash 工具；Python 收到请求后用 subprocess.run(...)
# 在当前目录执行那条 shell 命令，并把 stdout/stderr 捕获回来。
import subprocess

try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

# load_dotenv 会读取项目根目录附近的 .env 文件，并把里面的键值放进环境变量。
# OpenAI 客户端不应该把 key 写死在代码里，所以这里从 .env / 环境变量读取。
if not os.getenv("LLM_API_KEY"):
    raise RuntimeError("请在 .env 中设置 LLM_API_KEY")
if not os.getenv("LLM_MODEL_ID"):
    raise RuntimeError("请在 .env 中设置 LLM_MODEL_ID")

client = OpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL") or None,
)
MODEL = os.environ["LLM_MODEL_ID"]

SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── Tool definition: just bash ────────────────────────────
# 这是暴露给模型的“工具说明书”。
# 模型看到它之后，可以选择返回一个 tool_call，形如：
#   name = "bash"
#   arguments = {"command": "ls -la"}
# 注意：这里只是声明工具，真正执行命令的是下面的 run_bash。
TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]


# ── Tool execution ────────────────────────────────────────
def run_bash(command: str) -> str:
    # 一个很薄的安全挡板：阻止明显危险的命令。
    # 这不是完整沙箱，只是这个教学示例里最小的防护。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # subprocess.run 会启动一个子进程来执行命令。
        #
        # shell=True:
        #   让命令交给系统 shell 解析，所以 "ls -la"、"pwd && echo hi"
        #   这类终端命令字符串可以直接运行。
        #
        # cwd=os.getcwd():
        #   命令在当前项目目录执行，而不是在任意目录执行。
        #
        # capture_output=True:
        #   不让命令输出直接刷到屏幕，而是捕获到 r.stdout / r.stderr。
        #   这样 agent 才能把终端结果再喂回模型。
        #
        # text=True:
        #   把输出按字符串处理，而不是 bytes。
        #
        # timeout=120:
        #   防止命令一直卡住。
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        # stdout 是正常输出，stderr 是错误输出。
        # 对模型来说两者都重要，所以合并后作为工具结果返回。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ── The core pattern: a while loop that calls tools until the model stops ──
def agent_loop(messages: list):
    # messages 是对话历史。每一轮都把历史 + system prompt 发给模型。
    # 如果模型只回复文本，循环结束；如果模型请求调用工具，就执行工具，
    # 再把工具结果追加进 messages，继续问模型下一步要做什么。
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}, *messages],
            tools=TOOLS,
            max_tokens=8000,
        )
        assistant_message = response.choices[0].message

        # 记录模型这一轮的回复。
        # 如果这一轮包含 tool_calls，也必须把 assistant 的 tool_calls 原样放进历史；
        # 后面的 tool 结果要用 tool_call_id 对上这次请求。
        messages.append(assistant_message.model_dump(exclude_none=True))

        # 没有 tool_calls，说明模型已经给出最终答案，不需要再操作终端。
        if not assistant_message.tool_calls:
            return

        # 有 tool_calls，说明模型在说：“请帮我执行这些工具调用”。
        # 这里 Python 充当执行器，把模型给出的 JSON 参数转成真实函数调用。
        for tool_call in assistant_message.tool_calls:
            if tool_call.function.name != "bash":
                output = f"Error: unknown tool {tool_call.function.name}"
            else:
                # OpenAI 返回的 arguments 是 JSON 字符串，需要先解析成 dict。
                args = json.loads(tool_call.function.arguments or "{}")
                command = args.get("command", "")

                # 这行只是把即将执行的命令打印给人看。
                # 真正执行发生在 run_bash(command) 里面。
                print(f"\033[33m$ {command}\033[0m")
                output = run_bash(command)
                print(output[:200])

            # 把终端执行结果作为 role="tool" 的消息放回历史。
            # 下一轮模型会看到这个结果，然后决定继续调用 bash，还是输出最终答案。
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 用户输入进入 messages；agent_loop 会不断补充 assistant/tool 消息。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # Print the model's final text response
        response_content = history[-1]["content"]
        if response_content:
            print(response_content)
        print()
