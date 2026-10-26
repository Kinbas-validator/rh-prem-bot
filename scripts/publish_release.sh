#!/usr/bin/env bash
set -euo pipefail
: "${REPOSITORY:?Set REPOSITORY to owner/name}"
: "${RELEASE_TAG:?Set RELEASE_TAG to vX.Y.Z}"
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'Invalid release tag\n' >&2; exit 1; }
assets=(dist/rh-prem-bot-installer.run dist/rh-prem-bot-installer.run.sha256)
for asset in "${assets[@]}"; do [[ -s "$asset" ]] || { printf 'Missing asset: %s\n' "$asset" >&2; exit 1; }; done
if draft="$(gh release view "$RELEASE_TAG" --repo "$REPOSITORY" --json isDraft --jq .isDraft 2>/dev/null)"; then
  [[ "$draft" == true ]] || { printf 'Published releases are immutable; increment the version.\n' >&2; exit 1; }
  gh release upload "$RELEASE_TAG" --repo "$REPOSITORY" --clobber "${assets[@]}"
else
  gh release create "$RELEASE_TAG" --repo "$REPOSITORY" --verify-tag --draft \
    --title "RWA Premium Monitor $RELEASE_TAG" --notes-file docs/RELEASE_NOTES.md "${assets[@]}"
fi
gh release edit "$RELEASE_TAG" --repo "$REPOSITORY" --draft=false --latest
