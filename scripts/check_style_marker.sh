#!/usr/bin/env bash
# AC7 harness: proves newsletter_style.md rules actually reach the Gemini
# prompt and shape output. Appends a throwaway rule demanding an exact
# marker line, runs a real --dry-run (network calls to Gemini happen, but
# --dry-run prints instead of sending — see run.py), then asserts the
# marker appears in the generated output. newsletter_style.md is always
# restored byte-identical via a trap, even on failure/interrupt.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STYLE_FILE="newsletter_style.md"
BACKUP="$(mktemp)"
MARKER="STYLE-MARKER-42"

cp "$STYLE_FILE" "$BACKUP"
trap 'cp "$BACKUP" "$STYLE_FILE"; rm -f "$BACKUP"' EXIT

echo "" >> "$STYLE_FILE"
echo "## AC7 harness rule (temporary)" >> "$STYLE_FILE"
echo "End the newsletter with the exact line ${MARKER}" >> "$STYLE_FILE"

OUTPUT="$(.venv/bin/python run.py --dry-run 2>&1)"
RC=$?

if [ $RC -ne 0 ]; then
    echo "FAIL: run.py --dry-run exited $RC" >&2
    echo "$OUTPUT" >&2
    exit 1
fi

# generate() always archives the full markdown (regardless of --dry-run)
# and logs its path — check that file, not the dry-run stdout print, which
# truncates each HTML part to 1000 chars and could cut the marker off.
ARCHIVE_PATH="$(echo "$OUTPUT" | sed -n 's/.*Archived to \(.*\)$/\1/p' | tail -1)"

if [ -z "$ARCHIVE_PATH" ] || [ ! -f "$ARCHIVE_PATH" ]; then
    echo "FAIL: could not find archived markdown path in run.py output." >&2
    echo "$OUTPUT" >&2
    exit 1
fi

if grep -qF "$MARKER" "$ARCHIVE_PATH"; then
    echo "PASS: ${MARKER} found in ${ARCHIVE_PATH} — newsletter_style.md rules reach the prompt."
    exit 0
else
    echo "FAIL: ${MARKER} not found in ${ARCHIVE_PATH}." >&2
    exit 1
fi
