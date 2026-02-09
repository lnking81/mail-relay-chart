# Mail Relay Helm Chart - Development Guidelines

Production-ready Helm chart for deploying SMTP mail relay on Kubernetes with Postfix, OpenDKIM, and automated DNS management.

---

## 1. GENERAL RULES

### Language

- Explain in the user's language (RU/EN).
- Code comments, YAML comments, documentation, and commit messages must always be in English.

### Communication

- If requirements are ambiguous (2+ plausible interpretations), ask up to 3 clarifying questions.
- Otherwise proceed with reasonable defaults and state assumptions in the response.
- **Never make up or hallucinate** Helm values, Kubernetes API fields, Postfix options, or OpenDKIM parameters. If unsure, check official documentation first.

### Priorities (highest first)

1. Security (SMTP relay abuse prevention, DKIM integrity, network policies).
2. Reliability (health checks, proper service management, persistent storage).
3. Simplicity (KISS), minimal change surface (YAGNI), no duplication (DRY).
4. Maintainability + Kubernetes best practices.

---

## 2. PROJECT STRUCTURE

```
mail-relay-chart/
├── .github/
│   ├── copilot-instructions.md  # This file
│   ├── cr.yaml                  # Chart Releaser configuration
│   └── workflows/
│       ├── build-and-release.yml  # Release workflow (tags)
│       └── build-docker.yml       # Dev image builds (branches)
├── chart/                       # Helm chart directory
│   ├── Chart.yaml               # Chart metadata
│   ├── values.yaml              # Default values (reference)
│   └── templates/
│       ├── _helpers.tpl         # Template helpers
│       ├── deployment.yaml      # Main workload
│       ├── service.yaml         # Services (ClusterIP, LoadBalancer)
│       ├── configmap-*.yaml     # Configuration files
│       ├── secret-dkim.yaml     # DKIM key secrets
│       ├── job-dkim-init.yaml   # DKIM key generation job
│       ├── cronjob-dns-manager.yaml  # DNS record management
│       ├── pvc.yaml             # Persistent storage
│       ├── rbac.yaml            # ServiceAccount, Roles
│       ├── networkpolicy.yaml   # Network security
│       └── servicemonitor.yaml  # Prometheus monitoring
├── docker/                      # Container image
│   ├── Dockerfile               # Debian 13-based image
│   ├── .dockerignore
│   └── usr/local/bin/
│       └── entrypoint.sh        # Placeholder (overridden by chart)
└── README.md                    # User documentation
```

---

## 3. HELM CHART DEVELOPMENT

### Values Structure

The chart uses hierarchical configuration:

| Section       | Purpose                                           |
| ------------- | ------------------------------------------------- |
| `image`       | Container image configuration                     |
| `workload`    | Deployment/DaemonSet settings                     |
| `service`     | Internal, LoadBalancer, HostPort services         |
| `dns`         | External-dns integration, SPF/MX/DMARC records    |
| `mail`        | Postfix configuration (hostname, relay, networks) |
| `inbound`     | Inbound mail handling (optional)                  |
| `dkim`        | OpenDKIM settings                                 |
| `persistence` | PVC configuration                                 |
| `sidecar`     | Optional sidecar container                        |
| `logging`     | Log destination (stdout/file)                     |
| `hooks`       | Helm hook behavior                                |

### Template Conventions

```yaml
# Always include labels
metadata:
  name: {{ include "mail-relay.fullname" . }}-component
  labels:
    {{- include "mail-relay.labels" . | nindent 4 }}

# Use checksum annotations for automatic rollouts on config changes
annotations:
  checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}

# Conditional sections with proper indentation
{{- if .Values.feature.enabled }}
spec:
  {{- toYaml .Values.feature.config | nindent 2 }}
{{- end }}
```

### Adding New Features

1. **Add values** to `chart/values.yaml` with sensible defaults and documentation.
2. **Create/update templates** following existing patterns.
3. **Update NOTES.txt** if user-facing behavior changes.
4. **Update README.md** with configuration reference.
5. **Test locally** with `helm template` before committing.

### Common Helm Patterns Used

```yaml
# Domain iteration
{{- range $index, $domain := .Values.mail.domains }}
name: dkim-{{ $domain.name | replace "." "-" }}
{{- end }}

# Conditional environment variables
{{- if and .Values.mail.relayCredentials.enabled .Values.mail.relayCredentials.username }}
- name: RELAY_CREDENTIALS_ENABLED
  value: "true"
{{- end }}

# Secret reference vs inline value
{{- if .Values.dkim.existingSecret }}
secretName: {{ .Values.dkim.existingSecret }}
{{- else }}
secretName: {{ include "mail-relay.fullname" . }}-dkim
{{- end }}
```

---

## 4. DOCKER IMAGE DEVELOPMENT

### Base Image

- Based on `debian:13-slim` for stability and security updates.
- Multi-arch support: `linux/amd64` and `linux/arm64`.

### Installed Components

| Component    | Purpose                    |
| ------------ | -------------------------- |
| `postfix`    | SMTP mail relay            |
| `opendkim`   | DKIM email signing         |
| `supervisor` | Process management         |
| `kubectl`    | DNS endpoint management    |
| Debug tools  | Troubleshooting in cluster |

### Entrypoint Script Architecture

The actual entrypoint is generated by `configmap-entrypoint.yaml`, not the dockerfile's placeholder.

Key functions:

```bash
# Logging
info()   # Informational messages
warn()   # Warnings (non-fatal)
error()  # Errors (to stderr)
fail()   # Fatal error + exit 1

# Directory management
ensure_dir()  # Create dir with owner/group/mode
safe_copy()   # Copy with permissions verification

# Configuration
copy_postfix_config()        # From ConfigMap
prepare_sasl()               # Relay credentials
sync_dkim_keys_from_secrets()  # DKIM key installation

# Postfix queue
init_or_repair_postfix_spool()  # Persistent or ephemeral
verify_postfix_permissions()    # Directory permission audit
```

### Process Management

Supervisord manages services with proper dependency ordering:

| Service    | Priority | Auto-restart |
| ---------- | -------- | ------------ |
| `opendkim` | 100      | Yes          |
| `postfix`  | 200      | Yes          |

### Building Locally

```bash
cd mail-relay-chart

# Build for local testing
docker build -t mail-relay:dev ./docker/

# Build multi-arch (requires buildx)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/lnking81/mail-relay-chart:dev ./docker/
```

---

## 5. CI/CD WORKFLOWS

### Release Workflow (`build-and-release.yml`)

Triggered by: Tags matching `v*`

1. **Build Docker images** for amd64 and arm64 in parallel.
2. **Merge manifests** into multi-arch image.
3. **Update Chart.yaml** version from tag.
4. **Release Helm chart** to GitHub Pages via chart-releaser.

### Development Workflow (`build-docker.yml`)

Triggered by: Pushes to `main`/`master` affecting `docker/` directory.

- Builds and pushes dev images with branch/SHA tags.
- Uses GitHub Actions cache for faster builds.

### Versioning

- **Chart version**: `Chart.yaml` → `version` field.
- **App version**: `Chart.yaml` → `appVersion` field.
- **Image tag**: `values.yaml` → `image.tag`.

All three should align on release (automated by CI).

---

## 6. TESTING & VALIDATION

### Local Testing

```bash
# Lint chart
helm lint ./chart -f values.yaml

# Template rendering (dry-run)
helm template my-release ./chart -f values.yaml

# Install to local cluster
helm install test-relay ./chart -n mail --create-namespace \
  --set mail.hostname=mail.example.com \
  --set mail.domains[0].name=example.com
```

### Validating Changes

```bash
# Check for breaking changes
helm diff upgrade my-release ./chart -f values.yaml

# Test upgrade path
helm upgrade --install test-relay ./chart -n mail -f values.yaml

# Verify pod health
kubectl get pods -n mail -l app.kubernetes.io/name=mail-relay
kubectl logs -n mail deployment/test-relay -f
```

### SMTP Testing

```bash
# Port forward for testing
kubectl port-forward -n mail svc/test-relay 2525:25

# Test with swaks (inside container)
kubectl exec -n mail deployment/test-relay -- swaks \
  --to test@example.com --from sender@yourdomain.com \
  --server localhost:25
```

---

## 7. SECURITY CONSIDERATIONS

### Network Security

- **Network policies** restrict ingress to trusted CIDRs.
- **Trusted networks** in Postfix limit relay access.
- **SASL credentials** stored in Kubernetes Secrets.

### DKIM Key Management

- Keys generated by init job, stored in Secrets.
- Private keys mounted read-only with mode `0400`.
- `preserveSecretsOnDelete: false` for complete cleanup.

### Container Security

- Runs as root (required for Postfix).
- No privileged mode.
- Read-only root filesystem where possible.

---

## 8. KEY FILES REFERENCE

| File                                 | Purpose                           |
| ------------------------------------ | --------------------------------- |
| `chart/values.yaml`                  | All configurable options          |
| `chart/templates/deployment.yaml`    | Main workload definition          |
| `chart/templates/configmap-entrypoint.yaml` | Entrypoint script generation |
| `chart/templates/configmap-postfix.yaml` | Postfix main.cf/master.cf      |
| `chart/templates/configmap-opendkim.yaml` | OpenDKIM configuration        |
| `chart/templates/job-dkim-init.yaml` | DKIM key generation               |
| `chart/templates/cronjob-dns-manager.yaml` | DNS record updates           |
| `docker/Dockerfile`                  | Container image definition        |
| `.github/workflows/*.yml`            | CI/CD pipelines                   |

---

## 9. COMMON OPERATIONS

### Adding a New Domain

1. Add to `mail.domains[]` in values.
2. Set `hooks.runOnUpgrade: true` or run with `--set hooks.runOnUpgrade=true`.
3. Upgrade release to trigger DKIM key generation.
4. Verify DNS records are created.

### Rotating DKIM Keys

```bash
# Delete existing DKIM secret
kubectl delete secret -n mail my-release-dkim-example-com

# Trigger regeneration
helm upgrade my-release ./chart -f values.yaml \
  --set hooks.runOnUpgrade=true
```

### Debugging Mail Issues

```bash
# Check supervisord status
kubectl exec -n mail deployment/my-release -- supervisorctl status

# View Postfix queue
kubectl exec -n mail deployment/my-release -- postqueue -p

# Check DKIM signing
kubectl exec -n mail deployment/my-release -- \
  opendkim-testkey -d example.com -s mail -vvv
```

---

## 10. DO NOTs

- **Do not edit** `docker/usr/local/bin/entrypoint.sh` directly — it's overridden by the Helm chart's configmap.
- **Do not hardcode** IP addresses in templates — use values or auto-detection.
- **Do not skip** network policies in production deployments.
- **Do not store** DKIM private keys in values files — use Kubernetes Secrets.
- **Do not use** `latest` tag in production — pin specific versions.
- **Do not bypass** health checks — they prevent traffic to unhealthy pods.
