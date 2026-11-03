#!/usr/bin/env python3
"""
13. 协程锁：同一个线程，也可能在 await 前后丢掉修改

运行：python concurrency_basics/13_async_lock.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：多个协程需要读取并更新同一份状态，而且有关联的操作中间可能 await。
不加锁时两个协程都可能先读到旧值，再分别写回同一个新值。

asyncio.Lock 让拿不到锁的协程暂停，事件循环还能运行持锁的协程。
它与会阻塞线程的 threading.Lock 不同，也不能替代线程之间或进程之间的锁。
本例先稳定得到错误结果 1，再加锁得到 2。
"""

import asyncio


async def async_add_without_lock(counter):
    old_value = counter["value"]
    # sleep(0) 是有意让当前任务先暂停，让其他已安排的任务也读到旧值。
    # 真实业务中，“读完 -> 等数据库/网络 -> 写回”也可能出现类似情况。
    await asyncio.sleep(0)
    counter["value"] = old_value + 1


async def async_add_with_lock(counter, lock):
    # 拿不到 asyncio.Lock 时，暂停的是本协程，事件循环还能运行持锁的那个协程。
    async with lock:
        old_value = counter["value"]
        await asyncio.sleep(0)  # 仅为了展示：即使这里暂停，另一位也不能进这个代码段。
        counter["value"] = old_value + 1


async def main():
    counter = {"value": 0}
    await asyncio.gather(async_add_without_lock(counter), async_add_without_lock(counter))
    print(f"  不加锁，两个协程都读到 0，最后得到 {counter['value']}")
    assert counter["value"] == 1

    counter = {"value": 0}
    lock = asyncio.Lock()
    await asyncio.gather(async_add_with_lock(counter, lock), async_add_with_lock(counter, lock))
    print(f"  加 asyncio.Lock 后得到 {counter['value']}")
    assert counter["value"] == 2
    # 不要在这些协程里换用 threading.Lock 来等待：它会阻塞整个事件循环所在的线程。
    # 尤其是拿着 threading.Lock 跨过 await，另一协程来拿锁时可能把同一线程堵死，
    # 原协程也就没机会恢复并释放锁。这里不会实际运行这种死锁。
    # asyncio.Lock 只协调同一事件循环里的任务，不能替代跨线程/跨进程的锁。
    # 同一事件循环内，一小段不含让出点的纯计算通常不会被另一个协程插入；
    # 不应因此推出“整个 async 函数永远不会被插入”，因为里面可能有 await。
    # await 也不是每次都必然切换任务；等待对象若已经就绪，可以直接继续。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    asyncio.run(main())
    print("\n本例检查通过。")
