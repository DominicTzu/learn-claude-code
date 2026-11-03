#!/usr/bin/env python3
"""
03. 线程锁：为什么加了两次，结果却只有 1

运行：python concurrency_basics/03_thread_lock.py
只用标准库，不需要联网或密钥；也不需要先运行或导入其他示例文件。

适用场景：多个线程需要读取并修改同一个状态，例如计数器或后台任务记录。
锁要保护整组有关联的操作，不是只给最后一次赋值套一层外壳。

先安排两个线程都读到 0，再分别写回 1，稳定重现“丢失一次修改”。
随后用同一把 Lock，让第二个线程等第一个读、改、写完，最终得到 2。
故障用 Barrier 安排，不靠 sleep 碰运气；死锁只解释，不实际运行。

threading.Lock 只协调来获取同一把锁的线程，不会自动锁住字典或磁盘文件。
本例先失败再修复是有意设计；两段各自的预期结果都会用 assert 检查。
"""

import threading


def add_one_without_lock(counter, meet_here):
    old_value = counter["value"]
    # 为了每次都能看见问题，特意让两个线程读完旧值后在这里集合。
    # Barrier(2) 的意思是“等两个线程都走到 wait，再允许它们继续”。
    # 所以两边的 old_value 必定都是 0，而不是碰运气等待线程切换。
    # 真实业务不需要这条集合指令；它这里只是安排一个容易看懂的错误发生顺序。
    meet_here.wait(timeout=5)
    counter["value"] = old_value + 1


def add_one_with_lock(counter, lock):
    # 走到 with 时尝试拿锁；别人拿着，就等它放回。走出缩进范围时自动释放。
    # 两个线程用同一个 lock，所以第二个人必须等第一人“读完、加完、写回”才开始。
    with lock:
        old_value = counter["value"]
        new_value = old_value + 1
        counter["value"] = new_value


def main():
    counter = {"value": 0}
    meet_here = threading.Barrier(2) # 让两个线程在这里集合，保证它们都读到旧值 0。 Barrier(2) 的意思是“等两个线程都走到 wait，再允许它们继续”。
    first = threading.Thread(target=add_one_without_lock, args=(counter, meet_here))
    second = threading.Thread(target=add_one_without_lock, args=(counter, meet_here))
    first.start()
    second.start()
    first.join()
    second.join()
    print(f"  没加锁：本想加两次得到 2，实际却得到 {counter['value']}")
    assert counter["value"] == 1
    # 过程是：甲读 0，乙读 0，甲写 1，乙也写 1；乙覆盖了甲那次修改。
    # 这种结果受执行交错顺序影响的问题，叫“竞态条件”。

    counter = {"value": 0}
    lock = threading.Lock()
    first = threading.Thread(target=add_one_with_lock, args=(counter, lock))
    second = threading.Thread(target=add_one_with_lock, args=(counter, lock))
    first.start()
    second.start()
    first.join()
    second.join()
    print(f"  用同一把锁：第一次读 0 写 1，第二次读 1 写 2，得到 {counter['value']}")
    assert counter["value"] == 2

    # 要锁住整组“读取 -> 修改 -> 写回”，只在最后一行加锁，仍可能都读到旧值。
    # 锁不会自动附着到字典上：所有访问者都必须遵守“先拿同一把锁”的约定。
    # 锁内这段代码通常叫“临界区”。它不能被其他同样拿锁的代码插入，不表示出错会回滚。
    #
    # 不要把上面的 Barrier 搬进 with lock：第一个线程会拿着锁等第二个来集合，
    # 第二个却在等第一个释放锁，双方都无法前进，这叫“死锁”。本文件不实际运行死锁。
    # 普通 Lock 也不能让同一线程重复获取而不先释放；确有嵌套需求时有 RLock，
    # 但 RLock 不能替你解决多个线程以相反顺序获取多把锁的问题。
    # 不要拿着锁做漫长下载，其他需要锁的线程也会被迫陪着等待。

if __name__ == "__main__":
    # 直接运行本文件才执行示例；导入时只定义函数，不自动启动工作。
    main()
    print("\n本例检查通过。")
