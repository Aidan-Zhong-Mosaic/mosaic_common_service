# The platform layer

This file is the single source of truth for deploying and managing every
microservice on Kubernetes - from an empty AWS account to a fully running
cluster. Everything else (each Dockerfile, each Helm chart, each
service-specific README) lives with that service's own code, in that
service's own repo - this file is the ordered map that ties them together
and covers everything that isn't specific to any one service (the cluster
itself, cluster-wide addons, AWS resources). This lives in
mosaic_common_service because that's this project's role in the platform:
the shared backend other services call into for cross-cutting capabilities
(governed Redshift access today; MCP tool calls, LLM/SLM routing, and
shared algorithms are expected to land here too), not just a Redshift
gateway that happens to share a folder with these docs.

## In plain terms

Today, three things run: a chat assistant, World Monitor, and Mosaic
Common Service - the shared backend other tools call into rather than each
building the same capability separately; right now that's governed
Redshift access, with more shared capabilities (calling other AI tools,
routing to different language models) planned. All three run as one or more
Docker containers, started and restarted by hand on a couple of EC2
servers, each with its own upgrade process. That works, but nothing brings
a crashed container back up other than someone noticing, and updating each
app is a different set of steps.

Kubernetes replaces "a person runs `docker compose up` on a specific server"
with a small platform that runs containers *for* you: if one crashes, it
restarts automatically, usually within seconds, without anyone needing to
notice or intervene. Every app gets upgraded the same way, with one command,
regardless of how many pieces it's made of. And there's a dashboard showing
the health of everything in one place, instead of SSHing into different
servers to check.

**Status as of this writing: fully prepared, not yet turned on.** Every
service's Kubernetes configuration is written, reviewed, and ready. What
doesn't exist yet is the Kubernetes cluster itself - that's a deliberate,
one-time setup step (below) that hasn't been done because it's the point
where this starts costing money and needs a green light, not because
anything is unfinished.

## What this creates on AWS

Worth knowing before running any of this, since each of these has an
ongoing cost:

- **One EKS cluster** (the Kubernetes control plane) - a flat per-hour AWS
  charge regardless of how much or little is running on it.
- **EC2 instances** (the "nodes" the cluster schedules containers onto) -
  this is where most of the cost scales with usage; sized/counted at
  cluster-creation time below.
- **EBS volumes** - persistent disks for anything that needs to keep data
  across restarts (mongodb, the vector database, Meilisearch, Redis, file
  uploads).
- **One or more Application Load Balancers (ALB)** - created automatically
  the first time a service's Ingress is turned on; this is what replaces
  the manual nginx currently running on the EC2 hosts.
- **ECR repositories** - where the built Docker images live (small storage
  cost, effectively negligible next to the above).

Nothing here is provisioned by Terraform - it's all either `eksctl`/`aws`
CLI commands (one-time, run by hand, documented below) or Helm charts (the
services and addons).

## Prerequisites

Tools needed on whatever machine runs the commands below (not the EC2
servers - your own laptop, or a CI runner later):

- `aws` CLI, configured with credentials that have permission to create an
  EKS cluster, IAM roles, and ECR repositories (**not installed on this
  machine as of this session** - install it first).
- `eksctl` (the standard CLI for creating/managing EKS clusters).
- `kubectl` (already present on this machine, bundled with Docker Desktop).
- `helm` v3 (**not installed on this machine as of this session** - install
  it first; every deploy step below depends on it).
- `docker`, for building images.

## Full deployment runbook

Run these in order - later phases depend on earlier ones. Each phase links
to the file with the actual commands rather than repeating them here, so
this stays the map, not a second copy that can drift out of sync.

### Phase 0 - Create the EKS cluster

Nothing below this line exists yet. This is the one step this file hasn't
covered before now:

```bash
eksctl create cluster \
  --name mosaic-platform \
  --region <aws-region> \
  --nodegroup-name standard-workers \
  --node-type t3.medium \
  --nodes 3 \
  --nodes-min 2 \
  --nodes-max 5 \
  --managed
```

Fill in `<aws-region>` and confirm the node type/count with whoever owns
the AWS account - `t3.medium` x3 is a reasonable small-cluster starting
point, not a sized recommendation for your actual load. **Also confirm
which VPC/subnets to use before running this**: mosaic_common_service's
current Redshift capability needs real network connectivity to Redshift
(see its own `ARCHITECTURE.md`), so the cluster's VPC should either be the
one your current EC2 hosts and Redshift already share, or properly peered
with it - letting `eksctl` create a brand new, isolated VPC by default will
likely break that connectivity. This takes 15-20 minutes; `eksctl` also configures your
local `kubectl` to point at the new cluster once done.

### Phase 1 - Create the ECR repositories

One per image this platform builds:

```bash
for repo in mosaic-common-service worldmonitor worldmonitor-ais-relay worldmonitor-redis-rest mosaic-ai-chat; do
  aws ecr create-repository --repository-name "$repo" --region <aws-region>
done
```

### Phase 2 - Cluster addons

The AWS Load Balancer Controller (required before any service's Ingress
does anything), metrics-server, the Kubernetes Dashboard + its auth proxy,
and kube-prometheus-stack. Full commands in the "Cluster addons" section
below - do at least the Load Balancer Controller now, since every service
after this point may want it; the rest (Dashboard, Prometheus) can happen
any time.

### Phase 3 - Build, push, and configure each service

For each of the three services, in any order (they don't depend on each
other): build and push its image(s) to the ECR repos from Phase 1, then
create its namespace and its Secrets/ConfigMaps. The exact commands differ
per service (different images, different secrets) and live in that
service's own README so they stay next to the chart they describe:

- [mosaic_common_service/deploy/helm/README-deploy.md](helm/README-deploy.md)
- [worldmonitor_aidan_mosaic/deploy/helm/README-deploy.md](../../worldmonitor_aidan_mosaic/deploy/helm/README-deploy.md)
- [mosaic-ai-chat/deploy/helm/README-deploy.md](../../mosaic-ai-chat/deploy/helm/README-deploy.md)

Each covers, in order: building the image(s), creating the namespace,
creating the Secrets/ConfigMaps, and copying `values-prod.yaml.example` to
a filled-in `values-prod.yaml`.

### Phase 4 - Deploy

Once every service's `values-prod.yaml` exists (end of Phase 3):

```bash
bash deploy/deploy-all.sh
```

This is the `helm upgrade --install` loop covered in "Deploying" below -
every later code change goes through this same command, not Phase 3's
one-time setup.

### Phase 5 - Expose services and cut over DNS

For each service you want reachable from outside the cluster, set
`ingress.enabled: true` in its `values-prod.yaml` and re-run
`deploy-all.sh` (or that service alone). Find each ALB's address with
`kubectl -n <namespace> get ingress`. Once confirmed working, point the
real domains at the new ALB address(es) instead of the current EC2 hosts,
and only then decommission the manual nginx configs on those EC2 boxes
(`/etc/nginx/sites-enabled/mosaic`, `/etc/nginx/conf.d/worldmonitor.conf`,
etc.) - keep the old path running in parallel until DNS has actually
cut over and you've confirmed the new path works end to end.

### Phase 6 - Verify

```bash
kubectl get pods -A
```

Everything should be `Running`. Each service's own README has a more
specific health-check (`curl .../health`, etc.).

## Why this exists

You had three different upgrade habits (`scripts/upgrade.sh`,
`docker compose up -d --build`, plain `docker run`) because each service grew
its own. Moving to Kubernetes doesn't remove that per-service ownership - it
replaces "how do I run this container" with "how do I run this Helm chart",
still decided per service. What Kubernetes + Helm add on top is a *shared*
answer to the things Compose can't do across a fleet:

- **One cluster, many services** - `kubectl get pods -A` shows everything;
  Compose only ever knew about the services in its own file.
- **Self-healing** - a `Deployment` restarts crashed pods and reschedules them
  onto a healthy node automatically. `restart: unless-stopped` only restarts
  in place, on the same (possibly also-dead) host. This is genuinely
  different behavior, not just a bigger version of restart.
- **One command per service, same shape every time** -
  `helm upgrade --install <name> <chart> -f values-prod.yaml` replaces
  `upgrade.sh`'s bespoke build-and-restart sequence, `docker compose up -d
  --build`, and any manual `docker run`, with the same command regardless of
  how many containers that service happens to have.
- **A real rollback** - `helm rollback <name> <revision>` reverts every
  object the chart manages to exactly what a previous release put there.
  `git checkout <old-sha> && upgrade.sh` reverts code, but not whatever else
  changed in the meantime (env vars, replica count).

## Layout

```
mosaic_common_service/          <- this repo - shared platform home
  deploy/
    PLATFORM.md                        <- this file
    deploy-all.sh                      <- loops helm upgrade --install per service
    helm/mosaic-common-service/        <- this service's own chart
    helm/README-deploy.md              <- this service's own deploy instructions
    cluster-addons/                    <- one-time, cluster-wide installs (not
      kubernetes-dashboard-values.yaml    tied to any single app/namespace) -
      kube-prometheus-stack-values.yaml   see "Cluster addons" below
      dashboard-auth-proxy/              <- our own small chart

worldmonitor_aidan_mosaic/      <- sibling repo, its own chart
  deploy/helm/worldmonitor/
  deploy/helm/README-deploy.md

mosaic-ai-chat/                 <- sibling repo, its own chart
  deploy/helm/mosaic-ai-chat/
  deploy/helm/README-deploy.md
  Dockerfile.k8s                       <- the production image build
```

Each chart is deployed as its own independent Helm *release* - there's no
umbrella chart wiring them together as one unit. That's deliberate: these
three (and more to come) are separately-versioned, separately-upgraded
services today, and an umbrella chart would force them to share a version
number and a release lifecycle they don't actually share. `deploy-all.sh` is
just a loop, not a dependency graph - if you only need to redeploy
worldmonitor, run `deploy-all.sh worldmonitor` and nothing else moves.

**Note:** this only works because all the project folders sit as siblings on
this machine (`deploy-all.sh` references `../worldmonitor_aidan_mosaic/...`
etc.). If that ever changes (different checkout layout, CI runner, a second
developer's machine), update the paths in the `SERVICES` array.

## Deploying

```bash
# everything
bash deploy/deploy-all.sh

# one service
bash deploy/deploy-all.sh worldmonitor
```

Each service's own `deploy/helm/README-deploy.md` has the one-time setup
(building/pushing the image, creating Secrets/ConfigMaps, filling in
`values-prod.yaml`) that has to happen before its first deploy - see Phase 3
above.

## Onboarding a new microservice

1. In that service's own repo, add `deploy/helm/<name>/` following the
   pattern in this repo (simple, single-component), `worldmonitor_aidan_mosaic`
   (multi-component, one Deployment+Service pair per component sharing one
   chart), or `mosaic-ai-chat` (multi-component with PVCs and
   externally-managed databases).
2. Add one line to the `SERVICES` array in `deploy/deploy-all.sh`.
3. Add its own `deploy/helm/README-deploy.md` with that service's one-time
   setup.
4. Create its ECR repo(s) (Phase 1 above).

## Conventions across charts

- **One namespace per service** (`mosaic`, `worldmonitor`, `mosaic-chat`, ...)
  rather than one shared namespace - keeps `kubectl get pods -n X` scoped to
  one service, and RBAC/NetworkPolicy can be scoped per namespace later
  without restructuring anything.
- **Secrets/ConfigMaps holding real config are never created through Helm
  values in production.** Every chart supports an `existingSecret` (and
  where relevant `existingConfigMap`) value pointing at something you (or,
  later, External Secrets Operator syncing from AWS Secrets Manager)
  pre-create with `kubectl create secret`/`kubectl create configmap`. The
  `create: true` / inline-values path in each chart is for local/dev
  convenience only.
- **`values-prod.yaml` is gitignored, `values-prod.yaml.example` is
  committed** - the example shows the shape without real values in it.
- **Images come from ECR**, tagged per release (avoid floating `:latest` in
  `values-prod.yaml` once you're past initial setup - it makes `helm
  rollback` only roll back the k8s objects, not the image, which can leave
  you rolled back to old config running the new image).

## Cluster addons

Everything above is a *service* - namespaced, independently deployed,
covered by `deploy-all.sh`. Addons are different: one-time, cluster-wide
installs that every service's Ingress or monitoring depends on but that
don't belong to any single service's namespace or release lifecycle. They
live in `deploy/cluster-addons/` and are installed by hand, not looped over
by `deploy-all.sh`, since there's no per-service "one line to add" for them.

### AWS Load Balancer Controller

Required before *any* chart's `ingress.enabled: true` does anything - it's
what turns a k8s Ingress resource into a real ALB. One-time, cluster-wide:

```bash
eksctl utils associate-iam-oidc-provider --cluster <cluster-name> --approve
eksctl create iamserviceaccount \
  --cluster <cluster-name> --namespace kube-system \
  --name aws-load-balancer-controller --approve \
  --attach-policy-arn arn:aws:iam::<account-id>:policy/AWSLoadBalancerControllerIAMPolicy
helm repo add eks https://aws.github.io/eks-charts
helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=<cluster-name> \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller
```

(See AWS's own docs for the current `AWSLoadBalancerControllerIAMPolicy` JSON
- it's revised periodically.)

### metrics-server

Needed for `kubectl top` and for the Kubernetes Dashboard's CPU/memory
columns to show anything. Standard EKS add-on:

```bash
eksctl create addon --name metrics-server --cluster <cluster-name>
```

### Kubernetes Dashboard (pod browser)

The "fast, lite" option: a web UI for pods/logs/exec/events across every
namespace. It doesn't have real username/password auth built in (it's
built around bearer tokens/SSO), so `dashboard-auth-proxy/` - a small
in-repo chart, not upstream - sits in front doing HTTP Basic Auth with
credentials you control.

```bash
# 1. The dashboard itself
helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/
helm install kubernetes-dashboard kubernetes-dashboard/kubernetes-dashboard \
  -n kubernetes-dashboard --create-namespace \
  -f cluster-addons/kubernetes-dashboard-values.yaml

# Confirm the Service name/port it actually created, and update
# cluster-addons/dashboard-auth-proxy/values.yaml's `upstream` block if it
# doesn't match (chart versions differ - see the comment in that file):
kubectl -n kubernetes-dashboard get svc

# 2. RBAC: by default the dashboard can't see much. Bind it (or a dedicated
# admin-user ServiceAccount) to the built-in "view" ClusterRole for
# read-only visibility across every namespace - swap for "cluster-admin" if
# you also want to edit/delete from the UI, or a narrower custom Role if you
# want less:
kubectl create clusterrolebinding dashboard-view \
  --clusterrole=view \
  --serviceaccount=kubernetes-dashboard:kubernetes-dashboard

# 3. Basic auth credentials - pick your own username/password:
htpasswd -c .htpasswd <username>   # prompts for a password
kubectl -n kubernetes-dashboard create secret generic dashboard-auth-htpasswd \
  --from-file=.htpasswd=./.htpasswd
rm .htpasswd   # don't leave this sitting on disk

# 4. The auth proxy, fronting the dashboard
helm upgrade --install dashboard-auth-proxy cluster-addons/dashboard-auth-proxy \
  -n kubernetes-dashboard \
  --set htpasswd.existingSecret=dashboard-auth-htpasswd \
  --set ingress.enabled=true
```

Reach it at whatever address `kubectl -n kubernetes-dashboard get ingress`
shows for `dashboard-auth-proxy`.

### kube-prometheus-stack (Prometheus + Grafana)

The long-term option: metrics history, dashboards, alerting - not just
current pod status. One Helm install pulls in Prometheus, Grafana,
Alertmanager, node-exporter, and kube-state-metrics together, with a set of
default dashboards already wired up.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f cluster-addons/kube-prometheus-stack-values.yaml
```

Grafana has real username/password login built in (unlike k8s Dashboard),
so it doesn't need its own auth-proxy - see the comments in
`cluster-addons/kube-prometheus-stack-values.yaml` for setting a real admin
password via a Secret instead of the chart's default. Expose it the same
way as any other service (an Ingress) once you're ready.

## What's not here yet

- **GitOps.** Right now `deploy-all.sh` (and the cluster-addon installs
  above) are things a person runs by hand. If/when deploys should happen
  automatically on merge, that's ArgoCD or Flux reading from these same
  charts - worth adding once there's more than one person deploying, not
  before.
- **A tested rollback path for Phase 5's DNS cutover.** The runbook says
  "keep the old path running in parallel" but doesn't yet spell out exactly
  how far to test before flipping DNS, or what the rollback looks like if
  something's wrong after cutover. Worth writing once you're actually at
  that phase and know the real domain situation.
