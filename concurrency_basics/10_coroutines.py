#!/usr/bin/env python3
"""
10. 协程：async def、await 和事件循环

运行：python concurrency_basics/10_coroutines.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

先记住：协程不是新线程，而是一段可以“记住执行位置，暂停后接着往下走”的代码。
async def 定义协程函数；调用它先得到协程对象，还没有开始执行函数体。
本文件用三个小例子，把下面三个问题具体演示出来。

一、到底在等什么？
    等某个操作完成，拿到它的返回值；也可能等到异常，而不是成功结果。
    例如向服务器查询订单：请求已发出，但服务器还在查询数据库，回复还没到。
    此时客户端不是在不停计算，而是在等网络数据到达。
    又例如 await asyncio.sleep(0.3)：等计时器到期，默认返回值是 None。
    其他场景还有：异步队列里还没有消息，或需要等待另一个 Task 完成。

二、为什么要暂停？暂停的具体是什么？
    假设下一步是“显示订单状态”，但查询结果还没有回来，这一步就暂时做不了。
    当前协程停在 await 这里，保留局部变量和执行位置，不执行它后面的语句。
    它把控制权交回事件循环，让同一线程有机会处理别的、现在就能做的工作。
    条件满足后，事件循环再安排它继续；它从 await 后面接着走，不是从函数开头重来。
    注意：await 不代表“每次都一定暂停”。操作可以立即完成时，也可以直接继续。

三、“其他已安排的工作”是谁？
    不是文件里所有函数，也不是 await 后面那行代码，而是明确交给事件循环的任务。
    例如 asyncio.create_task(show_progress())，就安排了一个独立的“刷新提示”任务。
    事件循环可以理解成调度员：让能继续的任务运行，暂时放下还在等待的任务。
    如果没有其他任务能运行，它就等待计时器或输入输出事件，不会凭空创造工作。

使用场景：有很多等待网络回复、消息等的任务，而且使用的库支持异步等待。
不适合直接拿来加速大量计算；也不能把普通阻塞调用加上 await 就变成异步。
下面只用计时器模拟网络延迟，没有真的联网，也没有在后台偷偷创建工作线程。

参考：https://docs.python.org/3.10/library/asyncio-task.html
"""

import asyncio # 协程在一个线程里轮流执行，事件循环负责调度；它在一个线程中模拟“同时”运行多个线程并发。
import threading


WAIT_SECONDS = 0.15


async def async_job(name, delay=WAIT_SECONDS):
    print(f"  {name} 开始，线程名={threading.current_thread().name}")
    # sleep 会登记一个计时器。这里具体等的是“delay 秒已过去”，而不是某次计算。
    # 时间没到时，本函数停在这一行。name 和 delay 仍保留着，不需要重新传入。
    # 时间到了，本函数才有资格继续；实际何时继续还取决于事件循环的调度。
    await asyncio.sleep(delay)
    print(f"  {name} 结束")
    return f"{name} 的结果"


async def example_1_wait_in_order():
    print("\n例 1：没有安排其他任务，先等 A，再等 B。")
    # async_job 是协程函数；coroutine_a 是一次具体调用产生的协程对象。
    coroutine_a = async_job("A")
    print("  刚创建 A 的协程对象；现在还没打印“A 开始”。")

    # 这里有两层等待，别把它们混在一起：
    # 1. 本函数 await coroutine_a，最终要的是 async_job 返回的“A 的结果”。
    # 2. async_job 内部 await sleep，等的是计时器；sleep 完成后才会执行 return。
    # 直接 await 协程不会另开线程，也不会自动给它创建一个独立 Task。
    # A 等计时器时，调用它的本函数也不能向下走，因为 result_a 还没有拿到。
    result_a = await coroutine_a

    # A 完成以后才到这里，所以 B 到现在才被创建并执行。
    # 不能理解成“A 暂停了，所以自动执行下一行 B”；下一行仍然属于同一条执行流程。
    result_b = await async_job("B")
    print(f"  连续两次 await 的结果：{result_a}；{result_b}")
    assert result_a == "A 的结果"
    assert result_b == "B 的结果"


async def query_order():
    order_id = "ORDER-001"
    print(f"  查询 {order_id}：模拟请求已发出，回复尚未到达。")

    # 假设真实业务是：请求发给服务器后，服务器还需要 0.3 秒准备回复。
    # 客户端此时没有结果可处理，因此可以把这段等待时间让给别的任务。
    # 为了不依赖网络，本例实际等待的是计时器；订单状态也是下面写死的模拟数据。
    # 真正访问网络时，应调用异步客户端提供的请求方法，而不是 sleep 后猜一个结果。
    await asyncio.sleep(0.3)

    # 暂停期间 order_id 没有丢失；恢复后直接走到这里，不会重新打印“请求已发出”。
    print(f"  查询 {order_id}：模拟回复到达，继续处理结果。")
    return "已发货"


async def show_progress():
    # 这是一个具体的“其他工作”：输出提示。它不依赖订单查询结果，所以可以先做。
    # 整个例子仍只有一个线程，打印线程名是为了让你直接看到这一点。
    for number in range(1, 4):
        print(f"  提示任务：刷新第 {number} 次，线程名={threading.current_thread().name}")
        # 提示任务也会暂停：这里只是让两次刷新之间隔 0.05 秒，避免连续刷屏。
        # 它和查询任务都在等计时器时，事件循环也可以等待，不必一直空转。
        await asyncio.sleep(0.05)


async def example_2_do_other_work_while_waiting():
    print("\n例 2：查询结果还没回来时，另一个任务可以刷新提示。")

    # 关键是这一行：把 show_progress 的协程对象包装成 Task，交给事件循环安排。
    # Task 可以理解成“交给调度员管理的一份任务”，记录协程的运行状态和最终结果。
    # 如果只写 show_progress()，仅仅得到协程对象，并没有安排它独立运行。
    # 在本脚本的默认调度方式下，这里先登记任务，当前流程继续执行。
    progress_task = asyncio.create_task(show_progress())
    print("  已安排提示任务；现在去查询订单。")

    # 具体顺序：
    # 本函数进入 query_order -> 打印“请求已发出” -> 碰到未完成的 sleep -> 暂停。
    # 控制权回到事件循环后，刚才安排的提示任务就有机会执行它的 print。
    # 查询和提示各自等待、各自恢复，仍然是同一线程轮流执行，并非同时执行两行 Python。
    order_status = await query_order()

    # 本行依赖查询结果，必须等 query_order 的 return；不能趁它等待时提前执行。
    print(f"  主流程：现在拿到订单状态，才可以显示：{order_status}")
    assert order_status == "已发货"

    # 明确等提示任务结束，不让它遗留到下一个例子，也能接收到它运行中抛出的异常。
    # 它可能已经完成了；如果已完成，这次 await 就直接取结果，不需要再暂停等待。
    await progress_task


async def read_local_cache():
    # “缓存”指已经存在本机内存里的数据。这里直接返回固定值来模拟它。
    # 没有等网络、计时器，也没有内部 await，因此本次调用可以一口气执行到 return。
    return "缓存中已有的订单状态"


async def example_3_result_is_ready():
    print("\n例 3：虽然写了 await，但这次不需要暂停。")
    progress_task = asyncio.create_task(show_progress())

    # 这里只是为了演示语义，才把立即返回的函数写成 async def。
    # 普通的内存取值没有必要为了“异步”而特意包装成协程。
    cached_status = await read_local_cache()
    print(f"  已拿到：{cached_status}。注意：本例的提示任务还没有打印。")
    assert cached_status == "缓存中已有的订单状态"

    # 上面的 await 没有真正暂停，所以已安排的提示任务还没轮到执行。
    # 下面等待尚未完成的 Task，才把控制权交回事件循环，提示任务才开始输出。
    await progress_task


async def main():
    await example_1_wait_in_order()
    await example_2_do_other_work_while_waiting()
    await example_3_result_is_ready()

    # 最后区分两个名字相似、行为却不同的函数：
    # await asyncio.sleep(1)：当前任务暂停，事件循环可以运行其他任务。
    # time.sleep(1)：整个当前线程停 1 秒，同一线程上的事件循环也没法调度其他任务。
    # await time.sleep(1) 更不行：先阻塞 1 秒，再因返回的 None 不能 await 而报错。
    # 很长的普通计算循环也不会被 asyncio 自动打断；async def 并不是自动并发开关。


if __name__ == "__main__":
    # run 创建事件循环，并把 main 作为一个 Task 运行，最后关闭事件循环。
    # 本文件直接 await 的那些嵌套协程沿着 main 这条流程执行；不是每个函数一个 Task。
    # 只有显式 create_task 的提示任务，才在本例中成为另一条可独立调度的流程。
    # 已有事件循环的 Notebook 等环境通常直接 await，不能再嵌套调用 run。
    asyncio.run(main())
    print("\n本例检查通过。")
