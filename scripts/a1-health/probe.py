"""Post video MCP health to OCI Monitoring (namespace video_mcp_health, metric mcp_health, 1=ok 0=down).

check=local  -> http://127.0.0.1:8787/health
check=public -> <MCP_PUBLIC_BASE_URL of video-mcp.service>/health through the public entry (Funnel relay),
                resolved via public DNS because MagicDNS here maps the name to our own tailnet IP
Auth: instance principal (dynamic group a1-health-metrics, policy limited to this namespace).
Alarms in OCI fire on value 0 and on absence (A1 down or this timer dead).

Metric deploy_health (1=ok 0=needs a human, alarm a1-deploy-stuck) watches the auto-deploy instead,
posted separately after mcp_health so it can never cost those datapoints: the last run FAILED or
ROLLED_BACK, the service is draining while no deploy run holds the deploy lock, or the running commit
has been behind GitHub main for more than 18 hours (e.g. main CI keeps failing, which the auto-deploy
only records as DEFERRED).
"""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
import urllib.request

import oci

COMPARTMENT = "ocid1.tenancy.oc1..aaaaaaaanvps7hnqnqzmygn3dwa4uns5ptmyi2ze5w6x4c3h5u5fcwynwuaq"
NAMESPACE = "video_mcp_health"
ENDPOINT = "https://telemetry-ingestion.ap-tokyo-1.oraclecloud.com"
AUTODEPLOY_STATUS = Path("/var/lib/video-mcp-autodeploy/last.json")
AUTODEPLOY_LOCK = Path("/run/video-mcp-autodeploy.lock")
PROC_LOCKS = Path("/proc/locks")
MAIN_SHA_API = "https://api.github.com/repos/Reese-max/video-timeline-pipeline/commits/main"
BEHIND_STATE = Path("/var/lib/video-mcp-health/behind.json")  # the unit's StateDirectory
# Normal worst case: merge just after a 6-hourly run, next run drains 3h and defers, the one after deploys.
DEPLOY_LAG_LIMIT = datetime.timedelta(hours=18)


def fetch_health(url):
    """Return the /health JSON when it reports status ok, else None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "video-mcp-health/1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.load(resp)
            return body if resp.status == 200 and body.get("status") == "ok" else None
    except Exception as exc:
        print(f"{url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def public_ips(host):
    """IPv4 addresses of host from public DNS (the system resolver would return the tailnet IP)."""
    for server in ("8.8.8.8", "1.1.1.1"):
        proc = subprocess.run(
            ["dig", "+short", "+time=5", "+tries=1", f"@{server}", host, "A"],
            capture_output=True, text=True, check=False,
        )
        ips = []
        # On timeout dig exits non-zero and prints ";; communications error to 8.8.8.8#53" to stdout.
        for token in proc.stdout.split() if proc.returncode == 0 else []:
            try:
                ips.append(str(ipaddress.IPv4Address(token)))
            except ValueError:
                pass  # CNAME targets
        if ips:
            return ips
    print(f"{host}: no public A record", file=sys.stderr)
    return []


def public_health(base):
    """True when base/health is ok through any public IP of its host, as an outside client sees it."""
    host = urllib.parse.urlsplit(base).hostname
    for ip in public_ips(host):
        proc = subprocess.run(
            ["curl", "-fsS", "-m", "20", "-A", "video-mcp-health/1",
             "--resolve", f"{host}:443:{ip}", f"{base}/health"],
            capture_output=True, text=True, check=False,
        )
        try:
            if json.loads(proc.stdout).get("status") == "ok":
                return True
        except (ValueError, AttributeError):
            pass
        print(f"{base}/health via {ip}: rc={proc.returncode} {proc.stderr.strip()[:200]}", file=sys.stderr)
    return False


def unit_public_base_url():
    # Fallback when the local service is down; may be unreadable under DynamicUser.
    env = subprocess.run(
        ["systemctl", "show", "video-mcp", "-p", "Environment", "--value"],
        capture_output=True, text=True, check=False,
    ).stdout
    for item in env.split():
        if item.startswith("MCP_PUBLIC_BASE_URL="):
            return item.split("=", 1)[1].rstrip("/")
    return None


def autodeploy_running():
    """True while any auto-deploy run (timer or manual) holds its flock. Read from /proc/locks rather
    than probing the lock, which could make a run starting at that instant skip itself."""
    try:
        st = AUTODEPLOY_LOCK.stat()
    except FileNotFoundError:
        return False
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    return any(key in line.split() for line in PROC_LOCKS.read_text().splitlines())


def main_sha():
    """SHA of GitHub main (one small unauthenticated API call)."""
    req = urllib.request.Request(MAIN_SHA_API, headers={
        "Accept": "application/vnd.github.sha", "User-Agent": "video-mcp-health/1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        sha = resp.read(100).decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"unexpected main SHA {sha[:60]!r}")
    return sha


def behind_since(running, main, now):
    """When the running commit was first seen behind main (None when up to date). Remembered across
    runs so that new merges keep the clock running while nothing gets deployed."""
    if running == main:
        BEHIND_STATE.unlink(missing_ok=True)
        return None
    try:
        state = json.loads(BEHIND_STATE.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("running") == running:
            return datetime.datetime.fromisoformat(state["since"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    BEHIND_STATE.write_text(json.dumps({"running": running, "since": now.isoformat()}), encoding="utf-8")
    return now


def deploy_problems(local, now):
    """Why the auto-deploy needs a human ([] = fine). A check that cannot run this round is skipped."""
    problems = []
    try:
        last = json.loads(AUTODEPLOY_STATUS.read_text(encoding="utf-8"))
        result = last.get("result") if isinstance(last, dict) else None
        if result in ("FAILED", "ROLLED_BACK"):
            problems.append(f"last auto-deploy {result} at {last.get('time')}: {str(last.get('error', ''))[:150]}")
    except (OSError, ValueError) as exc:
        print(f"{AUTODEPLOY_STATUS}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if local is None:
        return problems  # service down: check=local already alarms
    try:
        if local.get("deployment_draining") and not autodeploy_running():
            problems.append("service is draining but no auto-deploy run holds the deploy lock")
    except OSError as exc:
        print(f"deploy-lock check: {type(exc).__name__}: {exc}", file=sys.stderr)
    running = str(local.get("commit_sha") or "")
    try:
        since = behind_since(running, main_sha(), now)
        if since is not None and now - since > DEPLOY_LAG_LIMIT:
            problems.append(f"running {running[:7]} has been behind GitHub main since {since:%Y-%m-%d %H:%M}Z")
    except Exception as exc:  # GitHub unreachable or rate-limited: the lag is judged again next round
        print(f"behind-main check: {type(exc).__name__}: {exc}", file=sys.stderr)
    return problems


def main():
    local = fetch_health("http://127.0.0.1:8787/health")
    base = ((local or {}).get("public_base_url") or unit_public_base_url() or "").rstrip("/")
    results = {
        "local": local is not None,
        "public": bool(base) and public_health(base),
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    metric_data = [
        oci.monitoring.models.MetricDataDetails(
            namespace=NAMESPACE,
            compartment_id=COMPARTMENT,
            name="mcp_health",
            dimensions={"check": check, "host": "oracle-agent-hub"},
            datapoints=[oci.monitoring.models.Datapoint(timestamp=now, value=1.0 if ok else 0.0)],
        )
        for check, ok in results.items()
    ]
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.monitoring.MonitoringClient(config={}, signer=signer, service_endpoint=ENDPOINT)
    resp = client.post_metric_data(oci.monitoring.models.PostMetricDataDetails(metric_data=metric_data))
    failed = resp.data.failed_metrics_count
    print(f"posted local={int(results['local'])} public={int(results['public'])} base={base} failed={failed}")

    # Service health is posted; the deploy check can no longer delay or lose it.
    try:
        problems = deploy_problems(local, now)
    except Exception as exc:
        print(f"deploy check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("posted deploy=unknown")
        return 1 if failed else 0
    for problem in problems:
        print(f"deploy: {problem}", file=sys.stderr)
    resp = client.post_metric_data(oci.monitoring.models.PostMetricDataDetails(metric_data=[
        oci.monitoring.models.MetricDataDetails(
            namespace=NAMESPACE,
            compartment_id=COMPARTMENT,
            name="deploy_health",
            dimensions={"host": "oracle-agent-hub"},
            datapoints=[oci.monitoring.models.Datapoint(timestamp=now, value=0.0 if problems else 1.0)],
        )]))
    deploy_failed = resp.data.failed_metrics_count
    print(f"posted deploy={int(not problems)} failed={deploy_failed}")
    return 1 if failed or deploy_failed else 0


if __name__ == "__main__":
    sys.exit(main())
