#!/usr/bin/env python3
"""
06. 进程：另开一个 Python，普通字典不会自动共享

运行：python concurrency_basics/06_processes.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：需要隔开内存或让计算有机会使用多个 CPU 核。
进程比线程更重，启动和传数据都有成本，并不是任务越小越应该开进程。

本例子进程把字典的 value 改为 99，父进程等它结束后仍读到 0。
原因是 spawn 启动的新进程收到的是另一份数据，而不是同一份普通字典。
重点观察不同的进程号，以及文件末尾的 __main__ 判断。
"""

import multiprocessing
import os


def change_data_in_child(data):
    # 这个函数要写在文件顶层，不能藏在 demo 函数里面，便于新进程找到它。
    print(f"  子进程号={os.getpid()}，改之前 data={data}", flush=True)
    data["value"] = 99
    print(f"  子进程里的 data 已变成 {data}", flush=True)


def main():
    data = {"value": 0}
    print(f"  父进程号={os.getpid()}，父进程最初 data={data}", flush=True)

    # spawn 表示启动一个新的 Python 解释器。这里明确指定，避免依赖系统默认方式。
    # 它会重新导入本文件，再执行指定函数，因此文件末尾的 __main__ 判断非常重要。如果没有这个判断，导入时就会启动新进程，可能无限递归。
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=change_data_in_child, args=(data,))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        # 仅防止这个演示在异常环境里一直挂着，不建议靠强制终止处理普通业务收尾。
        child.terminate()
        child.join()
        raise RuntimeError("子进程超过 10 秒没有结束")
    exit_code = child.exitcode
    child.close()

    assert exit_code == 0, f"子进程异常退出：{exit_code}"
    print(f"  子进程结束后，父进程的 data 仍然是 {data}")
    assert data["value"] == 0
    # 这次传过去的是另一份数据。子进程改自己的内存，不会直接改父进程的普通字典。
    # 要传回结果，需用 multiprocessing.Queue/Pipe 等通信方式，或08_process_pool.py 的进程池。
    # 进程池会替你传参数和结果，但大对象的传输本身也需要时间和内存。
    #
    # 进程之间也能共享专门的内存对象；那时要考虑共同使用的 multiprocessing.Lock。
    # 不能每个进程各建一个无关的锁，就认为它们会互相等待。
    # threading.Lock 只用于本进程的线程协调，不保护其他进程对同一文件的操作。

# spawn 子进程会重新导入本文件。没有下面的判断，就可能导入时又启动新进程。
# 工作函数放在顶层，让新解释器能找到它；不要依赖父进程中的局部函数或临时全局修改。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
