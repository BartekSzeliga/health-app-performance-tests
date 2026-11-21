#!/usr/bin/env python3
"""CloudWatch metrics of the AWS dev environment for the steps of a Gatling run.

Usage: aws_metrics.py <gatling-log> [--skip-first-minutes N]

Reads the "STEP u=<units> from=<iso> to=<iso>" lines that DietitianUnitsSimulation prints and, for every step, prints
one row per metric: ECS CPU and memory of the backend and Keycloak services (average and the busiest task), ALB
response time and errors, RDS CPU, connections and CPU credits. The first minute of every step is skipped by default,
so the transition between steps does not blur the numbers. Read-only: only CloudWatch GetMetricData is called.
"""
import datetime as dt
import json
import re
import subprocess
import sys

REGION = "eu-central-1"
CLUSTER = "helti-dev-cluster"
SERVICES = {"backend": "helti-dev-backend", "keycloak": "helti-dev-keycloak"}
RDS = "helti-dev-postgres"


def aws(*args):
    out = subprocess.run(["aws", "--region", REGION, "--output", "json", *args], check=True, capture_output=True,
                         text=True).stdout
    return json.loads(out) if out.strip() else {}


def alb_dimensions():
    lbs = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    lb = next(item for item in lbs if item["LoadBalancerName"].startswith("helti-dev"))
    lb_dim = lb["LoadBalancerArn"].split(":loadbalancer/")[1]
    groups = aws("elbv2", "describe-target-groups", "--load-balancer-arn", lb["LoadBalancerArn"])["TargetGroups"]
    tg = {}
    for group in groups:
        # Target groups are named helti-dev-api and helti-dev-auth.
        name = "keycloak" if group["TargetGroupName"].endswith(("-auth", "keycloak")) else "backend"
        tg[name] = group["TargetGroupArn"].split(":")[-1]
    return lb_dim, tg


def queries(lb_dim, tg):
    items = []

    def add(qid, label, namespace, metric, dims, stat):
        items.append({"Id": qid, "Label": label, "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": metric,
                       "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]},
            "Period": 60, "Stat": stat}})

    for key, service in SERVICES.items():
        dims = {"ClusterName": CLUSTER, "ServiceName": service}
        add(f"{key}_cpu_avg", f"{key} CPU % avg", "AWS/ECS", "CPUUtilization", dims, "Average")
        add(f"{key}_cpu_max", f"{key} CPU % busiest task", "AWS/ECS", "CPUUtilization", dims, "Maximum")
        add(f"{key}_mem_max", f"{key} memory % busiest task", "AWS/ECS", "MemoryUtilization", dims, "Maximum")
        group = tg[key]
        tdims = {"LoadBalancer": lb_dim, "TargetGroup": group}
        add(f"{key}_rt_p95", f"{key} ALB response p95 s", "AWS/ApplicationELB", "TargetResponseTime", tdims, "p95")
        add(f"{key}_req", f"{key} ALB requests/min", "AWS/ApplicationELB", "RequestCount", tdims, "Sum")
        add(f"{key}_5xx", f"{key} target 5xx/min", "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", tdims, "Sum")
    add("elb_5xx", "ALB own 5xx/min", "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", {"LoadBalancer": lb_dim}, "Sum")
    rds = {"DBInstanceIdentifier": RDS}
    add("rds_cpu", "RDS CPU %", "AWS/RDS", "CPUUtilization", rds, "Maximum")
    add("rds_conn", "RDS connections", "AWS/RDS", "DatabaseConnections", rds, "Maximum")
    add("rds_credit", "RDS CPU credits", "AWS/RDS", "CPUCreditBalance", rds, "Minimum")
    return items


def fetch(items, start, end):
    result = aws("cloudwatch", "get-metric-data", "--metric-data-queries", json.dumps(items),
                 "--start-time", start.isoformat(), "--end-time", end.isoformat())
    return {r["Label"]: r["Values"] for r in result["MetricDataResults"]}


def main():
    log, skip = sys.argv[1], 1
    if "--skip-first-minutes" in sys.argv:
        skip = int(sys.argv[sys.argv.index("--skip-first-minutes") + 1])
    steps = []
    for line in open(log, encoding="utf-8"):
        match = re.search(r"STEP u=(\d+) from=(\S+) to=(\S+)", line)
        if match:
            parse = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            steps.append((int(match.group(1)), parse(match.group(2)), parse(match.group(3))))
    if not steps:
        sys.exit("No STEP lines in " + log)
    lb_dim, tg = alb_dimensions()
    items = queries(lb_dim, tg)
    labels = [item["Label"] for item in items]
    print("metric".ljust(34) + "".join(f"u={units}".rjust(10) for units, _, _ in steps))
    columns = []
    for _, start, end in steps:
        data = fetch(items, start.replace(second=0, microsecond=0) + dt.timedelta(minutes=skip), end)
        columns.append(data)
    for label in labels:
        row = label.ljust(34)
        for data in columns:
            values = data.get(label) or []
            if not values:
                row += "-".rjust(10)
                continue
            if "credits" in label:
                value = min(values)
            elif "requests" in label or "5xx" in label:
                value = sum(values) / len(values)
            else:
                value = max(values)
            row += (f"{value:.3f}" if "response" in label else f"{value:.1f}").rjust(10)
        print(row)


if __name__ == "__main__":
    main()
