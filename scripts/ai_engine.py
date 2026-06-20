import json
import os
import statistics
import subprocess
import time
from datetime import datetime

import requests

PROMETHEUS_URL = "http://localhost:30580"
STATE_FILE = "state.json"
INCIDENT_LOG_FILE = "incidents_log.json"

DEPLOYMENT_NAME = "giirs-app"
NAMESPACE = "default"
MAX_REPLICAS = 4
COOLDOWN_SECONDS = 300  # same anomaly type ke liye 5 min wait karo dobara action lene se pehle

# Anomaly detection settings
ZSCORE_THRESHOLD = 2.5
ERROR_RATE_HARD_LIMIT = 0.05
LATENCY_HARD_LIMIT_SEC = 1.0


# ---------- Prometheus queries ----------

def query_prometheus(promql):
    url = f"{PROMETHEUS_URL}/api/v1/query"
    response = requests.get(url, params={"query": promql}, timeout=10)
    response.raise_for_status()
    data = response.json()
    if data["status"] != "success":
        raise Exception(f"Prometheus query failed: {data}")
    return data["data"]["result"]


def query_prometheus_range(promql, minutes=10, step="15s"):
    end = time.time()
    start = end - (minutes * 60)
    url = f"{PROMETHEUS_URL}/api/v1/query_range"
    params = {"query": promql, "start": start, "end": end, "step": step}
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data["status"] != "success":
        raise Exception(f"Prometheus range query failed: {data}")
    result = data["data"]["result"]
    if not result:
        return []
    return [float(v[1]) for v in result[0]["values"]]


def get_request_rate(job="giirs-service", window="1m"):
    query = f'sum(rate(http_requests_total{{job="{job}"}}[{window}]))'
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


def get_error_rate(job="giirs-service", window="1m"):
    query = (
        f'sum(rate(http_requests_total{{job="{job}",status=~"5.."}}[{window}])) / '
        f'sum(rate(http_requests_total{{job="{job}"}}[{window}]))'
    )
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


def get_p95_latency(job="giirs-service", window="1m"):
    query = (
        f'histogram_quantile(0.95, sum(rate('
        f'http_request_duration_seconds_bucket{{job="{job}"}}[{window}])) by (le))'
    )
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


def get_pod_restarts(namespace="default"):
    query = f'sum(kube_pod_container_status_restarts_total{{namespace="{namespace}"}})'
    result = query_prometheus(query)
    return float(result[0]["value"][1]) if result else 0.0


# ---------- State (runs ke beech yaad rakhne ke liye) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_restart_count": 0.0, "last_remediation": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def is_in_cooldown(state, metric):
    last = state.get("last_remediation", {}).get(metric)
    if last is None:
        return False
    return (time.time() - last) < COOLDOWN_SECONDS


def mark_remediated(state, metric):
    state.setdefault("last_remediation", {})[metric] = time.time()


# ---------- Anomaly detection ----------

def zscore_anomaly(history, current, threshold=ZSCORE_THRESHOLD):
    if len(history) < 5:
        return False, 0.0
    mean = statistics.mean(history)
    std = statistics.pstdev(history)
    if std == 0:
        return False, 0.0
    z = (current - mean) / std
    return abs(z) > threshold, z


def detect_anomalies():
    anomalies = []

    error_rate = get_error_rate()
    history = query_prometheus_range(
        'sum(rate(http_requests_total{job="giirs-service",status=~"5.."}[1m])) / '
        'sum(rate(http_requests_total{job="giirs-service"}[1m]))'
    )
    is_z, z = zscore_anomaly(history, error_rate)
    if is_z or error_rate > ERROR_RATE_HARD_LIMIT:
        anomalies.append({
            "metric": "error_rate",
            "value": round(error_rate, 4),
            "zscore": round(z, 2),
            "reason": "5xx error rate normal se zyada ya hard limit cross",
        })

    latency = get_p95_latency()
    history = query_prometheus_range(
        'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{job="giirs-service"}[1m])) by (le))'
    )
    is_z, z = zscore_anomaly(history, latency)
    if is_z or latency > LATENCY_HARD_LIMIT_SEC:
        anomalies.append({
            "metric": "p95_latency",
            "value": round(latency, 4),
            "zscore": round(z, 2),
            "reason": "Response time normal se kaafi zyada",
        })

    state = load_state()
    restarts = get_pod_restarts()
    if restarts > state.get("last_restart_count", 0.0):
        anomalies.append({
            "metric": "pod_restarts",
            "value": restarts,
            "zscore": None,
            "reason": "Naya pod crash/restart detect hua",
        })
    state["last_restart_count"] = restarts
    save_state(state)

    return anomalies


# ---------- Remediation ----------

def run_kubectl(args):
    return subprocess.run(["kubectl"] + args, capture_output=True, text=True)


def get_current_replicas():
    result = run_kubectl(
        ["get", "deployment", DEPLOYMENT_NAME, "-n", NAMESPACE, "-o", "jsonpath={.spec.replicas}"]
    )
    return int(result.stdout.strip())


def remediate_rollout_restart():
    result = run_kubectl(["rollout", "restart", f"deployment/{DEPLOYMENT_NAME}", "-n", NAMESPACE])
    return {
        "action": "rollout_restart",
        "command": f"kubectl rollout restart deployment/{DEPLOYMENT_NAME}",
        "success": result.returncode == 0,
        "output": (result.stdout or result.stderr).strip(),
    }


def remediate_scale_up():
    current = get_current_replicas()
    if current >= MAX_REPLICAS:
        action = remediate_rollout_restart()
        action["note"] = "Max replicas pe already hai, rollout restart kiya instead"
        return action

    new_replicas = current + 1
    result = run_kubectl(
        ["scale", f"deployment/{DEPLOYMENT_NAME}", f"--replicas={new_replicas}", "-n", NAMESPACE]
    )
    return {
        "action": "scale_up",
        "command": f"kubectl scale deployment/{DEPLOYMENT_NAME} --replicas={new_replicas}",
        "success": result.returncode == 0,
        "output": (result.stdout or result.stderr).strip(),
        "from_replicas": current,
        "to_replicas": new_replicas,
    }


def remediate_anomalies(anomalies, state):
    actions = []
    for a in anomalies:
        metric = a["metric"]
        if is_in_cooldown(state, metric):
            print(f"Cooldown active for {metric}, skip kar rahe hain.")
            continue

        if metric == "pod_restarts":
            result = remediate_rollout_restart()
        elif metric in ("error_rate", "p95_latency"):
            result = remediate_scale_up()
        else:
            continue

        result["metric"] = metric
        result["anomaly"] = a
        result["timestamp"] = datetime.now(datetime.UTC).isoformat() + "Z"
        actions.append(result)
        mark_remediated(state, metric)

    save_state(state)
    return actions


def log_incident(anomalies, actions):
    record = {
        "timestamp": datetime.now(datetime.UTC).isoformat() + "Z",
        "anomalies": anomalies,
        "actions": actions,
    }
    log = []
    if os.path.exists(INCIDENT_LOG_FILE):
        with open(INCIDENT_LOG_FILE) as f:
            log = json.load(f)
    log.append(record)
    with open(INCIDENT_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)
    return record


if __name__ == "__main__":
    print("--- Current metrics ---")
    print("Request rate (req/sec):", get_request_rate())
    print("Error rate (fraction):", get_error_rate())
    print("P95 latency (seconds):", get_p95_latency())
    print("Pod restarts (total):", get_pod_restarts())

    print("\n--- Anomaly check ---")
    found = detect_anomalies()
    if found:
        for a in found:
            print("ANOMALY:", a)

        state = load_state()
        actions = remediate_anomalies(found, state)

        print("\n--- Remediation actions ---")
        if actions:
            for act in actions:
                print("ACTION:", act)
        else:
            print("Koi action nahi liya (cooldown active tha).")

        log_incident(found, actions)
    else:
        print("Sab normal hai, koi anomaly detect nahi hui.")
