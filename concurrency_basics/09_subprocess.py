#!/usr/bin/env python3
"""
09. subprocess：运行现成的外部命令

运行：python concurrency_basics/09_subprocess.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：调用 git、ffmpeg 等已经存在的命令行程序。
它和“把 Python 函数交给进程池”不是同一种接口。

本例用当前 Python 的路径启动另一个解释器，让它计算 6 * 7 并打印。
父进程等待命令结束，拿到标准输出和退出码；不运行任意用户输入的命令。
"""

import subprocess
import sys


def main():
    # sys.executable 是当前 Python 解释器的路径；用 -c 让另一个解释器执行一小段代码。
    # 这里不会运行任意外部输入，也不需要 shell=True。
    command = [sys.executable, "-c", "print(6 * 7)"]
    completed = subprocess.run(
        command,
        capture_output=True,  # 取回 stdout/stderr，不直接让子进程把全部输出印到屏幕上。
        text=True,            # 把输出作为字符串返回。
        check=True,           # 非 0 退出码时抛 CalledProcessError。
        timeout=5,
    )
    print(f"  子进程输出={completed.stdout.strip()}，退出码={completed.returncode}")
    assert completed.stdout.strip() == "42"
    # run 会等这个命令结束。因此“用了子进程”不等于“调用处不阻塞”。
    # 运行 ffmpeg/git 等现成程序常用 subprocess；把 Python 计算函数交给多核常用进程池。
    # 想先启动命令再做别的，可以用 Popen；但还需处理输出读取、等待、超时和子进程收尾。
    # 参数列表避免 shell 解释 |、> 等符号，不代表任何外部命令本身都是安全的。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
