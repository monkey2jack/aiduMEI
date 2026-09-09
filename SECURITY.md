# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's private vulnerability reporting:

- Go to <https://github.com/monkey2jack/aiduMEI/security/advisories/new>

If that channel is unavailable, open a minimal public issue asking for a
private contact — do not include any details of the vulnerability.

We aim to acknowledge reports within 72 hours and to ship a fix or a
documented mitigation decision within 14 days, severity permitting.

## Scope notes

- aiduMEI is a **self-hosted, single-process** memory engine. It is designed
  for loopback or trusted-reverse-proxy deployment with `AIDUMEM_API_TOKEN` /
  UI password enabled. It is **not** a mutually-distrusting multi-tenant SaaS:
  checkpoints / persona banks / observation banks are not on the two-axis
  tenant model. See README's deployment sections before reporting
  configuration-class issues.
- Historical audits and remediations are tracked in
  `docs/SECURITY-AUDIT-LEDGER.md`.

## 中文摘要

安全漏洞请走 GitHub 私密漏洞报告（Security → Advisories），不要开公开
issue。72 小时内确认收到，14 天内给修复或书面缓解决定。本项目是自托管
单进程记忆引擎，信任边界（回环 / 可信反代 / 非多租户 SaaS）见 README
部署章节；历史审计见 `docs/SECURITY-AUDIT-LEDGER.md`。
