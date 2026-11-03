#!/usr/bin/env python3
"""
12. 阻塞事件循环：为什么 async 里面的 sleep 仍然会卡住别人

运行：python concurrency_basics/12_async_blocking.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：异步程序里不得不调用现有的普通阻塞式库。
本例先故意让 time.sleep 卡住整个事件循环线程，再用 asyncio.to_thread 修复。

观察错误版本中，进度提示要等阻塞操作结束才能开始；修正后进度可以在等待期间打印。
to_thread 是把普通函数交给线程，不是自动把函数内部改成异步代码。
"""

import asyncio
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


async def show_progress():
    for number in range(1, 4):
        print(f"  进度提示 {number}")
        await asyncio.sleep(0.05)


async def bad_async_wrapper():
    # 这是故意展示的错误写法：外层有 async，并不改变 blocking_job 里面的 time.sleep。
    # 当前线程被 sleep 卡住，事件循环也在这个线程上，所以别的协程也得等。
    return blocking_job("阻塞操作")


async def main():
    print("  错误示范：先看到阻塞操作结束，进度提示才能开始。")
    await asyncio.gather(bad_async_wrapper(), show_progress())

    print("  修正：交给线程去等，事件循环可以同时打印进度。")
    results = await asyncio.gather(
        asyncio.to_thread(blocking_job, "线程中的阻塞操作"),
        show_progress(),
    )
    assert results[0] == "线程中的阻塞操作 的结果"
    # to_thread 接函数名和参数，安排到线程中，并让协程可以 await 最终结果。
    # 注意是 to_thread(blocking_job, "...")，不是 to_thread(blocking_job("..."))。
    # 有原生异步接口时通常直接 await 它；已有普通阻塞式库时，to_thread 是一种衔接办法。
    # 它也不是纯 Python CPU 重计算的通用加速器；计算需求请回看进程池。
    # 循环里长时间纯计算且没有合适的让出点，同样会让其他协程一直等。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    asyncio.run(main())
    print("\n本例检查通过。")
