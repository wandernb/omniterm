#!/bin/zsh
# Prints the SHA-1 of a stable, self-signed code-signing identity for local
# builds, creating it on first use, and the keychain that holds it.
#
# Ad-hoc signatures change with every build, so macOS treats each build's
# Inbox widget as a new extension and drops the one placed in Notification
# Center. A fixed certificate gives every build the same designated
# requirement. The identity lives in its own keychain under ~/.omni/signing,
# never the login keychain; its password only guards this throwaway key.
set -euo pipefail

dir="$HOME/.omni/signing"
keychain="$dir/omniterm-local.keychain-db"
password_file="$dir/keychain-password"
name="OmniTerm Local Signing"

mkdir -p "$dir"
chmod 700 "$dir"

if [[ ! -f "$keychain" ]]; then
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    umask 077
    openssl rand -hex 24 > "$password_file"
    password="$(<"$password_file")"
    cat > "$work/cert.conf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $name
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$work/key.pem" -out "$work/cert.pem" -config "$work/cert.conf" 2>/dev/null
    openssl pkcs12 -export -legacy -inkey "$work/key.pem" -in "$work/cert.pem" \
        -name "$name" -out "$work/identity.p12" -passout "pass:$password" 2>/dev/null \
      || openssl pkcs12 -export -inkey "$work/key.pem" -in "$work/cert.pem" \
        -name "$name" -out "$work/identity.p12" -passout "pass:$password"
    security create-keychain -p "$password" "$keychain"
    security set-keychain-settings "$keychain"
    security import "$work/identity.p12" -k "$keychain" -P "$password" -T /usr/bin/codesign >/dev/null
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$password" "$keychain" >/dev/null
fi

security unlock-keychain -p "$(<"$password_file")" "$keychain"
hash="$(security find-certificate -c "$name" -Z "$keychain" | awk '/SHA-1 hash:/ {print $3; exit}')"
[[ -n "$hash" ]] || { print -u2 "No $name certificate in $keychain"; exit 1; }
print "$hash $keychain"
