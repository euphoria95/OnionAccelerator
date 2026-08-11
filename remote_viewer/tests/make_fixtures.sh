#!/usr/bin/env bash
# Build the local archive corpus the tests run against.
#
# Two .tar.xz variants matter and must both exist:
#   multiblock  - xz -T4 --block-size, independently decodable blocks, random access
#   singleblock - xz -T1, one block, no random access at all (the pathological case)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HERE/fixtures}"
mkdir -p "$DEST"
cd "$DEST"

if [ -f test.tar ] && [ -f test.tar.gz ] && [ "${FORCE:-0}" != "1" ]; then
    echo "fixtures already present in $DEST (FORCE=1 to rebuild)"
    exit 0
fi

rm -rf payload test.* selfsigned.*
mkdir -p payload/sub/deep

# A few multi-megabyte members so that headers land in only some xz blocks,
# plus small ones, a nested tree, and a symlink.
for i in 1 2 3 4 5 6 7 8; do
    head -c 3000000 /dev/urandom | base64 > "payload/file_$i.txt"
done
echo hello > payload/sub/deep/note.md
printf 'x%.0s' $(seq 1 100) > payload/sub/small.bin
ln -sf ../file_1.txt payload/sub/link.txt

tar cf test.tar payload
xz -T4 --block-size=1MiB -3 -c test.tar > test.multiblock.tar.xz
xz -T1 -3 -c test.tar > test.singleblock.tar.xz

# Recognised but unsupported: gzip has no block index, so listing means the whole file.
gzip -c test.tar > test.tar.gz

if command -v zip >/dev/null 2>&1; then
    zip -q -r -y test.zip payload
else
    7z a -tzip -bso0 -bsp0 test.zip payload >/dev/null
fi
7z a -bso0 -bsp0 test.7z payload >/dev/null

# A deliberately non-solid 7z so extraction cost stays small for one member.
7z a -bso0 -bsp0 -ms=off test.nonsolid.7z payload >/dev/null

# RAR, written in Python: there is no freely installable `rar`, and unrar and 7z only
# ever read the format. The tests check these against unrar rather than trusting them.
python3 "$HERE/rarwriter.py" .

# A self-signed certificate, so the TLS tests can serve what an onion service serves.
# Two files on purpose: the server needs key+cert, a client CA bundle must be cert only.
if command -v openssl >/dev/null 2>&1; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout selfsigned.key -out selfsigned.crt \
        -subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1" >/dev/null 2>&1
    cat selfsigned.key selfsigned.crt > selfsigned.pem
else
    echo "openssl not found — TLS tests will be skipped"
fi

ls -l
echo "fixtures built in $DEST"
