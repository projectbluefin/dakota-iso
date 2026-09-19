#!/usr/bin/bash
# variant-config.sh — the single host-side reader for per-variant ISO config.
#
# Source this file; it defines functions only and runs nothing at load time:
#
#     source scripts/variant-config.sh
#
# All paths are resolved relative to the current working directory, which is
# the repository root for every existing caller (justfile recipes and the
# scripts/ entry points).  That matches the behaviour of the inline `cat`
# call sites this file replaces.
#
# ── The two config namespaces ────────────────────────────────────────────────
#
# Namespace A — <variant>/ (top level, host-side only)
#     payload_ref   required by callers; not exposed here (no default to share)
#     live_target   default: the variant name itself
#     tag           default: "stable"
#     registry      default: "projectbluefin"
#
# Namespace B — live/src/<bootloader-variant>/ (also bind-mounted into the
# live container build at /tmp/src/<bootloader-variant>)
#     composefs     default: "true"
#     bootloader    default: "systemd"
#
# The namespace-B key is NOT the variant name.  It is the variant's
# live_target with the "-nvidia" / "-nvidia-open" suffix stripped, because
# a variant may build its live environment from an NVIDIA image while its
# bootloader/composefs config is shared with the non-NVIDIA image.  That
# derivation is what variant_bootloader_variant implements.
#
# ── Why this file exists ─────────────────────────────────────────────────────
#
# Before this file, the namespace-A/B read idiom and the live_target →
# bootloader-variant derivation were copy-pasted across five host-side call
# sites (justfile x3, scripts/iso-sd-boot.sh, scripts/luks-install-qemu.sh,
# scripts/plain-install-qemu.sh, scripts/build-live-squashfs.sh).  Each copy
# carried its own literal default, so the default set was a fact restated in
# five places with nothing reconciling them.  See dakota-iso#136.
#
# Note also that the previous idiom
#
#     $(cat "$f" 2>/dev/null | tr -d '[:space:]' || echo "$default")
#
# only reaches its default because every one of those call sites happened to
# have `set -o pipefail` in scope: without pipefail the pipeline exits 0 via
# `tr` even when `cat` fails, the `||` never runs, and the value is the empty
# string rather than the default.  The correctness of a boot-critical default
# should not depend on a `set` line hundreds of lines away in the caller, so
# _vc_read below tests for the file directly and is pipefail-independent.

# _vc_read <path> <default> — trimmed contents of <path>, or <default> if the
# file is absent or blank.  Pipefail-independent by construction.
_vc_read() {
    local path="$1" default="$2" value
    if [[ -f "${path}" ]]; then
        value="$(tr -d '[:space:]' < "${path}")"
        if [[ -n "${value}" ]]; then
            printf '%s' "${value}"
            return 0
        fi
    fi
    printf '%s' "${default}"
}

# ── Namespace A ──────────────────────────────────────────────────────────────

# live_title is deliberately NOT exposed here: it is free text that may contain
# spaces ("Dakota Live"), so it must not go through the whitespace-stripping
# read above, and it has exactly one call site (scripts/iso-sd-boot.sh) so
# there is nothing to deduplicate.  payload_ref is likewise not exposed: it has
# no default, and its required-ness is enforced differently by each caller.
variant_live_target() { _vc_read "$1/live_target" "$1"; }
variant_tag()         { _vc_read "$1/tag" "stable"; }
variant_registry()    { _vc_read "$1/registry" "projectbluefin"; }

# ── Namespace A → namespace B key derivation ─────────────────────────────────

# variant_bootloader_variant <variant> — the live/src/ directory name that
# holds this variant's bootloader/composefs config.
variant_bootloader_variant() {
    local live_target
    live_target="$(variant_live_target "$1")"
    printf '%s' "${live_target}" | sed 's/-nvidia-open$//;s/-nvidia$//'
}

# ── Namespace B ──────────────────────────────────────────────────────────────

variant_composefs() {
    _vc_read "live/src/$(variant_bootloader_variant "$1")/composefs" "true"
}

variant_bootloader() {
    _vc_read "live/src/$(variant_bootloader_variant "$1")/bootloader" "systemd"
}

# variant_bootloader_recipe <variant> — bootloader name as fisherman's recipe
# validator spells it ("grub" is "grub2" there).  Used by every caller that
# writes a fisherman recipe.
variant_bootloader_recipe() {
    local bootloader
    bootloader="$(variant_bootloader "$1")"
    if [[ "${bootloader}" == "grub" ]]; then
        printf 'grub2'
    else
        printf '%s' "${bootloader}"
    fi
}

# variant_composefs_json <variant> — "true"/"false" suitable for splicing into
# the composeFsBackend field of a fisherman recipe.
variant_composefs_json() {
    if [[ "$(variant_composefs "$1")" == "true" ]]; then
        printf 'true'
    else
        printf 'false'
    fi
}
