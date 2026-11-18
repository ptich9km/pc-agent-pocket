#!/usr/bin/env bash
# Build distributable Linux installation packages from this folder.
#
# Produces in dist/:
#   pc-agent-pocket-<version>-linux.tar.gz  archive; unpack and run setup.sh
#   pc-agent-pocket-<version>-linux.run     single-file self-extracting installer
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
NAME="pc-agent-pocket"
VERSION="1.0.0"
OUT="dist"

for file in bridge.py setup.sh run.sh README.md; do
    [[ -f $file ]] || { echo "ERROR: missing $file" >&2; exit 1; }
done
bash -n setup.sh
bash -n run.sh

mkdir -p "$OUT"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/${NAME}-build.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

mkdir "$STAGE/$NAME"
cp -p bridge.py setup.sh run.sh README.md "$STAGE/$NAME/"
chmod +x "$STAGE/$NAME/setup.sh" "$STAGE/$NAME/run.sh"

ARCHIVE="$OUT/$NAME-$VERSION-linux.tar.gz"
tar --owner=0 --group=0 --numeric-owner -czf "$ARCHIVE" -C "$STAGE" "$NAME"
echo "built: $ARCHIVE"

# Self-extracting installer: shell header + the same tar.gz as payload.
RUN="$OUT/$NAME-$VERSION-linux.run"
cat > "$RUN" <<'HEADER'
#!/usr/bin/env bash
# Self-extracting Linux installer for pc-agent-pocket.
# Usage:
#   ./pc-agent-pocket-VERSION-linux.run [setup.sh options]   install
#   ./pc-agent-pocket-VERSION-linux.run --check              verify only
#   ./pc-agent-pocket-VERSION-linux.run --uninstall          remove
#   ./pc-agent-pocket-VERSION-linux.run --extract-only [dir] just unpack
set -euo pipefail

SELF="$0"
MARKER="__PAYLOAD_BELOW__"
LINE=$(awk -v m="$MARKER" '$0==m {print NR+1; exit}' "$SELF")
[[ -n $LINE ]] || { echo "ERROR: corrupt installer, payload marker missing" >&2; exit 1; }

if [[ ${1:-} == "--extract-only" ]]; then
    DEST=${2:-.}
    mkdir -p "$DEST"
    tail -n +"$LINE" "$SELF" | tar -xz -C "$DEST"
    echo "extracted to $DEST/pc-agent-pocket"
    exit 0
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/pc-agent-pocket.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
tail -n +"$LINE" "$SELF" | tar -xz -C "$TMP"
bash "$TMP/pc-agent-pocket/setup.sh" "$@"
exit $?
HEADER
printf '%s\n' "__PAYLOAD_BELOW__" >> "$RUN"
cat "$ARCHIVE" >> "$RUN"
chmod +x "$RUN"
echo "built: $RUN"
