#!/usr/bin/env python3
"""
07. 线程池与 Future：控制线程数量，并拿回函数返回值

运行：python concurrency_basics/07_thread_pool.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：有很多普通阻塞式任务，不想每来一个任务就手动创建一个线程。
“池”保留有限数量的工作线程，让它们接着领取任务。
Future 记录某项工作的状态，之后可通过 result() 拿到返回值或收到异常。

本例先提交 A、B 两份工作，再分别拿结果。等待 A 时，B 仍能继续推进。
普通 CPython 3.10 的 GIL 限制多个线程同时执行 Python 字节码；
所以这里的收益是让等待重叠，不是让纯 Python 计算自动使用多个 CPU 核。
某些原生扩展会释放 GIL，需要按具体库分析。
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor # 用于创建线程池和 Future 对象


WAIT_SECONDS = 0.15


def blocking_job(name, delay=WAIT_SECONDS):
    """假装用一个普通库读取数据；等待期间，调用者所在的线程不能继续往下走。"""
    print(f"  {name} 开始，进程号={os.getpid()}，线程名={threading.current_thread().name}")
    time.sleep(delay)
    print(f"  {name} 结束")
    return f"{name} 的结果"


def main():
    # 最多使用两个工作线程，不是来一个任务就无限制创建一个线程。
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(blocking_job, "A") # thread没有返回值，但 future 有。
        future_b = pool.submit(blocking_job, "B") # submit安排工作，不一定立即执行；线程池会按空闲线程来安排。而thread.start 立即启动线程。
        # submit 已安排工作，不等完成；result 才等指定工作的结果。
        result_a = future_a.result(timeout=3) # result直到工作完成；join 也只能等线程结束，但没有返回值。Future 记录工作状态和返回值。
        result_b = future_b.result(timeout=3)
    print(f"  线程池返回：{result_a}；{result_b}")
    assert result_a == "A 的结果"
    assert result_b == "B 的结果"
    # 工作函数报错时，result() 会把错误带回调用者，可以在这里 try/except。
    # result(timeout=...) 超时不等于杀掉线程；已经运行的工作可能仍在继续。
    # 离开 with 会等待池里的工作收尾，所以不要把这个 timeout 当成整个代码块的硬时限。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
