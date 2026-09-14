#!/usr/bin/env bash
# scripts/variant-config.sh — canonical reader and validator for variant configuration.
#
# Declares the schema of variant configuration across:
#   - Top-level variant directory: <target>/ (or resolved <variant>/)
#   - Live container build context: live/src/<variant>/ (or in-container /tmp/src/<variant>/)
#
# Can be executed directly:
#   scripts/variant-config.sh <variant-or-target> <key>
#   scripts/variant-config.sh --validate <variant-or-target>
#   scripts/variant-config.sh --validate-all
# Or sourced into bash scripts:
#   source scripts/variant-config.sh
#   val=$(get_variant_config "dakota" "composefs")

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

resolve_variant_name() {
    local target="$1"
    local target_dir="${TARGET_DIR:-${REPO_ROOT}/${target}}"
    local raw_target="$target"
    if [[ -f "${target_dir}/live_target" ]]; then
        raw_target=$(cat "${target_dir}/live_target" | tr -d '[:space:]')
    elif [[ -f "${REPO_ROOT}/${target}/live_target" ]]; then
        raw_target=$(cat "${REPO_ROOT}/${target}/live_target" | tr -d '[:space:]')
    fi
    local variant
    variant=$(echo "$raw_target" | sed 's/-nvidia-open$//;s/-nvidia$//')
    if [[ -d "${TARGET_DIR:-${REPO_ROOT}/${variant}}" || -d "${REPO_ROOT}/live/src/${variant}" ]]; then
        echo "$variant"
    else
        echo "$target"
    fi
}

# Get a variant configuration value
# Usage: get_variant_config <target-or-variant> <key>
get_variant_config() {
    local target="$1"
    local key="$2"
    local variant
    variant=$(resolve_variant_name "$target")

    case "$key" in
        payload_ref)
            if [[ -n "${TARGET_DIR:-}" && -f "${TARGET_DIR}/payload_ref" ]]; then
                cat "${TARGET_DIR}/payload_ref" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${target}/payload_ref" ]]; then
                cat "${REPO_ROOT}/${target}/payload_ref" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${variant}/payload_ref" ]]; then
                cat "${REPO_ROOT}/${variant}/payload_ref" | tr -d '[:space:]'
            else
                echo "ERROR: ${target}/payload_ref not found — create it with base image reference" >&2
                return 1
            fi
            ;;
        live_target)
            if [[ -n "${TARGET_DIR:-}" && -f "${TARGET_DIR}/live_target" ]]; then
                cat "${TARGET_DIR}/live_target" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${target}/live_target" ]]; then
                cat "${REPO_ROOT}/${target}/live_target" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${variant}/live_target" ]]; then
                cat "${REPO_ROOT}/${variant}/live_target" | tr -d '[:space:]'
            else
                echo "${target}"
            fi
            ;;
        live_title)
            if [[ -n "${TARGET_DIR:-}" && -f "${TARGET_DIR}/live_title" ]]; then
                cat "${TARGET_DIR}/live_title" | tr -d '\r\n'
            elif [[ -f "${REPO_ROOT}/${target}/live_title" ]]; then
                cat "${REPO_ROOT}/${target}/live_title" | tr -d '\r\n'
            elif [[ -f "${REPO_ROOT}/${variant}/live_title" ]]; then
                cat "${REPO_ROOT}/${variant}/live_title" | tr -d '\r\n'
            else
                echo "Dakota Live"
            fi
            ;;
        registry)
            if [[ -n "${TARGET_DIR:-}" && -f "${TARGET_DIR}/registry" ]]; then
                cat "${TARGET_DIR}/registry" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${target}/registry" ]]; then
                cat "${REPO_ROOT}/${target}/registry" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${variant}/registry" ]]; then
                cat "${REPO_ROOT}/${variant}/registry" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/live/src/${variant}/registry" ]]; then
                cat "${REPO_ROOT}/live/src/${variant}/registry" | tr -d '[:space:]'
            else
                echo "projectbluefin"
            fi
            ;;
        tag)
            if [[ -n "${TARGET_DIR:-}" && -f "${TARGET_DIR}/tag" ]]; then
                cat "${TARGET_DIR}/tag" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${target}/tag" ]]; then
                cat "${REPO_ROOT}/${target}/tag" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${variant}/tag" ]]; then
                cat "${REPO_ROOT}/${variant}/tag" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/live/src/${variant}/tag" ]]; then
                cat "${REPO_ROOT}/live/src/${variant}/tag" | tr -d '[:space:]'
            else
                echo "stable"
            fi
            ;;
        composefs)
            if [[ -f "${REPO_ROOT}/live/src/${variant}/composefs" ]]; then
                cat "${REPO_ROOT}/live/src/${variant}/composefs" | tr -d '[:space:]'
            elif [[ -f "${REPO_ROOT}/${variant}/composefs" ]]; then
                cat "${REPO_ROOT}/${variant}/composefs" | tr -d '[:space:]'
            elif [[ "$variant" == "dakota" ]]; then
                echo "true"
            else
                echo "ERROR: composefs config missing for variant '${variant}' (live/src/${variant}/composefs)" >&2
                return 1
            fi
            ;;
        bootloader)
            local raw=""
            if [[ -f "${REPO_ROOT}/live/src/${variant}/bootloader" ]]; then
                raw=$(cat "${REPO_ROOT}/live/src/${variant}/bootloader" | tr -d '[:space:]')
            elif [[ -f "${REPO_ROOT}/${variant}/bootloader" ]]; then
                raw=$(cat "${REPO_ROOT}/${variant}/bootloader" | tr -d '[:space:]')
            elif [[ "$variant" == "dakota" ]]; then
                raw="systemd"
            else
                echo "ERROR: bootloader config missing for variant '${variant}' (live/src/${variant}/bootloader)" >&2
                return 1
            fi
            if [[ "$raw" == "grub" ]]; then
                echo "grub2"
            else
                echo "$raw"
            fi
            ;;
        base_imgref)
            if [[ -f "${REPO_ROOT}/live/src/${variant}/base_imgref" ]]; then
                cat "${REPO_ROOT}/live/src/${variant}/base_imgref" | tr -d '[:space:]'
            elif [[ "$variant" == "dakota" ]]; then
                echo "ghcr.io/projectbluefin/dakota:stable"
            else
                echo "ERROR: base_imgref missing for variant '${variant}'" >&2
                return 1
            fi
            ;;
        nvidia_imgref)
            if [[ -f "${REPO_ROOT}/live/src/${variant}/nvidia_imgref" ]]; then
                cat "${REPO_ROOT}/live/src/${variant}/nvidia_imgref" | tr -d '[:space:]'
            elif [[ "$variant" == "dakota" ]]; then
                echo "ghcr.io/projectbluefin/dakota-nvidia:stable"
            else
                echo "ERROR: nvidia_imgref missing for variant '${variant}'" >&2
                return 1
            fi
            ;;
        flatpak_var_path)
            if [[ -f "${REPO_ROOT}/live/src/${variant}/flatpak_var_path" ]]; then
                cat "${REPO_ROOT}/live/src/${variant}/flatpak_var_path" | tr -d '[:space:]'
            elif [[ "$variant" == "dakota" ]]; then
                echo "state/os/default/var"
            else
                echo "var/lib/flatpak"
            fi
            ;;
        *)
            echo "ERROR: Unknown variant configuration key '${key}'" >&2
            return 1
            ;;
    esac
}

validate_variant() {
    local target="$1"
    local variant
    variant=$(resolve_variant_name "$target")

    echo "Validating variant: target='${target}' -> variant='${variant}'"
    get_variant_config "$target" payload_ref >/dev/null
    get_variant_config "$target" live_target >/dev/null
    get_variant_config "$target" live_title >/dev/null
    get_variant_config "$target" registry >/dev/null
    get_variant_config "$target" tag >/dev/null
    get_variant_config "$target" composefs >/dev/null
    get_variant_config "$target" bootloader >/dev/null

    # If registry exists in both namespaces, assert they agree
    if [[ -f "${REPO_ROOT}/${variant}/registry" && -f "${REPO_ROOT}/live/src/${variant}/registry" ]]; then
        local r1 r2
        r1=$(cat "${REPO_ROOT}/${variant}/registry" | tr -d '[:space:]')
        r2=$(cat "${REPO_ROOT}/live/src/${variant}/registry" | tr -d '[:space:]')
        if [[ "$r1" != "$r2" ]]; then
            echo "ERROR: registry mismatch between ${variant}/registry ($r1) and live/src/${variant}/registry ($r2)" >&2
            return 1
        fi
    fi

    # If tag exists in both namespaces, assert they agree
    if [[ -f "${REPO_ROOT}/${variant}/tag" && -f "${REPO_ROOT}/live/src/${variant}/tag" ]]; then
        local t1 t2
        t1=$(cat "${REPO_ROOT}/${variant}/tag" | tr -d '[:space:]')
        t2=$(cat "${REPO_ROOT}/live/src/${variant}/tag" | tr -d '[:space:]')
        if [[ "$t1" != "$t2" ]]; then
            echo "ERROR: tag mismatch between ${variant}/tag ($t1) and live/src/${variant}/tag ($t2)" >&2
            return 1
        fi
    fi

    echo "  OK"
}

# If executed as script (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if [[ $# -eq 0 ]]; then
        echo "Usage: $0 <target-or-variant> <key>" >&2
        echo "       $0 --validate <target-or-variant>" >&2
        echo "       $0 --validate-all" >&2
        exit 1
    fi

    if [[ "$1" == "--validate-all" ]]; then
        for v in dakota bluefin bluefin-lts-hwe lts stable; do
            validate_variant "$v"
        done
    elif [[ "$1" == "--validate" ]]; then
        validate_variant "$2"
    else
        get_variant_config "$1" "$2"
    fi
fi
