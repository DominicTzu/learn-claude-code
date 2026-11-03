#!/usr/bin/env python3
"""
02. 线程：Thread、start、join 和共享对象

运行：python concurrency_basics/02_threads.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

主线程是程序最初执行的路线；Thread 可以在同一进程里另开一条路线。
适合把普通阻塞式库的等待交给其他线程，例如等待下载、文件或数据库返回。

本例同时安排 A 和 B 的等待，然后让主线程读取两份结果。
重点观察：进程号相同、线程名不同；主线程传入的字典会被工作线程直接修改。
两个线程分别写自己的字典，主线程等结束后再读，不是在同时抢写同一项。

start 是开始安排执行，join 是等线程结束；它们不是两个启动按钮。
A、B 的完成先后不保证固定。
"""

import os
import threading
import time


WAIT_SECONDS = 0.15


def blocking_job(name, delay=WAIT_SECONDS):
    """假装用一个普通库读取数据；等待期间，调用者所在的线程不能继续往下走。"""
    print(f"  {name} 开始，进程号={os.getpid()}，线程名={threading.current_thread().name}")
    time.sleep(delay)
    print(f"  {name} 结束")
    return f"{name} 的结果"


def thread_job(name, result_box):
    result = blocking_job(name)
    # result_box 是主线程传进来的同一个字典，不是自动复制出来的另一份。
    # 每个线程在此例中各写自己的字典，主线程等 join 后才读取，没有同时抢写同一项。
    result_box["result"] = result


def main():
    result_a = {}
    result_b = {}
    # target 放函数名，不加括号；args 中的值以后按顺序传给它。
    # 写 target=thread_job(...) 就会现在调用函数，不能达到这里的效果。
    thread_a = threading.Thread(target=thread_job, args=("A", result_a), name="worker-A")
    thread_b = threading.Thread(target=thread_job, args=("B", result_b), name="worker-B")

    started_at = time.perf_counter()
    thread_a.start()  # 安排另一个线程运行 thread_job("A", result_a)。
    thread_b.start()  # 不等 A 完成，接着启动 B。
    print("  主线程：我已经启动两项工作，现在再等它们交结果。")
    thread_a.join()   # 这次才等待 A 所在线程结束，不是再次启动它。
    thread_b.join()   # B 早已启动；等 A 的期间，B 也可以继续工作。

    print(f"  主线程能读到：{result_a}、{result_b}")
    print(f"  两次等待重叠，总耗时通常约 {time.perf_counter() - started_at:.2f} 秒")
    assert result_a["result"] == "A 的结果"
    assert result_b["result"] == "B 的结果"
    # 观察：进程号相同，线程名不同；A/B 的结束顺序不保证固定。
    # 如果写成 A.start -> A.join -> B.start -> B.join，就又变成一次只跑一项。
    # Thread 本身不替你保存函数的 return，也不会在 join 时把线程里的异常重新抛给你。
    # 所以真实任务通常要自己传结果/错误，或使用07_thread_pool.py 的线程池。
    # 本例使用默认的非守护线程，并明确 join；不靠程序退出时“顺便结束”后台工作。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
