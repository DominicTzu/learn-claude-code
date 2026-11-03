#!/usr/bin/env python3
"""
04. Queue：把结果一条条交给另一个线程

运行：python concurrency_basics/04_queue.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：一个线程产生结果，另一个线程按顺序接收。
与其一起修改复杂的大字典，可以通过 Queue 的 put/get 交接。
Queue 自己处理好线程之间的协调，不必在每次 put/get 外面再包一把锁。

本例由工作线程放入两条结果，主线程逐条取出。
约定 None 表示“没有下一条了”；这不是 Queue 自动规定的结束符。
这是 queue.Queue，供同一进程的线程使用；跨进程要考虑 multiprocessing.Queue。
"""

import queue
import threading


def produce_messages(mailbox):
    mailbox.put("第一条结果")
    mailbox.put("第二条结果")
    # 约定 None 不是业务结果，而是“后面没有新结果了”。这是我们自己的约定。
    mailbox.put(None)


def main():
    mailbox = queue.Queue()
    producer = threading.Thread(target=produce_messages, args=(mailbox,))
    producer.start()
    received = []
    try:
        while True:
            # 没数据时 get 会等待；超过 3 秒还没有，则抛 queue.Empty。
            # 不需要不断用 while 判断“现在有了吗”。
            message = mailbox.get(timeout=3)
            if message is None:
                break
            received.append(message)
            print(f"  主线程收到：{message}")
    finally:
        producer.join()
    assert received == ["第一条结果", "第二条结果"]
    # get 会取走这一条，下次 get 读取下一条，而不是反复读同一个结果。
    # Queue 保护的是入队/出队；放进去的可变对象不要再由两边同时随意修改。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
