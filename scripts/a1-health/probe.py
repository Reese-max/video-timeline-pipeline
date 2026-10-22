"""Post video MCP health to OCI Monitoring (namespace video_mcp_health, metric mcp_health, 1=ok 0=down).

check=local  -> http://127.0.0.1:8787/health
check=public -> <MCP_PUBLIC_BASE_URL of video-mcp.service>/health through the public entry (Funnel relay),
                resolved via public DNS because MagicDNS here maps the name to our own tailnet IP
Auth: instance principal (dynamic group a1-health-metrics, policy limited to this namespace).
Alarms in OCI fire on value 0 and on absence (A1 down or this timer dead).
"""
import datetime
import ipaddress
import json
import subprocess
import sys
import urllib.parse
import urllib.request

import oci

COMPARTMENT = "ocid1.tenancy.oc1..aaaaaaaanvps7hnqnqzmygn3dwa4uns5ptmyi2ze5w6x4c3h5u5fcwynwuaq"
NAMESPACE = "video_mcp_health"
ENDPOINT = "https://telemetry-ingestion.ap-tokyo-1.oraclecloud.com"


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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
