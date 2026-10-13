# Security Architecture & Threat Model

## 1. Zero-Inbound Host Exposure
The platform host operates behind an AWS EC2 Security Group configured with **zero inbound rules** (`IpPermissions: []`).
- No public ports (including 80, 443, 22) are open to the internet.
- Administrative and demonstration access is routed exclusively through **AWS Systems Manager (SSM) Session Manager** with encrypted port-forwarding over IAM authenticated websockets:
  ```bash
  aws ssm start-session --target <INSTANCE_ID> \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
  ```
- Public IPv4 is assigned strictly to allow the AWS SSM agent outbound connectivity to AWS endpoints without needing a costly NAT Gateway.

## 2. Docker Socket Isolation & tecnativa/docker-socket-proxy
Tenant containers must be dynamically created and updated, but granting the web application direct access to `/var/run/docker.sock` presents severe privilege escalation risks.
- The platform uses `tecnativa/docker-socket-proxy` bound exclusively to `127.0.0.1:2375`.
- **Security Notice on Filtering Mechanism:** The proxy enforces coarse-grained filtering by **Docker API section** (e.g. `CONTAINERS=1`, `NETWORKS=1`), not by individual action or container payload.
- To prevent abuse:
  - `EXEC=0`: `docker exec` commands are completely disabled.
  - `VOLUMES=0`: Arbitrary volume mounting is blocked.
  - `IMAGES=0`: Arbitrary image pulling from external registries is disabled.
  - `INFO=1`: Docker daemon stats and information are read-only.
- The `alloc` application process runs as a low-privileged system user that is **not** a member of the `docker` unix group.

## 3. Tenant Container Hardening
All tenant workloads execute an identical, fixed worker image (`alloc-worker:latest` built from Python 3.11 slim):
- **Read-only root filesystem**: Containers mount root as read-only with a small ephemeral `tmpfs` for scratch buffers.
- **Dropped capabilities**: `cap_drop=ALL`. No Linux capabilities granted.
- **No new privileges**: `no-new-privileges=True` prevents `setuid` binaries.
- **Process limit**: `pids_limit=100` protects against fork bombs.
- **Network isolation**: Attached exclusively to an internal bridge network (`alloc-tenants`) with no gateway or external internet routing.
- **No host mounts**: Zero access to host filesystem paths or Docker socket.

## 4. API & Application Defense
- **Strict Parameter Validation**: All API endpoints use Pydantic v2 schemas with tight numerical ranges (e.g. CPU $\in [0.05, 4.0]$, memory $\in [32, 16384]$ MB) and strict string enums. Shell commands and custom image strings are completely rejected.
- **Authentication**: Passwords hashed using Argon2id (`argon2-cffi`).
- **Session Security**: Cryptographically signed session cookies (`itsdangerous`) with `HttpOnly`, `SameSite=Strict`, and `Secure` (when TLS enabled).
- **Rate Limiting & CSRF**: Login rate limiting protects against brute force attacks; state-changing forms require CSRF validation.
- **Audit Logging**: All experiment starts, stops, configuration edits, and resets are written to an immutable `audit_logs` table.
- **Secret Hygiene**: Zero AWS credentials, API keys, or database passwords exist in source code or git repository. Runtime secrets reside in `/etc/alloc/env` (file mode `0600`, owned by `alloc`).
