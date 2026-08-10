#!/usr/bin/env bash
set -euo pipefail

readonly AZURE_CLI_URI='https://packages.microsoft.com/repos/azure-cli'
readonly MICROSOFT_PROD_URI='https://packages.microsoft.com/ubuntu/24.04/prod'

apt_root=${PLAYWRIGHT_APT_ROOT:-/}
source_dir="$apt_root/etc/apt/sources.list.d"
changed_source_count=0

if [[ "$apt_root" == '/' && $EUID -ne 0 ]]; then
    readonly PRIVILEGE=(sudo)
else
    readonly PRIVILEGE=()
fi

rewrite_list_file() {
    local source_file=$1
    local temporary_file

    temporary_file=$(mktemp)
    awk -v azure_cli_uri="$AZURE_CLI_URI" -v microsoft_prod_uri="$MICROSOFT_PROD_URI" '
        function has_blocked_uri(line, active_line) {
            active_line = line
            sub(/[[:space:]]+#.*/, "", active_line)
            return index(active_line, azure_cli_uri) || index(active_line, microsoft_prod_uri)
        }
        /^[[:space:]]*#/ { print; next }
        has_blocked_uri($0) { next }
        { print }
    ' "$source_file" > "$temporary_file"
    replace_if_changed "$source_file" "$temporary_file"
    rm -f -- "$temporary_file"
}

rewrite_sources_file() {
    local source_file=$1
    local temporary_file

    temporary_file=$(mktemp)
    awk -v azure_cli_uri="$AZURE_CLI_URI" -v microsoft_prod_uri="$MICROSOFT_PROD_URI" '
        function has_blocked_uri(line, active_line) {
            active_line = line
            sub(/[[:space:]]+#.*/, "", active_line)
            return index(active_line, azure_cli_uri) || index(active_line, microsoft_prod_uri)
        }
        function flush_stanza(    i, line, value, in_uris, blocked) {
            in_uris = 0
            blocked = 0
            for (i = 1; i <= stanza_size; i++) {
                line = stanza[i]
                if (line ~ /^[[:space:]]*#/) {
                    continue
                }
                if (line ~ /^[[:space:]]*[Uu][Rr][Ii][Ss][[:space:]]*:/) {
                    in_uris = 1
                    value = line
                    sub(/[[:space:]]+#.*/, "", value)
                    sub(/^[^:]*:[[:space:]]*/, "", value)
                    if (has_blocked_uri(value)) {
                        blocked = 1
                    }
                    continue
                }
                if (in_uris && line ~ /^[[:space:]]+/) {
                    if (has_blocked_uri(line)) {
                        blocked = 1
                    }
                    continue
                }
                in_uris = 0
            }
            for (i = 1; i <= stanza_size; i++) {
                if (!blocked || stanza[i] ~ /^[[:space:]]*#/) {
                    print stanza[i]
                }
            }
            stanza_size = 0
        }

        /^[[:space:]]*$/ {
            if (stanza_size) {
                flush_stanza()
            }
            print
            next
        }

        { stanza[++stanza_size] = $0 }
        END {
            if (stanza_size) {
                flush_stanza()
            }
        }
    ' "$source_file" > "$temporary_file"
    replace_if_changed "$source_file" "$temporary_file"
    rm -f -- "$temporary_file"
}

replace_if_changed() {
    local source_file=$1
    local temporary_file=$2
    local mode

    if cmp -s "$source_file" "$temporary_file"; then
        return
    fi

    mode=$(stat -c '%a' -- "$source_file")
    "${PRIVILEGE[@]}" install -m "$mode" -- "$temporary_file" "$source_file"
    changed_source_count=$((changed_source_count + 1))
    printf 'Removed unavailable Playwright apt entries from %s\n' "$source_file"
}

source_files=()
main_sources_file="$apt_root/etc/apt/sources.list"
if [[ -f "$main_sources_file" ]]; then
    source_files+=("$main_sources_file")
fi
if [[ -d "$source_dir" ]]; then
    shopt -s nullglob
    source_files+=("$source_dir"/*.list "$source_dir"/*.sources)
    shopt -u nullglob
fi

for source_file in "${source_files[@]}"; do
    [[ -f "$source_file" ]] || continue
    case "$source_file" in
        *.list) rewrite_list_file "$source_file" ;;
        *.sources) rewrite_sources_file "$source_file" ;;
    esac
done

if ((changed_source_count == 0)); then
    printf 'No unavailable Playwright apt entries found\n'
fi
