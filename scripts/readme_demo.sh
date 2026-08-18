#!/usr/bin/env bash
# Reproduce the credential-free README recording with real bridge and Docker paths.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE=(uv --project "$REPO" run --extra strix strix-claude-bridge)

case "${1:-}" in
  version)
    "${BRIDGE[@]}" --version
    ;;
  scan)
    demo_dir="$(mktemp -d "${TMPDIR:-/tmp}/strix-claude-readme.XXXXXX")"
    trap 'rm -rf "$demo_dir"' EXIT
    cp -R "$REPO/fixtures/vulnerable_app" "$demo_dir/vulnerable_app"
    cd "$demo_dir"
    "${BRIDGE[@]}" scan \
      --experimental \
      --dry-run \
      --target "$demo_dir/vulnerable_app" \
      --scan-mode quick \
      --run-name readme-demo \
      --max-turns 10 \
      --max-runtime 120 \
      | uv --project "$REPO" run python "$REPO/scripts/readme_demo_stream.py"
    ;;
  *)
    printf 'usage: %s {version|scan}\n' "$0" >&2
    exit 2
    ;;
esac
