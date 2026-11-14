#!/usr/bin/env bash
# sunnydash: run the device UI on a Mac/PC for previewing settings.
#   tools/preview_ui.sh        comma four (compact) UI
#   tools/preview_ui.sh big    comma 3/3X (large) UI
# Uses its own settings store (~/.comma_preview), so it never touches a real device's settings.
# First-time setup: uv sync --frozen --all-extras && scons openpilot/common msgq_repo openpilot/cereal
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source .venv/bin/activate

export OPENPILOT_PREFIX="_preview"
mkdir -p "/tmp/msgq_${OPENPILOT_PREFIX}"

if [[ "${1:-}" == "big" ]]; then
  export BIG=1
fi

# skip the first-run welcome, terms and training screens
python - <<'EOF'
from openpilot.common.params import Params
from openpilot.common.version import terms_version, terms_version_sp, training_version
p = Params()
p.put("HasAcceptedTerms", terms_version)
p.put("HasAcceptedTermsSP", terms_version_sp)
p.put("CompletedTrainingVersion", training_version)
EOF

exec python openpilot/selfdrive/ui/ui.py
