import asyncio


class MetricsReader:
    """Read real application metrics. Missing instruments stay missing, never become zero."""
    def __init__(self, client, spec):
        self.client, self.spec = client, spec

    async def read(self):
        if self.spec.mode == "none":
            return {"available": False, "reason": "서버 지표 미연결"}
        try:
            if self.spec.mode == "json":
                response = await self.client.get(self.spec.path.lstrip("/"), timeout=1)
                response.raise_for_status()
                body = response.json()
                result = {k: body[k] for k in ("pool_active", "pool_max", "pool_pending", "pool_timeouts", "threads_busy", "threads_max", "external_inflight",
                          "provider_attempts", "logical_auth_requests", "cache_errors", "cache_hits", "db_reads", "db_lock_errors",
                          "process_rss_bytes", "heap_used_bytes", "process_threads", "open_fds", "uptime_seconds")
                          if isinstance(body.get(k), (int, float)) and not isinstance(body[k], bool)}
                return {"available": bool(result), **result, "source": "application-json"}
            names = {"pool_active": "hikaricp.connections.active", "pool_max": "hikaricp.connections.max",
                     "pool_pending": "hikaricp.connections.pending", "pool_timeouts": "hikaricp.connections.timeout",
                     "threads_busy": "tomcat.threads.busy", "threads_max": "tomcat.threads.config.max",
                     "process_threads": "jvm.threads.live", "open_fds": "process.files.open", "heap_used_bytes": "jvm.memory.used"}
            async def metric(key, name):
                params = {"tag": f"pool:{self.spec.pool}"} if self.spec.pool and key.startswith("pool_") else None
                if key == "heap_used_bytes":
                    params = {"tag": "area:heap"}
                r = await self.client.get(self.spec.path.rstrip("/").lstrip("/") + "/" + name, params=params, timeout=1)
                if r.status_code != 200:
                    return key, None
                values = r.json().get("measurements", [])
                # Micrometer's timeout counter is cumulative, active/max/pending are gauges.
                value = next((m.get("value") for m in values if m.get("statistic") in {"VALUE", "COUNT"}), None)
                return key, value if isinstance(value, (int, float)) else None
            pairs = await asyncio.gather(*(metric(k, n) for k, n in names.items()))
            result = {k: v for k, v in pairs if v is not None}
            return {"available": bool(result), **result, "source": "spring-actuator"}
        except Exception as error:
            return {"available": False, "reason": type(error).__name__}
