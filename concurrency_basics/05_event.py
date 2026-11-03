#!/usr/bin/env python3
"""
05. Event：告诉线程“可以停了”，而不是强行杀掉它

运行：python concurrency_basics/05_event.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：主线程通知工作线程停止，或者通知它“准备工作完成了”。
Event 只有“已设置/未设置”这样的信号状态，不装业务结果，和 Queue 用途不同。

本例的工作线程隔一会儿检查停止信号。主线程调用 set 后，工作线程自己退出。
set 不会强行打断正在执行的普通函数；工作线程必须愿意检查这个信号。
"""

import threading
import time


def wait_until_stopped(stop_event):
    print("  后台线程：开始检查停止信号。")
    # wait(0.05)：最多等 0.05 秒；收到 set 返回 True，超时还没收到则返回 False。
    # 这样既能隔一会儿做点工作，又不用 while True 空转浪费 CPU。
    while not stop_event.wait(0.05):
        pass  # 实际程序可以在这里处理一小段工作，再回来检查信号。
    print("  后台线程：看到了停止信号，正常退出函数。")


def main():
    stop_event = threading.Event()
    worker = threading.Thread(target=wait_until_stopped, args=(stop_event,))
    worker.start()
    try: # try-finally语法，逻辑是：不管 try 里是正常结束还是抛异常，finally 都会执行。
        time.sleep(0.1)
    finally:
        # 正常结束或等待期间抛异常，都先请求停止，再等线程自己收尾。
        stop_event.set()
        worker.join()
    assert not worker.is_alive()
    # 若线程卡在长期不返回的函数里，不会自动跑来检查 Event，join 也可能一直等待。
    # 不能把“发出了停止请求”和“线程已经停止”当作同一件事。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
