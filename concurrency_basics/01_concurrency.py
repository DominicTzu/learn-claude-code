#!/usr/bin/env python3
"""
01. 顺序、并发、并行：先理解它们在描述什么

运行：python concurrency_basics/01_concurrency.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

本文件先运行最普通的顺序代码，后面的文件再改变安排方式。

顺序：A 完全结束，才开始 B。
并发：A、B 的工作时间段重叠，可以交替推进，不要求同一瞬间都在计算。
并行：同一时刻，A、B 真的在不同 CPU 核等计算资源上执行。
例如：A 等网络时切去处理 B，是并发；两个 CPU 核同时各算一份数据，是并行。

进程、线程、协程则是在回答“用什么方式组织这些工作”：
进程是运行中的一份程序；线程是进程内的一条执行路线；协程能暂停并稍后继续。
同一线程上的多个协程可以并发，但不会让这个线程同时执行两段 Python 代码。

阻塞：当前线程必须在这里等，不能执行自己的下一行。下面的 time.sleep 就是例子。
同步代码通常要等函数返回后才往下走；async/await 则允许协程在等结果时把机会让出去。
但 async 不等于不等结果，也不等于自动开线程。

使用场景：任务很少、很快，或者 B 必须依赖 A 的结果时，先写顺序代码最清楚。
本例预期顺序：A 开始 -> A 结束 -> B 开始 -> B 结束。
两次短 sleep 模拟等待，不是真的联网。时间只是观察值，不是严格性能测试。
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


def main():
    # 调用 blocking_job("A") 后，必须等它 return，才会给 result_a 赋值。
    # 之后才执行 B。所以顺序一定是：A 开始 -> A 结束 -> B 开始 -> B 结束。
    started_at = time.perf_counter() # 记录开始时间
    result_a = blocking_job("A")
    result_b = blocking_job("B")
    elapsed = time.perf_counter() - started_at
    print(f"  得到：{result_a}；{result_b}；总耗时约 {elapsed:.2f} 秒")
    assert result_a == "A 的结果"
    assert result_b == "B 的结果"
    # 预期约等两次 WAIT_SECONDS。工作很少、不耗时、有严格先后依赖时，这样最容易理解。
    # 并发不是必需品，不应该为了“高级”把所有普通函数都改成线程或 async。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
