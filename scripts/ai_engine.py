import requests

PROMETHEUS_URL = "http://localhost:30580"


def query_prometheus(promql):
    """Prometheus HTTP API ko PromQL query bhejta hai aur result return karta hai."""
    url = f"{PROMETHEUS_URL}/api/v1/query"
    response = requests.get(url, params={"query": promql}, timeout=10)
    response.raise_for_status()
    data = response.json()
    if data["status"] != "success":
        raise Exception(f"Prometheus query failed: {data}")
    return data["data"]["result"]


def get_request_rate(job="giirs-service", window="1m"):
    """Per-second request rate (kitni requests aa rahi hain)."""
    query = f'sum(rate(http_requests_total{{job="{job}"}}[{window}]))'
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


def get_error_rate(job="giirs-service", window="1m"):
    """5xx errors ka fraction (0.0 = no errors, 1.0 = sab errors)."""
    query = (
        f'sum(rate(http_requests_total{{job="{job}",status=~"5.."}}[{window}])) / '
        f'sum(rate(http_requests_total{{job="{job}"}}[{window}]))'
    )
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


def get_p95_latency(job="giirs-service", window="1m"):
    """95th percentile response time (seconds mein)."""
    query = (
        f'histogram_quantile(0.95, sum(rate('
        f'http_request_duration_seconds_bucket{{job="{job}"}}[{window}])) by (le))'
    )
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


def get_pod_restarts(namespace="default"):
    """Total pod restarts (crash-loop ka signal)."""
    query = f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}})'
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


if __name__ == "__main__":
    print("Request rate (req/sec):", get_request_rate())
    print("Error rate (fraction):", get_error_rate())
    print("P95 latency (seconds):", get_p95_latency())
    print("Pod restarts (total):", get_pod_restarts())
