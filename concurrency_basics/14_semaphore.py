#!/usr/bin/env python3
"""
14. Semaphore：最多允许几项工作同时进行

运行：python concurrency_basics/14_semaphore.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：限制同时下载数、数据库连接占用数，避免瞬间把资源用光。
Lock 通常用于一次只让一个进入；Semaphore 可以允许最多 N 个进入。

本例安排五项工作，但同时进入执行区的最多两项；结束一项就归还一个名额。
这限制的是“同时有多少个”，不是“每秒最多请求多少次”。
"""

import asyncio


async def limited_job(name, slots, counts):
    # Lock 常用于“只能一个人改共享状态”；Semaphore 常用于“最多同时发几个请求”。
    async with slots:
        counts["running"] += 1
        counts["peak"] = max(counts["peak"], counts["running"])
        print(f"  {name} 进入，目前同时进行 {counts['running']} 项")
        try:
            await asyncio.sleep(0.05)
            return name
        finally:
            counts["running"] -= 1
            # 走出 async with，自动归还名额；异常或取消时也要走完这些清理步骤。


async def main():
    slots = asyncio.Semaphore(2)
    counts = {"running": 0, "peak": 0}
    jobs = []
    for number in range(1, 6):
        jobs.append(limited_job(f"任务{number}", slots, counts))
    results = await asyncio.gather(*jobs)
    # *jobs 把列表展开成 gather(任务1, 任务2, ...)，这里总共安排 5 个，但最多 2 个进入。
    print(f"  完成 {len(results)} 项，实际同时工作的峰值为 {counts['peak']}")
    assert counts["peak"] == 2
    assert counts["running"] == 0
    assert len(results) == 5
    # 适用场景：限制数据库连接、同时下载数，避免瞬间把机器或服务压满。
    # “最多同时 2 个”不是“每秒最多 2 次”：并发数量限制与请求速率限制是不同问题。
    # 若有百万个任务，也不宜全建成 Task 后只靠 Semaphore 等待，应考虑分批或队列。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    asyncio.run(main())
    print("\n本例检查通过。")
