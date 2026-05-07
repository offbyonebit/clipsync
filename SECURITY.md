# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |
| < Latest| :x:                |

We recommend always using the latest version of ClipSync for the most secure
experience.

## Reporting a Vulnerability

We take the security of ClipSync seriously. If you discover a security
vulnerability, please follow these steps:

### How to Report

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them by contacting @offbyonebit on GitHub via direct message
or create a draft security advisory in the repository.

You should receive a response within 48 hours. If for some reason you do not,
please follow up to ensure we received your original message.

### What to Include

Please include the following information in your report:

* Type of issue (e.g., buffer overflow, SQL injection, cross-site scripting, etc.)
* Full paths of source file(s) related to the issue
* Location of the affected source code (tag/branch/commit or direct URL)
* Any special configuration required to reproduce the issue
* Step-by-step instructions to reproduce the issue
* Proof-of-concept or exploit code (if possible)
* Impact of the issue, including how an attacker might exploit it

### Preferred Languages

We prefer all communications to be in English.

## Security Best Practices

When using ClipSync, follow these best practices:

1. **Keep your encryption passphrase secure** - If you enable at-rest
   encryption, use a strong, unique passphrase and share it only with trusted
   devices.

2. **Verify device IDs** - When pairing devices, always verify the device ID
   through the QR code or manual entry to prevent man-in-the-middle attacks.

3. **Keep Syncthing updated** - ClipSync relies on Syncthing for peer-to-peer
   communication. Ensure your Syncthing installation is kept up to date.

4. **Monitor connected devices** - Regularly review the list of paired devices
   and remove any that are no longer needed.

5. **Use firewall rules** - Consider restricting Syncthing's network access
   to only trusted networks if you're concerned about exposure.

## Disclosure Policy

Once a security issue is reported:

1. We will acknowledge receipt of your report within 48 hours
2. We will investigate the issue and confirm the vulnerability
3. We will develop a fix and test it thoroughly
4. We will release a security update and publish an advisory
5. We will credit the reporter (unless you prefer to remain anonymous)

We aim to resolve critical vulnerabilities within 30 days of disclosure.

## Security Updates

Security updates will be announced via:

* GitHub Releases with security advisories
* Updates to the SECURITY.md file
* Direct notification to affected users for critical issues
