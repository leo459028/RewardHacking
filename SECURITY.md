# Security Policy

The **RewardHacking** team takes security vulnerabilities and AI safety flaws seriously. We appreciate the responsible disclosure of any security concerns.

---

## Supported Versions

Only the latest active minor release receives active security patches:

| Version | Supported          | Notes                                     |
| ------- | ------------------ | ----------------------------------------- |
| `v1.5.x`| :white_check_mark: | Current active release (including v1.5.2) |
| `< v1.5`| :x:                | Historical versions; please upgrade       |

---

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability, prompt injection bypass, or authentication flaw, please report it via one of the following methods:

1. **GitHub Private Vulnerability Reporting** (Preferred):  
   Navigate to the **Security** tab of this repository on GitHub and select **Report a vulnerability**.
2. **Security Contact Email**:  
   Send an email to the lead maintainer with the subject prefix `[SECURITY VULNERABILITY] RewardHacking`.

### What to Include in Your Report

To help us investigate and reproduce the issue rapidly, please include:
- A clear description of the vulnerability and its potential impact.
- Step-by-step reproduction instructions, payload examples, or a proof-of-concept (PoC) script.
- Component(s) affected (e.g., `Prompt Sanitizer`, `Policy Engine`, `API Auth`, `Worker DLQ`, `Admin FinOps`).
- Relevant logs, headers, or execution plan IDs.

---

## Response Process & SLA

When a security vulnerability is reported:
1. **Acknowledgement**: We will acknowledge receipt of your report within **48 hours**.
2. **Assessment**: Within **7 business days**, we will determine the severity, confirm reproducibility, and discuss remediation plans.
3. **Fix & Advisory**: We will develop and test a patch, assign a CVE if applicable, and coordinate a synchronized release date.
4. **Credit**: We will publicly credit the reporter in release notes and security advisories (unless you request anonymity).

---

## Vulnerability Scope

### In-Scope
- Bypasses of policy engine rules that allow destructive operations without approval.
- Remote Code Execution (RCE) or arbitrary file read/write vulnerabilities in worker execution pipelines.
- Authentication and authorization bypasses (e.g., unauthorized access to admin FinOps endpoints or tenant API keys).
- Inadvertent leakages of system credentials, JWT secrets, or DB connection strings.
- Exploitable flaws in prompt sanitation or Classical Chinese prompt guard algorithms.

### Out-of-Scope
- Theoretical attacks without practical proof-of-concept.
- Known limitations explicitly documented in `docs/security.md` (e.g., bounded execution sandbox dry-run boundary).
- Denial of Service (DoS) attacks on publicly exposed demo instances without prior authorization.
