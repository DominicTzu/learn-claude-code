#!/usr/bin/env python3
"""
15. 协程超时与取消：提出停止请求，并等待它完成清理

运行：python concurrency_basics/15_async_cancellation.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：不能无限等待某项操作，或用户已经不再需要它的结果。
wait_for 设置等待时限；task.cancel 主动请求取消；finally 负责离开时清理。

本例先演示等待超时，再演示主动取消。两个长任务都提前结束，不会等满十秒。
取消不是自动撤销已经发生的文件写入或网络操作，也不等于强行杀掉后台线程。
"""

import asyncio


async def long_async_job(started, cleaned):
    started.set()
    try:
        await asyncio.sleep(10)  # 本例会提前取消，不会真的等完这 10 秒。
    finally:
        print("  长任务：准备离开，执行 finally 清理。")
        cleaned.set()
        # 真实程序这里可以释放连接等。已经完成的外部写入不会因此自动撤销。


async def main():
    started = asyncio.Event()
    cleaned = asyncio.Event()
    task = asyncio.create_task(long_async_job(started, cleaned))
    await started.wait()  # 确认已进入函数，避免演示成“还没开始就取消”。
    try:
        await asyncio.wait_for(task, timeout=0.05)
    except asyncio.TimeoutError:
        print("  调用者：等太久了，wait_for 已请求取消这项任务。")
    assert task.cancelled()
    assert cleaned.is_set()

    # 也可以不等超时，主动提出取消请求。
    started = asyncio.Event()
    cleaned = asyncio.Event()
    task = asyncio.create_task(long_async_job(started, cleaned))
    await started.wait()
    task.cancel()
    try:
        await task  # cancel 不是“我说完就算处理完”；继续等待它收到请求并收尾。
    except asyncio.CancelledError:
        print("  调用者：已确认任务取消并完成清理。")
    assert task.cancelled()
    assert cleaned.is_set()

    # 取消一般会在协程有机会恢复时，以 CancelledError 的形式通知它。
    # 不要吞掉这个异常后装作成功；通常用 finally 清理，或捕获后继续 raise。
    # wait_for 会等待取消处理，因此实际耗时可能超过给定 timeout，并非硬实时闹钟。
    # 若协程用 time.sleep/死循环堵住线程，事件循环连超时处理都没机会及时执行。
    # 取消一个 await to_thread(...) 的任务，也不代表底层线程或外部命令被杀掉。
    # 线程要协作停止可用05_event.py 中的 Event；子进程需要单独管理。不要把几种取消混为一谈。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    asyncio.run(main())
    print("\n本例检查通过。")
