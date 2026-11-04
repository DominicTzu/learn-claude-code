"""用真正的 Playwright CLI 对照上一节的 Playwright MCP。

    python s19_mcp_vs_cli/playwright_cli_demo.py
    python s19_mcp_vs_cli/playwright_cli_demo.py --url http://127.0.0.1:8000

不调用模型。通过多个 CLI 子进程，操作同一个命名浏览器会话。
需要 Node.js 18+、npm/npx、本机 Chrome；本机无 npx 的运行方式见 README。
默认只打开页面和获取快照。--click 是用户明确指定的点击目标，可能产生页面副作用。
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_COMMAND = ["npx", "-y", "@playwright/cli@0.1.23"]


def cli_environment():
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "USER", "TMPDIR", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    ) if key in os.environ}
    env["NO_UPDATE_NOTIFIER"] = "1"  # 不额外检查 npm 上的新版本
    return env


def invoke(command, session, args, workdir):
    # -s=名字 让多个短命的命令进程找到同一个后台浏览器会话。
    # --json 要求机器可解析的输出，避免通过“第几行看起来像成功”猜测结果。
    argv = [*command, f"-s={session}", "--json", *args]
    print("\n执行：", json.dumps(argv, ensure_ascii=False))
    result = subprocess.run(
        argv, cwd=workdir, env=cli_environment(), shell=False,
        stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=90, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CLI 退出码 {result.returncode}: {result.stderr or result.stdout}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"没有得到预期的 JSON 输出：{result.stdout[:1000]}") from error
    # 进程成功启动并退出，不一定等于业务操作成功，所以还检查业务错误标志。
    # open 会把业务结果包在 result 中，snapshot 则直接返回业务结果。
    operation = payload.get("result", payload) if isinstance(payload, dict) else payload
    if isinstance(operation, dict) and operation.get("isError"):
        raise RuntimeError(f"浏览器操作失败：{payload}")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered[:8000] + ("\n[显示已截断]" if len(rendered) > 8000 else ""))
    return payload


def run_demo(command, url, click=None):
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError("--cli-command 必须是非空 JSON 字符串数组")
    if shutil.which(command[0]) is None:
        raise ValueError("找不到 CLI 启动程序；请准备 Node/npm 或使用 README 中的本机命令")
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("此示例只打开 http/https 页面")

    session = f"s19-cli-{uuid.uuid4().hex[:10]}"
    results = []
    # 临时工作目录避免加载项目中已有的 CLI 配置，也隔离本示例产生的快照。
    # 这里只隔离演示文件，不是操作系统沙箱；服务器程序仍须可信。
    with tempfile.TemporaryDirectory(prefix="s19-cli-") as directory:
        workdir = Path(directory)
        try:
            # 默认 headless、临时 profile；不 attach 用户已经登录的日常浏览器。
            # idle-timeout 是额外兜底：一段时间没有命令就结束后台会话。
            results.append(invoke(command, session, ["open", url, "--browser=chrome", "--idle-timeout=15000"], workdir))
            # 导航结果可能只有快照路径。这里显式取正文，不把“有路径”当成“已读”。
            results.append(invoke(command, session, ["snapshot"], workdir))
            if click:
                results.append(invoke(command, session, ["click", click], workdir))
                results.append(invoke(command, session, ["snapshot"], workdir))
        finally:
            # CLI 的调用进程结束，不代表后台浏览器也结束。
            # 只关闭本次随机命名的会话，绝不使用 close-all/kill-all 影响其他任务。
            invoke(command, session, ["close"], workdir)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--click", help="可选：用户指定的元素 ref 或选择器，不由本例自动猜测")
    parser.add_argument("--cli-command", help="完整 CLI 启动命令的 JSON 数组，替换默认的 npx 命令")
    args = parser.parse_args()
    command = json.loads(args.cli_command) if args.cli_command else DEFAULT_COMMAND
    run_demo(command, args.url, args.click)


if __name__ == "__main__":
    main()
