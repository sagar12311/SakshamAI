#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

fail() {
  echo "release check failed: $*" >&2
  exit 1
}

test -f LICENSE || fail "LICENSE is missing"
test -f SECURITY.md || fail "SECURITY.md is missing"
test -f CONTRIBUTING.md || fail "CONTRIBUTING.md is missing"

for forbidden in \
  'backend/.venv' 'frontend/node_modules' 'frontend/dist' 'frontend/dist-electron' \
  'backend/models' 'services/meeting_intelligence.zip'; do
  test ! -e "$forbidden" || fail "forbidden release artifact: $forbidden"
done

if find . -type f \( -name '.env' -o -name '*.log' -o -name '*.db' -o -name '*.sqlite*' \
  -o -name '*.onnx' -o -name '*.wav' -o -name '*.dmg' -o -name '*.zip' \) -print -quit | grep -q .; then
  fail "private data, model, or generated artifact found"
fi

if rg -n --hidden --glob '!.git/**' \
  '(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})' .; then
  fail "probable credential found"
fi

if rg -n --hidden --glob '!.git/**' --glob '!tools/release-check.sh' '(sagarsaywankar|100\\.90\\.236\\.76|/Users/)' .; then
  fail "personal identifier or absolute home path found"
fi

git check-ignore -q backend/.env || fail "backend/.env is not ignored"
git check-ignore -q backend/models/example.onnx || fail "backend models are not ignored"
git check-ignore -q frontend/dist/example.js || fail "frontend build output is not ignored"

echo "release check passed"
