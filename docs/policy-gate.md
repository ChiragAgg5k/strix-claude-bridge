# Anthropic authentication policy gate

Last live-checked: 2026-08-18 against official Anthropic documentation.

Public release of the subscription-backed mode is blocked unless Anthropic gives prior written approval.

The official [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) states:

> Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods described in the Quickstart instead.

The official [Claude Code legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance) also says OAuth is intended for subscription purchasers and native Anthropic applications, while developers building products or services—including Agent SDK products—should use API-key authentication through Claude Console or a supported cloud provider.

## Consequences for this repository

- Live Team-backed execution is technical compatibility evidence, not distribution permission.
- Keep the repository private and do not release packages or advertise subscription-backed use to third parties without written Anthropic approval.
- A public release should either obtain that approval or redesign authentication around documented API-key/cloud-provider methods.
- Local credentials must never be extracted, copied, proxied, hosted, or shared regardless of repository visibility.
- Re-check the current official wording immediately before any visibility or release change.

This is a conservative project release gate, not legal advice.
