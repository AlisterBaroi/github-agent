# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x (latest) | Yes |
| < 1.0 | No |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, **please report it responsibly**. Do **not** open a public GitHub issue.

### How to Report

1. Go to the [Security Advisories](../../security/advisories) tab of this repository
2. Click **"Report a vulnerability"**
3. Provide a detailed description including:
   - The type of vulnerability (e.g., credential exposure, injection, SSRF)
   - Steps to reproduce
   - Affected components (e.g., FastAPI endpoints, MCP server communication, Kubernetes manifests)
   - Potential impact

Alternatively, you can email the maintainer directly at the address listed in their GitHub profile.

### What to Expect

- **Acknowledgment** within 48 hours of your report
- **Status update** within 7 days with an initial assessment
- **Fix timeline** communicated once the issue is confirmed — typically within 30 days for critical issues

If the vulnerability is accepted, we will:
1. Develop and test a fix privately
2. Release a patched version
3. Credit you in the release notes (unless you prefer to remain anonymous)

If the vulnerability is declined, we will explain why.

## Security Considerations

This project handles sensitive credentials and communicates with external services. Contributors and deployers should be aware of the following:

### GitHub Personal Access Token (PAT)

- **Never** commit a PAT or any secret to the repository
- Store PATs using [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/) in deployments
- Use the `.env` file (gitignored) for local development only
- Use fine-grained PATs with the minimum required permissions
- Rotate tokens regularly and set expiration dates

### API Keys

- Google Gemini / Anthropic API keys must be stored as Kubernetes Secrets, never in manifests or code
- The `.env.example` file contains only placeholder values — ensure `.env` files are never committed

### Network & Deployment

- The MCP server and agent communicate over the internal Kubernetes cluster network
- Port-forwarding (`kubectl port-forward`) is intended for local testing only — do not expose agent ports to the public internet without authentication
- CORS is configured with `allow_origins=["*"]` by default — restrict this to specific origins in production
- All container images should be pulled from trusted registries only

### Dependencies

- All Python dependencies are pinned to exact versions in `requirements.txt`
- Regularly audit dependencies for known vulnerabilities using tools like `pip-audit` or `safety`
- The Docker image uses `python:3.12-slim` as a minimal base to reduce attack surface