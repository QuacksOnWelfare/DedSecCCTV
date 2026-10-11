# Security Policy

Thanks for taking the time to look at the security of DedSec CCTV. This page explains what we support, how to report a problem, and how the bug bounty works.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest official release (1.0.1 and newer) | Yes |
| `main` branch | Yes |
| Anything older than 1.0.1 | No |

If you are on an older build, update first and check whether the issue is still there.

## How to report a vulnerability

Please report security issues privately through GitHub:

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability**.
3. Fill in the form.

This is the only channel for security reports. Do not post vulnerabilities in public issues, pull requests, discussions, or on Discord. Reports made in public are not eligible for a reward.

The Discord server (https://discord.com/invite/4gRcYtTQ5c) is for general questions and contact only. You can use it to ask whether a report arrived, but never share details there.

### What to include

- A clear description of the issue.
- Step-by-step instructions to reproduce it.
- The impact: what an attacker could actually do with it.
- The affected version (official build version or commit hash).
- A working proof of concept. This is required for a reward. It must show that the exploit really works, for example a script, request and response captures, or a screen recording, together with the reproduction steps.

Your proof of concept must be tested only against your own account or against your own self-hosted copy of the server. Theoretical reports, or reports that do not demonstrate a real impact, are not eligible for a reward. We will still read them, though.

## Response targets

- We aim to acknowledge your report within 7 days.
- We aim to send a triage update (confirmed, need more info, or not accepted) within 14 days.

Fix times depend on the issue. We will keep you updated while we work on it.

## Bug bounty

Rewards are discretionary. Each valid report is judged case by case by the project owner, based on its severity and real-world impact, roughly following CVSS. There are no fixed amounts. A reward can also include public credit in the release notes and a place in the Hall of Fame below, if you want that.

To qualify for a reward:

- The report must include a working proof of concept, as described above, that demonstrates the impact.
- Testing must have been done only against your own account or a self-hosted copy.
- The issue must be in scope and you must have followed the rules on this page.
- It must be the first valid report of that issue. Duplicates are not rewarded.

### Severity tiers

These are examples to give you an idea, not a complete list.

**Critical**
- Remote code execution on users' machines through the updater.
- Bypassing the signature check on updates so a modified build gets installed.
- Compromise of the official DedSec API server.
- Authentication bypass or takeover of any account without user interaction.

**High**
- Reading or changing other users' data (emails, account settings, sessions).
- Bypassing 2FA or the HWID/device check on someone else's account.
- Stealing or forging another user's session or access token.

**Medium**
- Bypassing rate limits in a way that makes brute forcing logins or 2FA codes practical.
- Account enumeration or similar leaks with a clear, demonstrated impact.
- Logic flaws in sign-up, verification, or password reset that weaken account security.

**Low**
- Minor information leaks (for example internal details in error messages) with limited impact.
- Small hardening issues that still have a demonstrated security effect.

## Scope

### In scope

- The official DedSec API (the auth API used by the official build).
- The official Windows build of DedSec CCTV and its updater.
- The source code in this repository.

### Out of scope

- Cracking or patching your own copy of the client to skip the login.
- Extracting the bundled catalog from the official build. This is a known design limitation.
- The third-party camera streams themselves.
- Cloudflare, OVH, or GitHub infrastructure.
- Denial of service, stress testing, or load testing.
- Spam.
- Social engineering of the owner, contributors, or users.
- Physical attacks.
- Automated scanner output with no proof of impact.
- Missing best-practice headers or settings with no real impact.
- Anything that requires an already compromised user PC.

## Safe harbour

If you follow these rules, we consider your research authorised and will not take legal action against you:

- Only test against your own accounts or your own self-hosted copy.
- Do not access, change, or delete other users' data.
- No denial of service or anything that degrades the service for others.
- If you get access to anything sensitive, stop right away and report it. Do not dig further.
- Give us reasonable time to fix the issue (around 90 days) before you disclose anything publicly.
- Follow the law.

Good-faith research under these rules will not lead to legal action from us.

## Hall of Fame

Thanks to the following people for responsibly reporting security issues:

None yet.
