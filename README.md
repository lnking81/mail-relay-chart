# Mail Relay Helm Chart

A Helm chart for deploying a mail relay server with Postfix and OpenDKIM on Kubernetes.

## Overview

This chart deploys a mail relay server that provides:

- SMTP relay functionality with Postfix
- DKI### 3. Required DNS Recor### 4. Testing

```bash
# Test SMTP connectivity
kubectl run --rm -i --tty debug --image=busybox --restart=Never -- sh
telnet <release-name> 25

# Check DNS propagation
kubectl get configmap <release-name>-dns-helper -o jsonpath='{.data.check-dns-propagation\.sh}' | bash

# Get complete DNS setup with detected IP
kubectl get configmap <release-name>-dns-helper -o jsonpath='{.data.get-dns-records-with-ip\.sh}' | bash
```

## Advanced Features

### Automatic IP Detection and DNS Management

When you enable `externalDns.autoManageDnsRecords: true`, the chart will:

1. **Detect External IP**: Uses multiple public IP services to detect your server's external IP
2. **Generate Enhanced SPF Records**: Creates SPF records with both IP and hostname for better reliability
3. **Create DNS ConfigMaps**: Stores all DNS records in Kubernetes ConfigMaps for easy retrieval
4. **Provide Ready-to-Use Records**: Generates exact DNS records you can copy to your DNS provider

**Benefits of IP-based SPF records:**

- More reliable than hostname-only SPF
- Faster DNS resolution
- Better compatibility with some email providers
- Fallback to hostname if IP changesch domain, you need to create:

**When autoManageDnsRecords is enabled:**

- **A Record**: `mail.example.com. A <DETECTED-IP>` (automatically determined)
- **MX Record**: `example.com. MX 10 mail.example.com.`
- **SPF Record**: `example.com. TXT "v=spf1 ip4:<DETECTED-IP> a:mail.example.com ~all"`
- **DKIM Record**: `mail._domainkey.example.com. TXT "v=DKIM1; ..."`
- **DMARC Record**: `_dmarc.example.com. TXT "v=DMARC1; p=none; rua=mailto:postmaster@example.com"`

**When autoManageDnsRecords is disabled:**

- **A Record**: `mail.example.com. A <YOUR-EXTERNAL-IP>` (manual)
- **MX Record**: `example.com. MX 10 mail.example.com.`
- **SPF Record**: `example.com. TXT "v=spf1 a:mail.example.com ~all"`
- **DKIM Record**: `mail._domainkey.example.com. TXT "v=DKIM1; ..."`
- **DMARC Record**: `_dmarc.example.com. TXT "v=DMARC1; p=none; rua=mailto:postmaster@example.com"`g with OpenDKIM
- Automatic DKIM key generation
- DNS helper tools and documentation
- External DNS integration
- Persistent storage for DKIM keys
- Network policies for security

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Persistent Volume support (if persistence is enabled)
- External DNS (optional, for automatic DNS record management)

## Installation

### Add the Helm Repository

```bash
# If using a Helm repository
helm repo add mail-relay https://your-repo-url
helm repo update
```

### Install the Chart

```bash
# Basic installation
helm install my-mail-relay mail-relay/mail-relay

# Install with custom values
helm install my-mail-relay mail-relay/mail-relay -f values.yaml

# Install with inline values
helm install my-mail-relay mail-relay/mail-relay \
  --set mail.hostname=mail.example.com \
  --set mail.domains[0].name=example.com
```

## Configuration

### Basic Configuration

The following table lists the configurable parameters and their default values:

| Parameter          | Description                | Default        |
| ------------------ | -------------------------- | -------------- |
| `image.repository` | Container image repository | `debian`       |
| `image.tag`        | Container image tag        | `12-slim`      |
| `image.pullPolicy` | Image pull policy          | `IfNotPresent` |

### Mail Configuration

| Parameter               | Description                         | Default                                         |
| ----------------------- | ----------------------------------- | ----------------------------------------------- |
| `mail.hostname`         | SMTP server hostname                | `mail.example.com`                              |
| `mail.domains`          | List of domains to handle           | `[{name: "example.com", dkimSelector: "mail"}]` |
| `mail.relayHost`        | External SMTP relay host (optional) | `""`                                            |
| `mail.relayPort`        | External SMTP relay port            | `587`                                           |
| `mail.trustedNetworks`  | Networks allowed to send mail       | `["127.0.0.0/8", "10.0.0.0/8", ...]`            |
| `mail.trustedIPs`       | Additional trusted IP addresses     | `[]`                                            |
| `mail.messageSizeLimit` | Maximum message size                | `50MB`                                          |

### DKIM Configuration

| Parameter             | Description                       | Default |
| --------------------- | --------------------------------- | ------- |
| `dkim.enabled`        | Enable DKIM signing               | `true`  |
| `dkim.keySize`        | DKIM key size in bits             | `2048`  |
| `dkim.autoGenerate`   | Automatically generate DKIM keys  | `true`  |
| `dkim.existingSecret` | Use existing secret for DKIM keys | `""`    |

### DNS and External DNS

| Parameter                          | Description                        | Default                   |
| ---------------------------------- | ---------------------------------- | ------------------------- |
| `externalDns.enabled`              | Enable external-dns annotations    | `true`                    |
| `externalDns.hostname`             | Hostname for external-dns          | `""` (uses mail.hostname) |
| `externalDns.ttl`                  | DNS record TTL                     | `300`                     |
| `externalDns.autoManageDnsRecords` | Automatically create DNS records   | `false`                   |
| `dnsHelper.enabled`                | Enable DNS helper tools            | `true`                    |
| `dnsHelper.extractDkimJob`         | Create job to extract DKIM records | `true`                    |

### Service Configuration

| Parameter                 | Description             | Default     |
| ------------------------- | ----------------------- | ----------- |
| `service.type`            | Kubernetes service type | `ClusterIP` |
| `service.ports.smtp.port` | SMTP service port       | `25`        |

### Persistence

| Parameter                   | Description               | Default         |
| --------------------------- | ------------------------- | --------------- |
| `persistence.enabled`       | Enable persistent storage | `true`          |
| `persistence.storageClass`  | Storage class for PVC     | `""`            |
| `persistence.accessMode`    | Access mode for PVC       | `ReadWriteOnce` |
| `persistence.size`          | Size of PVC               | `1Gi`           |
| `persistence.existingClaim` | Use existing PVC          | `""`            |

### Security

| Parameter                            | Description              | Default               |
| ------------------------------------ | ------------------------ | --------------------- |
| `networkPolicy.enabled`              | Enable network policy    | `true`                |
| `networkPolicy.ingress.allowedCIDRs` | Allowed CIDR blocks      | `["10.0.0.0/8", ...]` |
| `securityContext.runAsUser`          | User ID to run container | `0`                   |
| `podSecurityContext.runAsUser`       | Pod user ID              | `0`                   |

### Resources and Scheduling

| Parameter                   | Description     | Default |
| --------------------------- | --------------- | ------- |
| `resources.limits.cpu`      | CPU limit       | `500m`  |
| `resources.limits.memory`   | Memory limit    | `512Mi` |
| `resources.requests.cpu`    | CPU request     | `100m`  |
| `resources.requests.memory` | Memory request  | `128Mi` |
| `nodeSelector`              | Node selector   | `{}`    |
| `tolerations`               | Pod tolerations | `[]`    |
| `affinity`                  | Pod affinity    | `{}`    |

### Monitoring

| Parameter                            | Description           | Default |
| ------------------------------------ | --------------------- | ------- |
| `monitoring.enabled`                 | Enable monitoring     | `false` |
| `monitoring.serviceMonitor.enabled`  | Create ServiceMonitor | `false` |
| `monitoring.serviceMonitor.interval` | Scrape interval       | `30s`   |

## Usage Examples

### Example 1: Basic Mail Relay

```yaml
mail:
  hostname: "mail.example.com"
  domains:
    - name: "example.com"
      dkimSelector: "mail"

dkim:
  enabled: true
  autoGenerate: true

persistence:
  enabled: true
  size: 1Gi
```

### Example 2: Multi-Domain Setup with Auto DNS Management

```yaml
mail:
  hostname: "alerts.example.com"
  domains:
    - name: "alerts.example.com"
      dkimSelector: "alerts"
    - name: "newsletters.example.com"
      dkimSelector: "newsletters"

dkim:
  enabled: true
  keySize: 2048

externalDns:
  enabled: true
  autoManageDnsRecords: true # Enables automatic IP detection and DNS record generation
```

When `autoManageDnsRecords` is enabled:

- External IP is automatically detected from public IP services
- SPF records include both detected IP and hostname for better deliverability
- DNS ConfigMaps are created with ready-to-use DNS records
- Helper scripts provide the exact DNS records to configure

### Example 3: With External Relay

```yaml
mail:
  hostname: "mail.example.com"
  domains:
    - name: "example.com"
      dkimSelector: "mail"
  relayHost: "smtp.mailgun.org"
  relayPort: 587
  relayCredentials:
    enabled: true
    username: "postmaster@example.com"
    password: "your-password"
```

## Post-Installation

After installing the chart, you'll need to configure DNS records. The chart provides helper tools to assist with this:

### 1. Get DNS Records with Detected IP

```bash
# Get DNS records using the detected external IP
kubectl get configmap <release-name>-dns-helper -o jsonpath='{.data.get-dns-records-with-ip\.sh}' | bash

# Check specific domain DNS ConfigMap
kubectl get configmap <release-name>-dns-example-com -o jsonpath='{.data.spf-full}'
```

### 2. Get DKIM Records

```bash
# Method 1: Direct access
kubectl exec -n <namespace> deployment/<release-name>-mail-relay -- cat /data/dkim-keys/*.txt

# Method 2: Using helper script
kubectl get configmap <release-name>-dns-helper -o jsonpath='{.data.get-dkim-records\.sh}' | bash

# Method 3: Check extractor job logs
kubectl logs -n <namespace> job/<release-name>-dkim-extractor
```

### 2. Required DNS Records

For each domain, you need to create:

- **MX Record**: `example.com. MX 10 mail.example.com.`
- **SPF Record**: `example.com. TXT "v=spf1 a:mail.example.com ~all"`
- **DKIM Record**: `mail._domainkey.example.com. TXT "v=DKIM1; ..."`
- **DMARC Record**: `_dmarc.example.com. TXT "v=DMARC1; p=none; rua=mailto:postmaster@example.com"`

### 3. Testing

```bash
# Test SMTP connectivity
kubectl run --rm -i --tty debug --image=busybox --restart=Never -- sh
telnet <release-name> 25

# Check DNS propagation
kubectl get configmap <release-name>-dns-helper -o jsonpath='{.data.check-dns-propagation\.sh}' | bash
```

## Troubleshooting

### Common Issues

1. **DKIM Keys Not Generated**

   - Check if persistence is enabled
   - Verify PVC is bound
   - Check pod logs for errors

2. **External DNS Not Working**

   - Ensure external-dns is installed in cluster
   - Check external-dns controller logs
   - Verify DNS provider configuration

3. **Mail Not Relaying**
   - Check trusted networks configuration
   - Verify firewall rules
   - Check Postfix logs in container

### Getting Logs

```bash
# Application logs
kubectl logs -n <namespace> deployment/<release-name>-mail-relay

# DKIM extractor job logs
kubectl logs -n <namespace> job/<release-name>-dkim-extractor

# External DNS logs (if using external-dns)
kubectl logs -n kube-system deployment/external-dns
```

## Uninstallation

```bash
helm uninstall <release-name>

# Optionally remove PVC (if persistence was enabled)
kubectl delete pvc <release-name>-mail-relay-data
```

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For support and questions:

- GitHub Issues: [Create an issue](https://github.com/lnking81/mail-relay-chart/issues)
- Email: ilya@strukov.net

## Changelog

### v0.1.0

- Initial release
- Postfix and OpenDKIM integration
- Automatic DKIM key generation
- External DNS support
- DNS helper tools
- Network policies
- Persistent storage support
