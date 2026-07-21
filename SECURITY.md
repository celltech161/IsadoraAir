# Security Policy

## Reporting a Vulnerability

Please report suspected security vulnerabilities privately to:

**celltech161@yahoo.com**

Please do **not** open a public GitHub issue for security concerns.

Include as much detail as you can: affected version / commit, reproduction steps, and (if you have one) a proposed fix.  You'll get an acknowledgement within a few days.  A fix and coordinated disclosure timeline are discussed case-by-case.

## Scope

IsadoraAir is a self-hosted broadcast automation stack.  Security-relevant surfaces include:

- Authentication and session handling in the Django app
- The RBDS UECP encoding pipeline (a bug that emits malformed frames could crash a transmitter's RDS encoder — treated as security-relevant)
- Any input that reaches shell/subprocess calls (audio-file paths, ALSA device strings, admin-supplied fdkaac argument templates)
- Any dependency with a live CVE that the deployed instance is exposed to

Out of scope: configuration mistakes an operator makes on their own install (running with `DEBUG=True`, `ALLOWED_HOSTS=['*']`, publishing `.env`, etc.).

## Deployed Instance

The KOGR-LP production install runs behind nginx TLS termination on a machine that's not publicly reachable from the internet.  This repo is the source; how you deploy it is up to you.
