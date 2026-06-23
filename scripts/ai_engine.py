import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone, timezone, timezone

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
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        actions.append(result)
        mark_remediated(state, metric)

    save_state(state)
    return actions


def log_incident(anomalies, actions):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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


# ---------- GitHub PR creation (audit trail) ----------

import base64
from dotenv import load_dotenv

load_dotenv()

GITHUB_OWNER = "ARPITDIXIT789"
GITHUB_REPO = "giirs-devops"
GITHUB_API = "https://api.github.com"


def github_headers():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise Exception("GITHUB_TOKEN .env file mein set nahi hai")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def get_main_branch_sha():
    """main branch ka latest commit SHA laata hai — naya branch isi se banega."""
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/main"
    response = requests.get(url, headers=github_headers())
    response.raise_for_status()
    return response.json()["object"]["sha"]


def create_branch(branch_name, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs"
    payload = {"ref": f"refs/heads/{branch_name}", "sha": sha}
    response = requests.post(url, headers=github_headers(), json=payload)
    response.raise_for_status()
    return response.json()


def create_incident_file(branch_name, file_path, content_text, commit_message):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{file_path}"
    encoded = base64.b64encode(content_text.encode()).decode()
    payload = {"message": commit_message, "content": encoded, "branch": branch_name}
    response = requests.put(url, headers=github_headers(), json=payload)
    response.raise_for_status()
    return response.json()


def create_pull_request(branch_name, title, body):
    url = f"{GITHUB_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls"
    payload = {"title": title, "head": branch_name, "base": "main", "body": body}
    response = requests.post(url, headers=github_headers(), json=payload)
    response.raise_for_status()
    return response.json()


def build_incident_markdown(record):
    lines = [f"# Incident report — {record['timestamp']}", "", "## Anomalies detected", ""]
    for a in record["anomalies"]:
        lines.append(f"- **{a['metric']}**: value={a['value']}, reason: {a['reason']}")
    lines.append("")
    lines.append("## Remediation actions taken")
    lines.append("")
    if record["actions"]:
        for act in record["actions"]:
            lines.append(f"- `{act['command']}` — success: {act['success']}")
    else:
        lines.append("- No action taken (cooldown active)")
    lines.append("")
    lines.append("---")
    lines.append("_Auto-generated by GIIRS AI engine._")
    return "\n".join(lines)


def create_incident_pr(record):
    """Incident record se naya branch + file + PR banata hai. Yahi GitOps audit trail hai."""
    if not record["actions"]:
        print("Koi remediation action nahi liya, PR nahi banayenge.")
        return None

    ts = record["timestamp"].replace(":", "-").replace(".", "-")
    branch_name = f"incident-{ts}"
    file_path = f"incidents/{branch_name}.md"
    content = build_incident_markdown(record)

    sha = get_main_branch_sha()
    create_branch(branch_name, sha)
    create_incident_file(branch_name, file_path, content, f"Incident report: {branch_name}")
    pr = create_pull_request(
        branch_name,
        title=f"[GIIRS] Auto-remediation — {record['actions'][0]['metric']}",
        body=content,
    )
    print("PR created:", pr["html_url"])
    return pr["html_url"]

# ---------- Grafana annotation (incident timeline) ----------

GRAFANA_URL = "http://localhost:30286"


def grafana_headers():
    token = os.environ.get("GRAFANA_TOKEN")
    if not token:
        raise Exception("GRAFANA_TOKEN .env file mein set nahi hai")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def create_grafana_annotation(record):
    """Incident ka time-marker Grafana dashboard par daalta hai (graph par vertical line)."""
    metrics = ", ".join(a["metric"] for a in record["anomalies"])
    actions_text = "; ".join(
        f"{act['action']} ({'success' if act['success'] else 'failed'})"
        for act in record["actions"]
    ) or "no action taken"

    text = f"GIIRS incident: {metrics} -> {actions_text}"
    incident_time = datetime.fromisoformat(record["timestamp"].rstrip("Z"))

    payload = {
        "time": int(incident_time.timestamp() * 1000),
        "tags": ["giirs", "incident", "auto-remediation"],
        "text": text,
    }

    url = f"{GRAFANA_URL}/api/annotations"
    response = requests.post(url, headers=grafana_headers(), json=payload)
    response.raise_for_status()
    result = response.json()
    print("Grafana annotation created:", result.get("message", result))
    return result


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

        record = log_incident(found, actions)
        if actions:
            create_incident_pr(record)
            create_grafana_annotation(record)
    else:
        print("Sab normal hai, koi anomaly detect nahi hui.")

