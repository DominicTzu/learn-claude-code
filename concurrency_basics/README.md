# Python 并发基础

从一个大文件拆成按概念排列的小文件。每个文件都包含自己的标准库导入、辅助函数、通俗注释和运行入口，**不依赖其他示例文件，也不依赖 Agent 代码**。

## 怎么学

建议按编号阅读，一次只打开一个文件。先猜打印顺序，再运行，最后对照注释。
先学 01–09 的普通函数、线程和进程，再学 10–15 的协程。

在项目根目录运行，例如：

```sh
python concurrency_basics/03_thread_lock.py
python concurrency_basics/10_coroutines.py
```

也可以在编辑器里直接运行当前文件，或进入本目录再运行文件名。不再需要原大文件的编号参数或 `--list`。

## 阅读目录

| 文件 | 本次主要解释 | 观察重点 |
| --- | --- | --- |
| [01_concurrency.py](01_concurrency.py) | 顺序、并发、并行、阻塞 | A 结束才开始 B |
| [02_threads.py](02_threads.py) | Thread、start、join | 同一进程，不同线程；共享对象 |
| [03_thread_lock.py](03_thread_lock.py) | 线程锁 | 两次修改为什么只留下了一次 |
| [04_queue.py](04_queue.py) | Queue | 结果如何从一个线程交给另一个 |
| [05_event.py](05_event.py) | Event | 发出停止信号不等于强行杀死线程 |
| [06_processes.py](06_processes.py) | Process、spawn | 子进程改字典，父进程为何没变 |
| [07_thread_pool.py](07_thread_pool.py) | 线程池、Future | 先安排工作，再拿结果 |
| [08_process_pool.py](08_process_pool.py) | 进程池 | 用子进程计算并传回结果 |
| [09_subprocess.py](09_subprocess.py) | 外部命令 | 启动命令仍可能让调用者等待 |
| [10_coroutines.py](10_coroutines.py) | async、await、事件循环 | 协程对象不等于已经执行 |
| [11_async_tasks.py](11_async_tasks.py) | Task、gather | 先安排两个任务和连续 await 的差别 |
| [12_async_blocking.py](12_async_blocking.py) | to_thread | 普通阻塞函数为何卡住其他协程 |
| [13_async_lock.py](13_async_lock.py) | asyncio.Lock | 协程在 await 前后也可能丢失修改 |
| [14_semaphore.py](14_semaphore.py) | Semaphore | 五项工作最多同时进行两项 |
| [15_async_cancellation.py](15_async_cancellation.py) | 超时、取消、finally | 请求取消后，仍要等待清理 |

## 使用边界

- Python 3.10+，只用标准库；不读取密钥、不联网、不修改业务文件。
- 短暂 `sleep` 只是模拟等待，不是真正下载。打印顺序可能交错，耗时不是性能测试。
- `assert` 用于核对示例结果，不是生产环境的安全校验；运行时不要用 `-O` 跳过它们。
- 错误示范只演示可控的丢失修改或短暂阻塞；不会真的运行永不结束的死锁。
- 线程和子进程会等待收尾；协程示例也会等待结果或确认取消。

## 实际项目怎么选

先问“慢在等别人，还是慢在自己计算”，不要一开始就混用所有机制。

- 少量、简单或必须前后依赖的步骤：先顺序执行。
- 普通库里大量等待网络、磁盘等 I/O：考虑线程池。
- 使用本来就支持 await 的库，有很多独立等待：考虑协程，并限制同时进行的数量。
- 大量纯 Python 计算，需要多个 CPU 核：考虑进程池，同时计算传数据和启动进程的成本。
- 调用 git、ffmpeg 等现成命令：使用 subprocess，明确输出、超时与退出处理。
- 共享可变状态：优先考虑各自管理数据或用队列交接；需要锁时选择对应线程、协程或进程的机制。

不论用哪一种，都要想清楚：谁领取结果、谁处理错误、怎样超时、退出时谁收尾。

## 参考

内容按普通 CPython 3.10 的行为解释并验证，尤其不要把 GIL 的说明推广到所有 Python 实现或配置。

- [线程](https://docs.python.org/3.10/library/threading.html)
- [进程](https://docs.python.org/3.10/library/multiprocessing.html)
- [线程池与进程池](https://docs.python.org/3.10/library/concurrent.futures.html)
- [协程与 Task](https://docs.python.org/3.10/library/asyncio-task.html)
- [协程同步工具](https://docs.python.org/3.10/library/asyncio-sync.html)
- [队列](https://docs.python.org/3.10/library/queue.html)
- [子进程](https://docs.python.org/3.10/library/subprocess.html)
