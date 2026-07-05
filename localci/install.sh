#!/usr/bin/env bash
# Bootstrap the local CI/CD convention onto a box: git template hooks (future
# repos inherit them on init/clone) + the per-repo installer. The convention's
# source of truth lives here in clanker; per-repo CI suites live in each repo.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p ~/.git-templates/hooks ~/bin
cp "$HERE"/hooks/pre-push "$HERE"/hooks/post-commit ~/.git-templates/hooks/
chmod +x ~/.git-templates/hooks/pre-push ~/.git-templates/hooks/post-commit
cp "$HERE"/CONVENTION.md ~/.git-templates/README.md
cp "$HERE"/localci-install ~/bin/localci-install
chmod +x ~/bin/localci-install
git config --global init.templateDir ~/.git-templates
echo "local CI/CD convention installed: templateDir set; run 'localci-install <repo>' (or --all) for existing repos"
