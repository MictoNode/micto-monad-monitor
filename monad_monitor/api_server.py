"""FastAPI backend for monitoring dashboard."""
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cachetools
import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordBearer  # kept for backward compat


def _safe_float(v) -> float | None:
    """Convert to float, return None for NaN/Inf (not JSON-serializable)."""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def verify_password(plain: str, stored: str) -> bool:
    return plain == stored


def create_access_token(secret: str, expires_delta: int = 86400) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=expires_delta)
    payload = {"exp": expire, "iat": now}
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


PROMETHEUS_QUERIES = {
    # Consensus & Execution
    "block_height": 'monad_execution_ledger_block_num{{name="{name}"}}',
    "block_time": '1000 / rate(monad_execution_ledger_block_num{{name="{name}"}}[5m])',
    "commit_rate": 'rate(monad_state_consensus_events_commit_block{{name="{name}"}}[5m])',
    "tx_throughput": 'rate(monad_execution_ledger_num_tx_commits{{name="{name}"}}[5m])',
    "consensus_votes": 'rate(monad_state_consensus_events_created_vote{{name="{name}"}}[5m])',
    "consensus_vote_received": 'rate(monad_state_consensus_events_vote_received{{name="{name}"}}[5m])',
    "consensus_handle_proposal": 'rate(monad_state_consensus_events_handle_proposal{{name="{name}"}}[5m])',
    "consensus_qc": 'rate(monad_state_consensus_events_process_qc{{name="{name}"}}[5m])',
    "consensus_tc": 'rate(monad_state_consensus_events_process_tc{{name="{name}"}}[5m])',
    "tc_ratio": 'rate(monad_state_consensus_events_process_tc{{name="{name}"}}[5m]) / clamp_min(rate(monad_state_consensus_events_process_qc{{name="{name}"}}[5m]) + rate(monad_state_consensus_events_process_tc{{name="{name}"}}[5m]), 0.0001)',
    "consensus_local_timeout": 'rate(monad_state_consensus_events_local_timeout{{name="{name}"}}[5m])',
    "consensus_bad_state_root": 'rate(monad_state_consensus_events_rx_bad_state_root{{name="{name}"}}[5m])',
    "consensus_execution_lagging": 'rate(monad_state_consensus_events_rx_execution_lagging{{name="{name}"}}[5m])',
    "consensus_failed_txn": 'rate(monad_state_consensus_events_failed_txn_validation{{name="{name}"}}[5m])',
    "consensus_out_of_order": 'rate(monad_state_consensus_events_out_of_order_proposals{{name="{name}"}}[5m])',
    "consensus_invalid_leader": 'rate(monad_state_consensus_events_invalid_proposal_round_leader{{name="{name}"}}[5m])',
    "consensus_remote_timeout": 'rate(monad_state_consensus_events_remote_timeout_msg{{name="{name}"}}[5m])',
    "validation_invalid_sig": 'rate(monad_state_validation_errors_invalid_signature{{name="{name}"}}[5m])',
    "validation_insufficient_stake": 'rate(monad_state_validation_errors_insufficient_stake{{name="{name}"}}[5m])',
    "validation_invalid_author": 'rate(monad_state_validation_errors_invalid_author{{name="{name}"}}[5m])',
    "validation_invalid_epoch": 'rate(monad_state_validation_errors_invalid_epoch{{name="{name}"}}[5m])',
    "validation_invalid_seq": 'rate(monad_state_validation_errors_invalid_seq_num{{name="{name}"}}[5m])',
    "validation_not_well_formed": 'rate(monad_state_validation_errors_not_well_formed_sig{{name="{name}"}}[5m])',
    "vote_delay_p50": 'monad_state_vote_delay_ready_after_timer_start_p50_ms{{name="{name}"}}',
    "vote_delay_p90": 'monad_state_vote_delay_ready_after_timer_start_p90_ms{{name="{name}"}}',
    "vote_delay_p99": 'monad_state_vote_delay_ready_after_timer_start_p99_ms{{name="{name}"}}',
    # Peer & Network
    "peers": 'monad_peer_disc_num_peers{{name="{name}"}}',
    "upstream_validators": 'monad_peer_disc_num_upstream_validators{{name="{name}"}}',
    "downstream_fullnodes": 'monad_peer_disc_num_downstream_fullnodes{{name="{name}"}}',
    "pending_peers": 'monad_peer_disc_num_pending_peers{{name="{name}"}}',
    "peer_ping_timeout": 'rate(monad_peer_disc_ping_timeout{{name="{name}"}}[5m])',
    "peer_lookup_timeout": 'rate(monad_peer_disc_lookup_timeout{{name="{name}"}}[5m])',
    "peer_rate_limited": 'rate(monad_peer_disc_rate_limited{{name="{name}"}}[5m])',
    "peer_drop_ping": 'rate(monad_peer_disc_drop_ping{{name="{name}"}}[5m])',
    "peer_retry_lookup": 'rate(monad_peer_disc_retry_lookup_request{{name="{name}"}}[5m])',
    "peer_send_ping": 'rate(monad_peer_disc_send_ping{{name="{name}"}}[5m])',
    "peer_recv_pong": 'rate(monad_peer_disc_recv_pong{{name="{name}"}}[5m])',
    "peer_send_lookup": 'rate(monad_peer_disc_send_lookup_request{{name="{name}"}}[5m])',
    "peer_recv_lookup_response": 'rate(monad_peer_disc_recv_lookup_response{{name="{name}"}}[5m])',
    # Raptorcast
    "raptor_primary_latency": 'monad_bft_raptorcast_udp_primary_broadcast_latency_p99_ms{{name="{name}"}}',
    "raptor_secondary_latency": 'monad_bft_raptorcast_udp_secondary_broadcast_latency_p99_ms{{name="{name}"}}',
    "raptor_authed_rx": 'rate(monad_raptorcast_auth_authenticated_udp_bytes_read{{name="{name}"}}[5m])',
    "raptor_authed_tx": 'rate(monad_raptorcast_auth_authenticated_udp_bytes_written{{name="{name}"}}[5m])',
    "raptor_non_authed_rx": 'rate(monad_raptorcast_auth_non_authenticated_udp_bytes_read{{name="{name}"}}[5m])',
    "raptor_decoded": 'rate(monad_raptorcast_decoding_cache_decoded{{name="{name}"}}[5m])',
    "raptor_cache_hit": 'rate(monad_raptorcast_decoding_cache_decoded_hit{{name="{name}"}}[5m])',
    "raptor_pending_hit": 'rate(monad_raptorcast_decoding_cache_pending_hit{{name="{name}"}}[5m])',
    "raptor_new_entry": 'rate(monad_raptorcast_decoding_cache_new_entry{{name="{name}"}}[5m])',
    "raptor_total_messages": 'rate(monad_raptorcast_total_messages_received{{name="{name}"}}[5m])',
    "raptor_recv_errors": 'rate(monad_raptorcast_total_recv_errors{{name="{name}"}}[5m])',
    "raptor_insertions": 'rate(monad_raptorcast_decoding_cache_p2p_total_insertions{{name="{name}"}}[5m])',
    # Txpool
    "txpool_txs": 'monad_bft_txpool_pool_tracked_txs{{name="{name}"}}',
    "txpool_addresses": 'monad_bft_txpool_pool_tracked_addresses{{name="{name}"}}',
    "txpool_owned_inserts": 'rate(monad_bft_txpool_pool_insert_owned_txs{{name="{name}"}}[5m])',
    "txpool_forwarded_inserts": 'rate(monad_bft_txpool_pool_insert_forwarded_txs{{name="{name}"}}[5m])',
    "txpool_committed_removals": 'rate(monad_bft_txpool_pool_tracked_remove_committed_txs{{name="{name}"}}[5m])',
    "txpool_expired_evictions": 'rate(monad_bft_txpool_pool_tracked_evict_expired_txs{{name="{name}"}}[5m])',
    "txpool_drop_nonce": 'rate(monad_bft_txpool_pool_drop_nonce_too_low{{name="{name}"}}[5m])',
    "txpool_drop_fee": 'rate(monad_bft_txpool_pool_drop_fee_too_low{{name="{name}"}}[5m])',
    "txpool_drop_balance": 'rate(monad_bft_txpool_pool_drop_insufficient_balance{{name="{name}"}}[5m])',
    "txpool_drop_full": 'rate(monad_bft_txpool_pool_drop_pool_full{{name="{name}"}}[5m])',
    "txpool_drop_replaced": 'rate(monad_bft_txpool_pool_drop_replaced_by_higher_priority{{name="{name}"}}[5m])',
    "txpool_drop_existing": 'rate(monad_bft_txpool_pool_drop_existing_higher_priority{{name="{name}"}}[5m])',
    "txpool_drop_sig": 'rate(monad_bft_txpool_pool_drop_invalid_signature{{name="{name}"}}[5m])',
    "txpool_drop_malformed": 'rate(monad_bft_txpool_pool_drop_not_well_formed{{name="{name}"}}[5m])',
    "txpool_drop_not_ready": 'rate(monad_bft_txpool_pool_drop_pool_not_ready{{name="{name}"}}[5m])',
    # RPC
    "rpc_active": 'sum(monad_rpc_active_requests{{name="{name}"}})',
    "rpc_exec_p50": 'histogram_quantile(0.50, sum by(le) (rate(monad_rpc_execution_duration_seconds_bucket{{type="total",name="{name}"}}[5m])))',
    "rpc_exec_p95": 'histogram_quantile(0.95, sum by(le) (rate(monad_rpc_execution_duration_seconds_bucket{{type="total",name="{name}"}}[5m])))',
    "rpc_exec_p99": 'histogram_quantile(0.99, sum by(le) (rate(monad_rpc_execution_duration_seconds_bucket{{type="total",name="{name}"}}[5m])))',
    "rpc_call_rate": 'sum by(main) (rate(monad_rpc_execution_duration_seconds_count{{type="total",name="{name}"}}[5m]))',
    "rpc_wait_p50": 'histogram_quantile(0.50, sum by(le) (rate(monad_rpc_execution_duration_seconds_bucket{{type="wait",name="{name}"}}[5m])))',
    "rpc_wait_p95": 'histogram_quantile(0.95, sum by(le) (rate(monad_rpc_execution_duration_seconds_bucket{{type="wait",name="{name}"}}[5m])))',
    "rpc_wait_p99": 'histogram_quantile(0.99, sum by(le) (rate(monad_rpc_execution_duration_seconds_bucket{{type="wait",name="{name}"}}[5m])))',
    "rpc_methods_latency": 'histogram_quantile(0.99, sum by(le,main) (rate(monad_rpc_execution_duration_seconds_bucket{{type="total",name="{name}"}}[5m])))',
    # Host — CPU
    "cpu_user": 'sum by(mode) (rate(node_cpu_seconds_total{{mode="user",name="{name}"}}[5m])) / on() group_left count(count by(cpu)(node_cpu_seconds_total{{name="{name}"}}))',
    "cpu_system": 'sum by(mode) (rate(node_cpu_seconds_total{{mode="system",name="{name}"}}[5m])) / on() group_left count(count by(cpu)(node_cpu_seconds_total{{name="{name}"}}))',
    "cpu_iowait": 'sum by(mode) (rate(node_cpu_seconds_total{{mode="iowait",name="{name}"}}[5m])) / on() group_left count(count by(cpu)(node_cpu_seconds_total{{name="{name}"}}))',
    "cpu_softirq": 'sum by(mode) (rate(node_cpu_seconds_total{{mode="softirq",name="{name}"}}[5m])) / on() group_left count(count by(cpu)(node_cpu_seconds_total{{name="{name}"}}))',
    "cpu_nice": 'sum by(mode) (rate(node_cpu_seconds_total{{mode="nice",name="{name}"}}[5m])) / on() group_left count(count by(cpu)(node_cpu_seconds_total{{name="{name}"}}))',
    # Host — Memory
    "memory_used": 'node_memory_MemTotal_bytes{{name="{name}"}} - node_memory_MemAvailable_bytes{{name="{name}"}}',
    "memory_cache": 'node_memory_Cached_bytes{{name="{name}"}} + node_memory_Buffers_bytes{{name="{name}"}}',
    "memory_free": 'node_memory_MemFree_bytes{{name="{name}"}}',
    "memory_usage": '(1 - node_memory_MemAvailable_bytes{{name="{name}"}} / node_memory_MemTotal_bytes{{name="{name}"}}) * 100',
    # Host — Disk
    "disk_read": 'rate(node_disk_read_bytes_total{{device!~"dm-.*|loop.*",name="{name}"}}[5m])',
    "disk_write": 'rate(node_disk_written_bytes_total{{device!~"dm-.*|loop.*",name="{name}"}}[5m])',
    "disk_usage": '(1 - node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay|squashfs|devtmpfs",name="{name}"}} / node_filesystem_size_bytes{{fstype!~"tmpfs|overlay|squashfs|devtmpfs",name="{name}"}}) * 100',
    # Host — Network (filters: loopback, Docker, bridges, K8s CNI)
    "net_recv": 'rate(node_network_receive_bytes_total{{device!~"lo|veth.*|docker.*|br-.*|cali.*|cilium.*|flannel.*",name="{name}"}}[5m])',
    "net_sent": 'rate(node_network_transmit_bytes_total{{device!~"lo|veth.*|docker.*|br-.*|cali.*|cilium.*|flannel.*",name="{name}"}}[5m])',
    # Host — Load (normalized per CPU core)
    "load1": 'node_load1{{name="{name}"}} / scalar(count(node_cpu_seconds_total{{mode="idle",name="{name}"}}))',
    "load5": 'node_load5{{name="{name}"}} / scalar(count(node_cpu_seconds_total{{mode="idle",name="{name}"}}))',
    "load15": 'node_load15{{name="{name}"}} / scalar(count(node_cpu_seconds_total{{mode="idle",name="{name}"}}))',
    # Host — NVMe
    "nvme_temp": 'nvme_temperature_celsius{{name="{name}"}}',
    "nvme_wear": 'nvme_percentage_used_ratio{{name="{name}"}}',
    # TrieDB
    "triedb_fast": 'monad_triedb_fast_used_bytes{{name="{name}"}}',
    "triedb_slow": 'monad_triedb_slow_used_bytes{{name="{name}"}}',
    "triedb_free": 'monad_triedb_free_capacity_bytes{{name="{name}"}}',
}

OVERVIEW_QUERIES = {
    "node_status": 'min(up{{name="{name}"}})',
    "block_height": 'monad_execution_ledger_block_num{{name="{name}"}}',
    "peers": 'monad_peer_disc_num_peers{{name="{name}"}}',
    "syncing": 'monad_statesync_syncing{{name="{name}"}}',
    "uptime_us": 'monad_total_uptime_us{{name="{name}"}}',
    "upstream_validators": 'monad_peer_disc_num_upstream_validators{{name="{name}"}}',
    "self_stake_bps": 'monad_state_node_state_self_stake_bps{{name="{name}"}}',
    "proposals": 'monad_bft_txpool_create_proposal{{name="{name}"}}',
    "commits": 'monad_state_consensus_events_commit_block{{name="{name}"}}',
    "local_timeouts": 'monad_state_consensus_events_local_timeout{{name="{name}"}}',
    "rpc_requests": 'sum(monad_rpc_active_requests{{name="{name}"}})',
}


_RANGE_CONFIG: dict[str, tuple[int, str]] = {
    "1m": (60, "2s"),
    "5m": (300, "5s"),
    "30m": (1800, "15s"),
    "1h": (3600, "30s"),
    "all": (2592000, "300s"),
}


def _resolve_range(range_param: str) -> tuple[int, str]:
    """Return (start_seconds_ago, step) for a given range string. Defaults to 1h."""
    return _RANGE_CONFIG.get(range_param, _RANGE_CONFIG["1h"])


class PrometheusClient:
    """Async client for Prometheus HTTP API with caching."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cache: cachetools.TTLCache = cachetools.TTLCache(maxsize=256, ttl=10)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def query(self, promql: str) -> list[dict]:
        client = await self._get_client()
        cache_key = f"q:{promql}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            resp = await client.get(
                f"{self.base_url}/api/v1/query",
                params={"query": promql},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, Exception):
            return []

        if data.get("status") != "success":
            return []

        results = []
        for r in data.get("data", {}).get("result", []):
            raw = r["value"][1] if r.get("value") else None
            results.append({
                "labels": r.get("metric", {}),
                "value": _safe_float(raw),
            })
        self.cache[cache_key] = results
        return results

    async def query_range(self, promql: str, range_param: str = "1h") -> list[dict]:
        start_seconds, step = _resolve_range(range_param)
        client = await self._get_client()
        now_ts = time.time()
        start_ts = str(now_ts - start_seconds)
        end_ts = str(now_ts)
        cache_key = f"r:{promql}:{start_ts}:{step}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            resp = await client.get(
                f"{self.base_url}/api/v1/query_range",
                params={"query": promql, "start": start_ts, "end": end_ts, "step": step},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, Exception):
            return []

        if data.get("status") != "success":
            return []

        results = []
        for r in data.get("data", {}).get("result", []):
            values = []
            for ts, v in r.get("values", []):
                fv = _safe_float(v)
                if fv is not None:
                    values.append((float(ts), fv))
            results.append({
                "labels": r.get("metric", {}),
                "values": values,
            })
        self.cache[cache_key] = results
        return results

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.close()


def create_app(password: str, jwt_secret: str, prometheus_url: str, validators_config: list[dict]) -> FastAPI:
    app = FastAPI(title="Monad Monitor Dashboard API", docs_url=None, redoc_url=None)
    prom = PrometheusClient(prometheus_url)
    def _get_current_user(request: Request) -> dict:
        token = request.cookies.get("jwt")
        if not token:
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
        if not token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            decode_token(token, jwt_secret)
            return {"authenticated": True}
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    @app.post("/api/auth/login")
    async def login(request: Request, response: Response):
        body = await request.json()
        pw = body.get("password", "")
        if not verify_password(pw, password):
            raise HTTPException(status_code=401, detail="Invalid password")
        token = create_access_token(secret=jwt_secret)
        response.set_cookie(
            "jwt", token,
            httponly=True,
            samesite="strict",
            max_age=86400,
            path="/",
        )
        return {"access_token": token, "token_type": "bearer"}

    @app.post("/api/auth/logout")
    async def api_logout(response: Response):
        response.delete_cookie("jwt", path="/")
        return {"status": "ok"}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/validators")
    async def get_validators(user: dict = Depends(_get_current_user)):
        result = []
        for v in validators_config:
            result.append({
                "name": v.get("name", ""),
                "network": v.get("network", "testnet"),
                "host": v.get("host", ""),
            })
        return result

    @app.get("/api/overview")
    async def get_overview(user: dict = Depends(_get_current_user)):
        result = {}
        for v in validators_config:
            name = v.get("name", "")
            metrics = {}
            for key, query_tpl in OVERVIEW_QUERIES.items():
                query = query_tpl.format(name=name)
                results = await prom.query(query)
                if results:
                    metrics[key] = results[0]["value"]
                else:
                    metrics[key] = None
            result[name] = metrics
        return result

    @app.get("/api/metrics/{validator_name}")
    async def get_metrics(validator_name: str, user: dict = Depends(_get_current_user)):
        metrics = {}
        for key, query_tpl in PROMETHEUS_QUERIES.items():
            query = query_tpl.format(name=validator_name)
            results = await prom.query(query)
            if results:
                if len(results) == 1:
                    metrics[key] = results[0]["value"]
                else:
                    metrics[key] = [{"labels": r["labels"], "value": r["value"]} for r in results]
            else:
                metrics[key] = None
        return metrics

    @app.get("/api/chart/{validator_name}/{metric_key}")
    async def get_chart(validator_name: str, metric_key: str, range: str = "1h", user: dict = Depends(_get_current_user)):
        query_tpl = PROMETHEUS_QUERIES.get(metric_key)
        if not query_tpl:
            raise HTTPException(status_code=404, detail=f"Unknown metric: {metric_key}")
        query = query_tpl.format(name=validator_name)
        results = await prom.query_range(query, range_param=range)
        return {"metric": metric_key, "validator": validator_name, "series": results}

    @app.get("/")
    async def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/dashboard/")

    static_dir = Path(__file__).parent / "static_dashboard"
    if static_dir.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/dashboard", StaticFiles(directory=str(static_dir), html=True), name="dashboard")

    return app


class APIServer:
    """Run FastAPI in a background daemon thread."""

    def __init__(self, prometheus_url: str, password: str, jwt_secret: str, validators_config: list[dict], port: int = 8383):
        self.prometheus_url = prometheus_url
        self.password = password
        self.jwt_secret = jwt_secret
        self.validators_config = validators_config
        self.port = port
        self._thread: threading.Thread | None = None
        self._loop = None

    def start(self):
        import asyncio
        import uvicorn

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            app = create_app(
                password=self.password,
                jwt_secret=self.jwt_secret,
                prometheus_url=self.prometheus_url,
                validators_config=self.validators_config,
            )
            config = uvicorn.Config(app, host="0.0.0.0", port=self.port, log_level="warning")
            server = uvicorn.Server(config)
            self._loop.run_until_complete(server.serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
