from __future__ import annotations

import logging

import redis

logger = logging.getLogger("p4gitsync.redis_monitor")


def get_redis_metrics(r: redis.Redis, stream_key: str, group_name: str) -> dict:
    """Redis Stream 모니터링 지표를 수집하여 반환."""
    try:
        memory_info = r.info("memory")
        memory_used_mb = round(memory_info.get("used_memory", 0) / (1024 * 1024), 2)
    except Exception:
        memory_used_mb = -1

    try:
        stream_info = r.xinfo_stream(stream_key)
        stream_length = stream_info.get("length", 0)
    except redis.ResponseError:
        stream_length = 0

    try:
        groups = r.xinfo_groups(stream_key)
    except redis.ResponseError:
        groups = []

    consumer_lag = 0
    pending_messages = 0

    for group in groups:
        if group.get("name") == group_name:
            pending_messages = group.get("pending", 0)
            last_delivered = group.get("last-delivered-id", "0-0")
            consumer_lag = _calculate_lag(r, stream_key, last_delivered)
            break

    return {
        "memory_used_mb": memory_used_mb,
        "stream_length": stream_length,
        "consumer_lag": consumer_lag,
        "pending_messages": pending_messages,
        "group_count": len(groups),
    }


def _calculate_lag(r: redis.Redis, stream_key: str, last_delivered_id: str) -> int:
    """last-delivered-id 이후의 미전달 메시지 수를 계산."""
    try:
        if last_delivered_id == "0-0":
            info = r.xinfo_stream(stream_key)
            return info.get("length", 0)

        remaining = r.xrange(stream_key, min=f"({last_delivered_id}", max="+")
        return len(remaining)
    except Exception:
        return -1
