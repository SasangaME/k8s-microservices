# Operational Runbook — k8s-microservices

Operator-facing playbook for the `microservices` namespace. Every command assumes `kubectl` is pointed at the right cluster and that you have access to the `microservices` namespace.

```bash
export NS=microservices
alias k="kubectl -n $NS"
```

---

## 1. Service inventory

| Workload  | Kind         | Replicas | Critical deps              | Restart-safe? |
|-----------|--------------|----------|----------------------------|---------------|
| frontend  | Deployment   | 2        | api                        | Yes (stateless) |
| api       | Deployment   | 2        | postgres, redis            | Yes (stateless) |
| worker    | Deployment   | 3        | postgres, redis            | Yes — jobs requeue via `BRPOP` semantics only if not yet popped; in-flight jobs without ack are lost |
| redis     | Deployment   | 1        | PVC `redis-data`           | Yes — queue contents persist on PVC |
| postgres  | StatefulSet  | 1        | PVC `postgres-data-postgres-0` | Yes — but never delete the PVC |

Health endpoint: `GET /healthz` on the API (also wired as readiness probe).

---

## 2. Routine operations

### 2.1 Deploy / re-deploy

```bash
# Build & load
docker build -t api:latest apps/api && kind load docker-image api:latest    # if using kind
k rollout restart deploy/api
k rollout status  deploy/api --timeout=120s
```

Apply order for a clean cluster:

```bash
kubectl apply -f infra/cluster/namespace.yaml
kubectl apply -f infra/data/database/
kubectl apply -f infra/apps/api/
kubectl apply -f infra/apps/frontend/
kubectl apply -f infra/apps/worker/
kubectl apply -f infra/cluster/ingress.yaml
kubectl apply -f infra/cluster/network-policies/   # always last
```

### 2.2 Roll back a Deployment

```bash
k rollout history deploy/api
k rollout undo    deploy/api                  # previous revision
k rollout undo    deploy/api --to-revision=3  # specific revision
k rollout status  deploy/api
```

### 2.3 Scale

```bash
k scale deploy/api    --replicas=4
k scale deploy/worker --replicas=10           # drain backlog faster
k scale deploy/frontend --replicas=4
```

Postgres is a single-replica StatefulSet — **do not scale `postgres`**, the manifests are not configured for replication.

### 2.4 Restart a workload

```bash
k rollout restart deploy/api
k rollout restart statefulset/postgres        # rolling restart respects ordinal order
```

### 2.5 Update the database password

```bash
kubectl -n $NS create secret generic postgres-credentials \
  --from-literal=username=appuser \
  --from-literal=password='<new>' \
  --dry-run=client -o yaml | kubectl apply -f -

# Update the password inside Postgres itself, then bounce consumers:
k exec statefulset/postgres -- psql -U appuser -d appdb \
  -c "ALTER USER appuser WITH PASSWORD '<new>';"
k rollout restart deploy/api deploy/worker
```

---

## 3. Observability

### 3.1 Logs

```bash
k logs -l app=api -c api --tail=200 -f
k logs -l app=api -c fluent-bit --tail=200       # sidecar (stdout sink)
k logs -l app=worker --tail=200 -f
k logs statefulset/postgres --tail=200
```

API writes to `/var/log/api/api.log` inside the pod; Fluent Bit tails that file via the shared `emptyDir` and ships to stdout. To tap the file directly:

```bash
k exec deploy/api -c api -- tail -f /var/log/api/api.log
```

### 3.2 Pod / event status

```bash
k get pods -o wide
k describe pod <pod>
k get events --sort-by=.lastTimestamp | tail -30
k top pods                                       # requires metrics-server
```

### 3.3 DNS sanity check from inside the cluster

```bash
k run -it --rm dns-debug --image=busybox:1.36 --restart=Never -- \
  nslookup postgres.microservices.svc.cluster.local
```

---

## 4. Common incidents

### 4.1 API pods stuck in `Init`

**Symptom:** `wait-for-postgres` or `wait-for-redis` init container never completes.

```bash
k logs <api-pod> -c wait-for-postgres
k get pods -l app=postgres
k get svc postgres redis
```

Checks:
1. Is `postgres-0` `Running` and `Ready`? If not, see §4.4.
2. Does `nslookup postgres.microservices.svc.cluster.local` resolve from inside another pod?
3. Did you apply network policies before workloads? Re-apply the database policies — DNS must be allowed (`allow-dns-egress`).

### 4.2 API returns 5xx / cannot reach Postgres

```bash
k logs deploy/api -c api --tail=100
k exec deploy/api -c api -- nc -zv postgres.microservices.svc.cluster.local 5432
k exec deploy/api -c api -- nc -zv redis.microservices.svc.cluster.local 6379
```

Likely causes:
- Postgres pod not ready → check StatefulSet (§4.4).
- Network policy regression — confirm `api-policy` egress lists 5432/6379.
- Wrong credentials — Secret rotated but pods not restarted (`k rollout restart deploy/api`).

### 4.3 Jobs queued but `items` list is empty

The worker has crashed or can't reach Postgres.

```bash
k get pods -l app=worker
k logs -l app=worker --tail=100
k exec deploy/redis -- redis-cli LLEN jobs       # current backlog
```

Drain manually if needed:
```bash
k exec deploy/redis -- redis-cli LRANGE jobs 0 -1
```

### 4.4 Postgres pod won't start

```bash
k describe pod postgres-0
k logs postgres-0
k get pvc                                        # data volume bound?
k get events --field-selector involvedObject.name=postgres-0
```

- `CrashLoopBackOff` with permission errors → PVC was created with wrong fsGroup; check the StorageClass.
- `Pending` PVC → no default StorageClass, or no available PV.
- Probe failing (`pg_isready`) → exec in and run it manually:
  ```bash
  k exec postgres-0 -- pg_isready -U appuser -d appdb
  ```

**Never** `kubectl delete pvc postgres-data-postgres-0` to "fix" issues — that destroys the database.

### 4.5 Ingress returns 404 / 502

```bash
kubectl -n ingress-nginx get pods
kubectl -n ingress-nginx logs deploy/ingress-nginx-controller --tail=100
k get ingress microservices-ingress
k get endpoints frontend api
```

Checklist:
- `ingress-nginx` namespace label is `kubernetes.io/metadata.name=ingress-nginx` (network policies depend on it).
- `app.local` resolves to the Ingress LB / node IP in `/etc/hosts`.
- Endpoints non-empty (else the underlying Service has no ready pods).

### 4.6 NetworkPolicy regression — pods can't talk

The fastest reversibility test:

```bash
kubectl -n $NS delete networkpolicy default-deny-all
# verify connectivity, then re-apply
kubectl apply -f infra/cluster/network-policies/default-deny.yaml
```

If your CNI does not enforce NetworkPolicies (e.g. kind with default kindnet), policies are inert — symptoms will not match. Confirm the CNI supports policy enforcement.

---

## 5. Backup & restore

### 5.1 Postgres logical backup

```bash
k exec postgres-0 -- pg_dump -U appuser -d appdb -Fc -f /tmp/appdb.dump
kubectl -n $NS cp postgres-0:/tmp/appdb.dump ./appdb-$(date +%F).dump
```

### 5.2 Restore

```bash
kubectl -n $NS cp ./appdb-YYYY-MM-DD.dump postgres-0:/tmp/restore.dump
k exec postgres-0 -- pg_restore -U appuser -d appdb --clean /tmp/restore.dump
```

### 5.3 Redis snapshot

Redis runs with default RDB snapshotting on the `redis-data` PVC. To force a snapshot:

```bash
k exec deploy/redis -- redis-cli BGSAVE
kubectl -n $NS cp $(k get pod -l app=redis -o name | head -1 | cut -d/ -f2):/data/dump.rdb ./redis-$(date +%F).rdb
```

---

## 6. Smoke test

After any deploy:

```bash
# 1. All pods Ready
k get pods

# 2. API health
k port-forward svc/api 8080:8080 &
curl -fsS http://localhost:8080/healthz
kill %1

# 3. End-to-end via Ingress
curl -fsS http://app.local/api/items
curl -fsS -X POST http://app.local/api/jobs \
  -H 'content-type: application/json' \
  -d '{"name":"smoke-test"}'
sleep 2
curl -fsS http://app.local/api/items | grep smoke-test
```

---

## 7. Teardown

```bash
kubectl delete -f infra/cluster/network-policies/
kubectl delete -f infra/cluster/ingress.yaml
kubectl delete -f infra/apps/worker/
kubectl delete -f infra/apps/frontend/
kubectl delete -f infra/apps/api/
kubectl delete -f infra/data/database/
# PVCs survive namespace/Deployment deletion — remove explicitly if desired:
kubectl -n $NS delete pvc --all
kubectl delete -f infra/cluster/namespace.yaml
```

---

## 8. Escalation

| Symptom                                  | First responder action                    | Escalate if                                      |
|------------------------------------------|-------------------------------------------|--------------------------------------------------|
| Single pod CrashLoop                     | `rollout restart`, check logs             | Restart loop persists past 2 cycles              |
| Postgres unavailable                     | §4.4, then snapshot PVC                   | PVC bound but DB will not start                  |
| Whole namespace unreachable              | §4.6 (network policy bypass)              | Bypass restores traffic — file a policy bug      |
| Data corruption suspected                | Stop writers (`scale worker --replicas=0`), take backup (§5.1) | Any inconsistency confirmed |
