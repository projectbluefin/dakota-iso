#!/usr/bin/env bash
# scripts/e2e-exit.sh
# Exit codes and the wall-clock deadline shared by the `plain-test-qemu` and
# `luks-test-qemu` E2E harnesses (justfile). Sourced, never run.
#
# Exit codes (docs/ci.md, "E2E exit codes and wall-clock deadline"):
#   0       passed
#   1       usage error, or a failure before the first phase
#   11      phase 1: boot the live ISO
#   12      phase 2: run the installer (fisherman)
#   13      phase 3: boot the installed disk
#   14      phase 4: unlock LUKS (LUKS lane only)
#   15      phase 5: verify the installed system
#   75      E2E_WALL_CLOCK_DEADLINE reached (EX_TEMPFAIL); evidence was kept
#   77      a host dependency (QEMU, OVMF, socat) is missing
#
# A harness sets E2E_PHASE_EXIT at each phase. Its EXIT trap reports
# `e2e_status`: E2E_EXIT_CODE when a failure named its own code, otherwise the
# phase's code for any failure -- an explicit failure and a bare command
# tripping `set -e` in the same phase are the same class.

E2E_EXIT_DEADLINE=75
E2E_EXIT_DEPENDENCY=77
E2E_PHASE_EXIT=1
E2E_EXIT_CODE=""

# e2e_status STATUS: the exit status to report for a harness exiting with STATUS.
e2e_status() {
    if [[ -n "${E2E_EXIT_CODE}" ]]; then
        echo "${E2E_EXIT_CODE}"
    elif (( $1 == 0 )); then
        echo 0
    else
        echo "${E2E_PHASE_EXIT}"
    fi
}

# e2e_phase_name CODE: human-readable name of an exit code, for job summaries.
e2e_phase_name() {
    case "$1" in
        0)  echo "passed" ;;
        11) echo "live ISO boot" ;;
        12) echo "install" ;;
        13) echo "installed-disk boot" ;;
        14) echo "LUKS unlock" ;;
        15) echo "post-install verification" ;;
        75) echo "wall-clock deadline reached" ;;
        77) echo "missing host dependency" ;;
        "") echo "not run" ;;
        *)  echo "failure before the first phase" ;;
    esac
}

# e2e_exit CODE MESSAGE: fail with an explicit code.
e2e_exit() {
    E2E_EXIT_CODE="$1"
    shift
    echo "FAIL: $*" >&2
    exit "${E2E_EXIT_CODE}"
}

# e2e_require CMD...: exit 77 when a host command is missing.
e2e_require() {
    local cmd
    for cmd in "$@"; do
        command -v "${cmd}" >/dev/null 2>&1 \
            || e2e_exit "${E2E_EXIT_DEPENDENCY}" "host dependency '${cmd}' is not installed"
    done
}

# e2e_deadline_start HANDLER
#
# When E2E_WALL_CLOCK_DEADLINE (epoch seconds) is set, run HANDLER in this
# shell once it passes. HANDLER records what it can and calls
# `e2e_exit "${E2E_EXIT_DEADLINE}" ...`, so the harness fails with its
# evidence while the CI job still has time to upload it, rather than being
# killed by timeout-minutes with nothing kept.
#
# Bash runs a signal trap only after the foreground command returns, so the
# watcher signals first and, if the harness is still running after
# E2E_DEADLINE_GRACE seconds (default 30), terminates the harness's child
# processes to unblock it.
e2e_deadline_start() {
    local handler="$1" deadline="${E2E_WALL_CLOCK_DEADLINE:-}"
    [[ -n "${deadline}" ]] || return 0
    [[ "${deadline}" =~ ^[0-9]+$ ]] \
        || e2e_exit 1 "E2E_WALL_CLOCK_DEADLINE must be epoch seconds, got '${deadline}'"
    (( EPOCHSECONDS < deadline )) \
        || e2e_exit "${E2E_EXIT_DEADLINE}" "the wall-clock deadline passed before the harness started"
    echo "Wall-clock deadline: $(date -u -d "@${deadline}" '+%Y-%m-%d %H:%M:%S UTC') ($(( (deadline - EPOCHSECONDS) / 60 )) min from now)"
    # shellcheck disable=SC2064 # expand the handler name now
    trap "${handler}" USR1
    local main=$$ grace="${E2E_DEADLINE_GRACE:-30}"
    (
        trap - USR1
        while kill -0 "${main}" 2>/dev/null; do
            left=$(( deadline - EPOCHSECONDS ))
            if (( left <= 0 )); then
                kill -USR1 "${main}" 2>/dev/null || exit 0
                sleep "${grace}"
                kill -0 "${main}" 2>/dev/null && pkill -TERM -P "${main}" 2>/dev/null
                exit 0
            fi
            sleep $(( left < 5 ? left : 5 ))
        done
    ) </dev/null >/dev/null 2>&1 &
}
