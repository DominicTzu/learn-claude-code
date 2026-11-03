#!/usr/bin/env python3
"""
11. 协程并发：先安排两个 Task，再等待结果

运行：python concurrency_basics/11_async_tasks.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：几项互不依赖的异步操作，可以让等待时间重叠。
create_task 把协程安排为可运行任务；gather 用来一起安排并收集多个结果。
Task 是协程的运行记录，不是新线程。

本例先创建 A、B 两个 Task，再 await 它们；与先 await A 再创建 B 不同。
观察所有工作仍在同一线程上，但 A 等待时 B 可以开始。
"""

import asyncio
import threading


WAIT_SECONDS = 0.15


async def async_job(name, delay=WAIT_SECONDS):
    print(f"  {name} 开始，线程名={threading.current_thread().name}")
    # 这里模拟一种“能够把等待交给 asyncio 管理”的操作。
    # await 的意思是：我要它的结果；结果还没好时，当前协程可以暂停，让其他任务运行。
    # 暂停的是当前协程，不是把整个线程停在 sleep 里。
    await asyncio.sleep(delay)
    print(f"  {name} 结束")
    return f"{name} 的结果"


async def main():
    # create_task 把协程交给事件循环安排，还返回一个 Task，便于之后等结果或取消。
    # Task 是运行记录，不是新线程。本文件没有自定义调度方式，任务在控制权交回循环后运行。
    task_a = asyncio.create_task(async_job("A"))
    task_b = asyncio.create_task(async_job("B"))
    print("  A、B 都已安排，下面才开始等结果。")
    result_a = await task_a
    result_b = await task_b
    # 虽然 await 写了两行，但 B 在 await A 之前已经安排了，因此能在 A 等待时运行。
    assert result_a == "A 的结果"
    assert result_b == "B 的结果"

    # gather 是“把这些工作一起安排，并等所有结果”的常见写法。
    # B2 的等待时间更短，会先结束；但返回的 results 按传入顺序排列，不按完成顺序。
    results = await asyncio.gather(async_job("A2", 0.1), async_job("B2", 0.05))
    print(f"  gather 返回：{results}")
    assert results == ["A2 的结果", "B2 的结果"]
    # “先 await A 再创建 B”和“先创建两个 Task，再 await 它们”不同，就差在安排的时间。
    # 不要只 create_task 后就退出而不管结果；保留引用，等待它，并处理异常和取消。
    # gather 中某项出错时，默认会抛出该错误，不代表其他任务已被自动取消或回滚。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    asyncio.run(main())
    print("\n本例检查通过。")
