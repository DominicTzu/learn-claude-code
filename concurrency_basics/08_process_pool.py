#!/usr/bin/env python3
"""
08. 进程池：把 Python 计算交给多个进程

运行：python concurrency_basics/08_process_pool.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：大量纯 Python 计算，需要利用多个 CPU 核，且任务足够大。
ProcessPoolExecutor 管理子进程，也替你传参数、取返回值。

本例计算两份平方和，结果里带着实际执行计算的子进程号。
任务故意很小，只用于看逻辑，不用于证明多进程一定更快。
参数和结果需要能够在进程之间传输；并不是任意连接对象或局部函数都能直接交过去。
"""

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor


def calculate_squares(limit):
    # 这次不是等网络，而是实际进行计算。参数故意设得很小，避免演示占用很久。
    total = 0
    for number in range(limit):
        total += number * number
    return {"pid": os.getpid(), "total": total}


def main():
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        future_a = pool.submit(calculate_squares, 50_000)
        future_b = pool.submit(calculate_squares, 60_000)
        result_a = future_a.result(timeout=10)
        result_b = future_b.result(timeout=10)
    print(f"  父进程号={os.getpid()}")
    print(f"  子进程交回结果：{result_a}、{result_b}")
    assert result_a["total"] == calculate_squares(50_000)["total"]
    assert result_b["total"] == calculate_squares(60_000)["total"]
    assert result_a["pid"] != os.getpid()
    assert result_b["pid"] != os.getpid()
    # 具体用哪个工作进程由池安排，不保证两个小任务一定分别交给两个不同进程。
    # 启动进程和传数据也要花时间，小计算可能反而更慢，大对象也可能让传输成为瓶颈。
    # result 超时不会自动终止正在运行的计算，退出 with 仍要等待工作收尾。

# spawn 会导入本文件，所以只在直接运行时启动进程池，不能在导入时就启动。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
