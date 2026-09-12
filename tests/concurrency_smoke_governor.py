#!/usr/bin/env python3
"""并发压测:AgentCacheGovernor.sweep_pressure 在缓存 churn 下不崩(冒烟)。

复现 Manny7717 的条件:2000 条缓存 + churn 线程(并发 insert/evict) +
governor 反复跑 sweep_pressure。旧代码会抛
RuntimeError: dictionary changed size during iteration(异常被吞→压力阀静默失效);
修复后应稳定跑完,且只释放"非活跃 + 已持久化"的转录。
"""
import sys
import threading
import time

sys.path.insert(0, "/root/hermes-webui")

from api.agent_cache_governance import AgentCacheGovernor, transcript_persistence_caught_up


class FakeAgent:
    """模拟 AIAgent:_session_messages + _last_flushed_db_idx + _db_flush_scan_prefix。"""

    def __init__(self, session_id, flushed=False, activity=0.0):
        self.session_id = session_id
        self._session_messages = [{"role": "user", "content": "x" * 100} for _ in range(10)]
        self._last_flushed_db_idx = len(self._session_messages) if flushed else 3
        self._db_flush_scan_prefix = list(self._session_messages[:3])
        self._last_activity_ts = activity

    def __repr__(self):
        return f"<FakeAgent {self.session_id} flushed={self._last_flushed_db_idx == len(self._session_messages)}>"


def main():
    n = 2000
    cache = {}
    for i in range(n):
        cache[f"s{i}"] = (FakeAgent(f"s{i}", flushed=True), object())
    lock = threading.Lock()

    gov = AgentCacheGovernor(
        cache, lock,
        idle_ttl_secs=0,          # 只测压力路径
        memory_high_mb=1,         # 强制超预算(实际会读 RSS,但传 rss_mb 覆盖)
        protect_recent=0,
    )

    stop = threading.Event()
    stats = {"churn": 0, "evicted": 0}

    def churn():
        i = 0
        while not stop.is_set():
            with lock:
                if len(cache) > n:
                    # 随机逐出
                    key = next(iter(cache))
                    cache.pop(key, None)
                cache[f"churn{i % 500}"] = (FakeAgent(f"churn{i % 500}", flushed=True), object())
                i += 1
                stats["churn"] += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()

    errors = []
    start = time.time()
    passes = 300
    for p in range(passes):
        try:
            dropped = gov.sweep_pressure(rss_mb=2000)  # 强制超预算
            stats["evicted"] += dropped
        except RuntimeError as e:
            errors.append(f"pass {p}: RuntimeError: {e}")
        except Exception as e:
            errors.append(f"pass {p}: {type(e).__name__}: {e}")

    stop.set()
    t.join(timeout=5)
    elapsed = time.time() - start

    print(f"passes={passes} churn_ops={stats['churn']} dropped_total={stats['evicted']} elapsed={elapsed:.1f}s")
    if errors:
        print(f"FAIL: {len(errors)} errors, first: {errors[0]}")
        sys.exit(1)

    # 校验:被释放的必须是 flushed 的 agent;活跃 agent 不应被释放
    unflushed_released = 0
    with lock:
        for key, entry in cache.items():
            agent = entry[0] if isinstance(entry, tuple) and entry else entry
            if agent is not None and hasattr(agent, "_session_messages"):
                if agent._session_messages == [] and agent._last_flushed_db_idx != len([1] * 10) and agent._last_flushed_db_idx < 10:
                    # 释放后 messages=[];若释放时未 flushed(flushed idx<10)就是 bug
                    unflushed_released += 1
    # 更简单的校验:释放的 agent 的 _session_messages 应为空,且 persistence 当时 caught up(通过记录)
    # 这里直接检查:没有任何 agent 同时满足"messages 被清 且 原本未 flushed"
    # 由于我们无法追溯释放瞬间,改为断言:所有仍在缓存中的 agent,若 messages=[] 则其 flush idx 必须 == 10(flushed)
    bad = 0
    with lock:
        for key, entry in cache.items():
            agent = entry[0] if isinstance(entry, tuple) and entry else entry
            if agent is not None and getattr(agent, "_session_messages", None) == []:
                if not transcript_persistence_caught_up(agent):
                    bad += 1
    if bad:
        print(f"FAIL: {bad} released agents were NOT persisted at release time")
        sys.exit(1)

    print("OK: 300 sweeps under churn, no RuntimeError; all released agents were persisted")
    sys.exit(0)


if __name__ == "__main__":
    main()
