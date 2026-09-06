#!/usr/bin/env bash
# rlm-tools-bsl -- quick install as a systemd --user service
#
# Prerequisites:
#   Python 3.10+  https://python.org
#   uv            https://docs.astral.sh/uv/
#
# Optional LLM env vars (for llm_query helper):
#   Create .env next to this script, or set environment variables:
#     RLM_LLM_BASE_URL, RLM_LLM_API_KEY, RLM_LLM_MODEL  (OpenAI-compatible)
#     ANTHROPIC_API_KEY                                    (Anthropic API)
#   Without LLM keys all core features still work (find_module, grep, xml parsing).
#
# Usage:
#   ./simple-install.sh                        # keep current settings, auto-detect .env
#   ./simple-install.sh /path/to/.env          # explicit .env path
#   RLM_HOST=0.0.0.0 ./simple-install.sh       # custom bind host
#   RLM_PORT=3000 ./simple-install.sh          # custom port
#   RLM_NO_ENV=1 ./simple-install.sh           # start the service without any .env file
#   UV_NATIVE_TLS=true ./simple-install.sh     # corporate proxy with TLS replacement
#
# Re-running the script upgrades in place: host, port and .env path of an existing
# installation are KEPT unless RLM_HOST / RLM_PORT / an .env argument say otherwise.

set -euo pipefail

BIND_HOST="${RLM_HOST:-}"
PORT="${RLM_PORT:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Read the settings of an existing installation ---
# Explicit user values and saved values are passed to `service install`; an omitted
# value is left for the server to resolve. Passing the saved values also protects an
# upgrade that unexpectedly resolves an older package without server-side memory.
PY_BIN=""
if command -v python3 &>/dev/null; then
    PY_BIN="python3"
elif command -v python &>/dev/null && python -c 'import sys; sys.exit(sys.version_info[0] < 3)' 2>/dev/null; then
    # Only if `python` is Python 3: under Python 2 the reader below would fail on
    # open(encoding=) and quietly report "nothing saved".
    PY_BIN="python"
fi

# Checked HERE, before anything reads the config: further down "cannot be parsed" has to
# mean the file really is broken, not that there was no Python 3 to check it with.
if [ -z "$PY_BIN" ]; then
    echo "ERROR: Python 3 not found. Install Python 3.10+ from https://python.org"
    exit 1
fi
if [ "$#" -gt 0 ] && [ -z "$(printf '%s' "$1" | tr -d '[:space:]')" ]; then
    echo "ERROR: empty .env path; omit the argument to preserve settings or set RLM_NO_ENV=1."
    exit 1
fi

discover_installed_config() {
    # A custom RLM_CONFIG_FILE is pinned in the unit so the service can find it after
    # systemd starts it from a different environment. A later installer run may come
    # from a fresh shell where that variable is no longer exported; recover the active
    # path before looking for service.json. Only parse the exact unit syntax emitted by
    # _service_linux.py. If the unit mentions the setting in another syntax, fail safe
    # instead of guessing a different config and resetting the service.
    local unit="$HOME/.config/systemd/user/rlm-tools-bsl.service"
    if [ ! -f "$unit" ]; then
        printf '.'
        return 0
    fi
    "$PY_BIN" -c 'import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
except (OSError, ValueError):
    raise SystemExit(2)
match = re.search(r"^ExecStart=/usr/bin/env \"RLM_CONFIG_FILE=((?:\\.|[^\"])*)\"", text, re.MULTILINE)
if match is None:
    if "RLM_CONFIG_FILE=" in text:
        raise SystemExit(2)
    legacy = re.search(
        r"^ExecStart=(?!/usr/bin/env ).+ --transport streamable-http --host \S+ --port \S+\s*$",
        text,
        re.MULTILINE,
    )
    raise SystemExit(3 if legacy else 0)
raw = match.group(1)
out = []
i = 0
escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "\"": "\""}
while i < len(raw):
    if raw[i] == "\\" and i + 1 < len(raw):
        out.append(escapes.get(raw[i + 1], raw[i + 1]))
        i += 2
    else:
        out.append(raw[i])
        i += 1
value = "".join(out).replace("%%", "%").replace("$$", "$")
sys.stdout.write(value)' "$unit"
    local status=$?
    if [ "$status" -eq 0 ]; then
        # The sentinel keeps command substitution from stripping trailing newlines from
        # a legal POSIX path. The caller removes exactly this appended character.
        printf '.'
    fi
    return "$status"
}

legacy_unit_matches_config() {
    "$PY_BIN" -c 'import json, re, sys
try:
    unit = open(sys.argv[1], encoding="utf-8").read()
    config = json.load(open(sys.argv[2], encoding="utf-8"))
    command = re.search(
        r"^ExecStart=.+ --transport streamable-http --host (\S+) --port (\S+)\s*$",
        unit,
        re.MULTILINE,
    )
    env_line = re.search(r"^EnvironmentFile=-(.*)$", unit, re.MULTILINE)
    saved_port = config.get("port") if isinstance(config, dict) else None
    port_matches = not isinstance(saved_port, bool) and int(saved_port) == int(command.group(2))
    matches = (
        command is not None
        and isinstance(config.get("host"), str)
        and config["host"] == command.group(1)
        and port_matches
        and config.get("env_file") == (env_line.group(1) if env_line else None)
    )
except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
    matches = False
raise SystemExit(0 if matches else 1)' "$HOME/.config/systemd/user/rlm-tools-bsl.service" "$1"
}

LEGACY_UNIT_WITHOUT_CONFIG="no"
if [ -n "${RLM_CONFIG_FILE:-}" ]; then
    CONFIG_FILE="$RLM_CONFIG_FILE"
else
    if DISCOVERED_CONFIG_RAW="$(discover_installed_config)"; then
        :
    else
        DISCOVERY_STATUS=$?
        if [ "$DISCOVERY_STATUS" -eq 3 ]; then
            LEGACY_UNIT_WITHOUT_CONFIG="yes"
            DISCOVERED_CONFIG_RAW="."
        else
            echo "ERROR: the existing systemd unit contains an unreadable RLM_CONFIG_FILE setting."
            echo "       Set RLM_CONFIG_FILE explicitly before running the installer."
            exit 1
        fi
    fi
    DISCOVERED_CONFIG="${DISCOVERED_CONFIG_RAW%.}"
    CONFIG_FILE="${DISCOVERED_CONFIG:-$HOME/.config/rlm-tools-bsl/service.json}"
    if [ -n "$DISCOVERED_CONFIG" ]; then
        export RLM_CONFIG_FILE="$CONFIG_FILE"
    fi
fi

CONFIG_BACKUP_FILE="$CONFIG_FILE.rlm-backup"
CONFIG_RESCUE_FILE="$CONFIG_FILE.rlm-unreadable"
CONFIG_LINK_FILE="$CONFIG_FILE.rlm-linktarget"
CONFIG_LINK_TARGET=""
# Staging files are named per PID, so this only ever removes our own. EXIT covers
# Ctrl+C and every error exit too.
trap 'rm -f "$CONFIG_FILE.partial.$$" "$CONFIG_BACKUP_FILE.partial.$$" "$CONFIG_RESCUE_FILE.partial.$$" "$CONFIG_LINK_FILE.partial.$$" "${CONFIG_LINK_TARGET:-/nonexistent}.partial.$$"' EXIT
INSTALL_ARGS=(service install)

read_saved() {
    # read_saved <key> [file]
    #
    # Prints NOTHING unless the value has the type that key is supposed to hold. Printing
    # whatever json.load returned turned `"host": ["bad host"]` into the string
    # "['bad host']" -- non-empty, so the preflight below waved the upgrade through and
    # the service was unregistered before the server ever saw the value. The port is
    # normalised the same way the server normalises it (an integral float and a padded
    # string are both accepted), so the scripts cannot reject a config the server takes.
    local file="${2:-$CONFIG_FILE}"
    if [ -z "$PY_BIN" ] || [ ! -f "$file" ]; then
        return 0
    fi
    "$PY_BIN" -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)
if not isinstance(data, dict):
    raise SystemExit(0)
key = sys.argv[2]
value = data.get(key)
if value is None or isinstance(value, bool):
    raise SystemExit(0)
if key == "port":
    if isinstance(value, float) and not value.is_integer():
        raise SystemExit(0)
    try:
        print(int(value.strip()) if isinstance(value, str) else int(value))
    except (TypeError, ValueError):
        raise SystemExit(0)
elif isinstance(value, str):
    sys.stdout.write(value)' "$file" "$1" 2>/dev/null || true
}

config_is_readable() {
    # Parses as a JSON object -- says nothing about what is IN it.
    if [ -z "$PY_BIN" ] || [ ! -f "$1" ]; then
        return 1
    fi
    "$PY_BIN" -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if isinstance(data, dict) else 1)' "$1" 2>/dev/null
}

config_is_valid() {
    # USABLE, not merely well-formed: `{}` and `"port": "oops"` are valid JSON objects
    # the server rightly refuses. Judging them "valid" here is what let a semantically
    # broken file be copied over the last good backup.
    if [ -z "$PY_BIN" ] || [ ! -f "$1" ]; then
        return 1
    fi
    "$PY_BIN" -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(1)
if not isinstance(data, dict):
    raise SystemExit(1)
host = data.get("host")
if not isinstance(host, str) or not host.strip():
    raise SystemExit(1)
port = data.get("port")
if isinstance(port, bool):
    raise SystemExit(1)
if isinstance(port, float) and not port.is_integer():
    raise SystemExit(1)
try:
    port = int(port)
except (TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if 1 <= port <= 65535 else 1)' "$1" 2>/dev/null
}

assert_config_destination_writable() {
    # save_config() stages next to service.json and atomically renames the sibling. A
    # missing config does not exercise the backup path, so prove this before stopping a
    # working service that may still get its host/port directly from the systemd unit.
    local config_dir probe
    config_dir="$(dirname "$CONFIG_FILE")"
    probe="$CONFIG_FILE.partial.$$"
    if ! mkdir -p -- "$config_dir" 2>/dev/null; then
        echo "ERROR: cannot prepare the service config directory: $config_dir"
        echo "       Check its permissions and free space; the service is left untouched."
        return 1
    fi
    if ! (set -o noclobber; : > "$probe") 2>/dev/null; then
        echo "ERROR: cannot create an atomic-write staging file next to $CONFIG_FILE"
        echo "       Check the directory permissions and free space; the service is left untouched."
        return 1
    fi
    if ! rm -f -- "$probe" 2>/dev/null; then
        echo "ERROR: cannot remove the atomic-write staging file next to $CONFIG_FILE"
        echo "       Check the directory permissions; the service is left untouched."
        return 1
    fi
}

copy_atomic() {
    # copy_atomic <src> <dst> -- via a private staging file, so a half-written copy is
    # never visible under the real name.
    cp "$1" "$2.partial.$$"
    mv "$2.partial.$$" "$2"
}

is_valid_port() {
    "$PY_BIN" -c 'import sys
try:
    port = int(sys.argv[1])
except (TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if 1 <= port <= 65535 else 1)' "$1"
}

systemd_env_path_is_safe() {
    "$PY_BIN" -c 'import sys
value = sys.argv[1]
trailing_backslashes = len(value) - len(value.rstrip("\\"))
safe = (
    "\n" not in value
    and "\r" not in value
    and value == value.strip()
    and trailing_backslashes % 2 == 0
    and not any(char in value for char in "*?[")
)
raise SystemExit(0 if safe else 1)' "$1"
}

config_path_state() {
    # Exit 0: the path can be inspected; 1: genuinely absent; 2: it may exist but stat
    # failed (permissions/I/O). `[ -f ]` cannot distinguish the latter two and would let
    # an update unregister the service before discovering that the config is inaccessible.
    "$PY_BIN" -c 'import os, sys
try:
    os.lstat(sys.argv[1])
except FileNotFoundError:
    raise SystemExit(1)
except OSError as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0)' "$1"
}

restore_config() {
    # A symlinked config was deleted BY THE LINK by the legacy uninstall. Put the content
    # back into the original target and recreate the link: writing a plain file here
    # would silently detach the service from the shared file it was pointing at.
    if [ -n "$CONFIG_LINK_TARGET" ] && [ ! -e "$CONFIG_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_LINK_TARGET")"
        # The target keeps whatever it holds: it is a real settings file of its own, and
        # somebody may well have updated it while the link was missing. Only a target
        # that does not exist at all is filled from the backup.
        if [ ! -e "$CONFIG_LINK_TARGET" ]; then
            if ! (cp "$CONFIG_BACKUP_FILE" "$CONFIG_LINK_TARGET.partial.$$" \
                && mv "$CONFIG_LINK_TARGET.partial.$$" "$CONFIG_LINK_TARGET"); then
                rm -f "$CONFIG_LINK_TARGET.partial.$$"
                echo "WARN: could not write $CONFIG_LINK_TARGET"
                return 1
            fi
        fi
        if ln -s "$CONFIG_LINK_TARGET" "$CONFIG_FILE" 2>/dev/null; then
            return 0
        fi
        echo "WARN: could not recreate the symlink $CONFIG_FILE -> $CONFIG_LINK_TARGET"
    fi
    # Atomic create-if-absent: fill a private temp file, then hard-link it into place.
    # `ln` FAILS when the target already exists, so a parallel run that has already
    # written fresh settings can never be overwritten by this older copy, and no
    # half-written config can ever be observed by anyone.
    local tmp="$CONFIG_FILE.partial.$$"
    mkdir -p "$(dirname "$CONFIG_FILE")"
    if ! cp "$CONFIG_BACKUP_FILE" "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 1
    fi
    if ln "$tmp" "$CONFIG_FILE" 2>/dev/null; then
        rm -f "$tmp"
        return 0
    fi
    # Filesystems without hard links: still no partial file, just no atomicity.
    if [ ! -f "$CONFIG_FILE" ]; then
        mv "$tmp" "$CONFIG_FILE"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# A run interrupted between `service uninstall` and `service install` used to leave the
# machine with no config at all. The backup is a file next to the config (not in the
# temp dir) precisely so the NEXT run can still find it.
# The link target has to be known BEFORE anything is restored: an earlier run may have
# been interrupted after the legacy uninstall removed the LINK, and by then there is
# nothing left to read it from -- hence the note next to the config.
if [ -L "$CONFIG_FILE" ]; then
    CONFIG_LINK_TARGET="$(readlink -f "$CONFIG_FILE" 2>/dev/null || true)"
    if [ -n "$CONFIG_LINK_TARGET" ]; then
        printf '%s\n' "$CONFIG_LINK_TARGET" > "$CONFIG_LINK_FILE.partial.$$"
        mv "$CONFIG_LINK_FILE.partial.$$" "$CONFIG_LINK_FILE"
    fi
elif [ ! -e "$CONFIG_FILE" ] && [ -f "$CONFIG_LINK_FILE" ]; then
    CONFIG_LINK_TARGET="$(head -n 1 "$CONFIG_LINK_FILE")"
fi

if [ ! -f "$CONFIG_FILE" ] && [ -f "$CONFIG_BACKUP_FILE" ]; then
    if restore_config; then
        echo "Recovered the service settings left behind by an interrupted run."
    fi
fi

CONFIG_EXISTS="no"
if config_path_state "$CONFIG_FILE"; then
    CONFIG_EXISTS="yes"
else
    CONFIG_STATE=$?
    if [ "$CONFIG_STATE" -ne 1 ]; then
        echo "ERROR: cannot inspect $CONFIG_FILE; the service is left untouched."
        exit 1
    fi
fi

if [ "$LEGACY_UNIT_WITHOUT_CONFIG" = "yes" ] && {
    [ "$CONFIG_EXISTS" != "yes" ] || ! legacy_unit_matches_config "$CONFIG_FILE"
}; then
    echo "ERROR: the existing service did not record RLM_CONFIG_FILE (versions up to 1.34.0)."
    echo "       The default config is absent or does not match that unit, so the service is left untouched."
    echo "       Export RLM_CONFIG_FILE with the path used for that installation and retry."
    exit 1
fi

assert_config_destination_writable

SAVED_HOST="$(read_saved host)"
SAVED_PORT="$(read_saved port)"
SAVED_ENV_FILE_RAW="$(read_saved env_file; printf '.')"
SAVED_ENV_FILE="${SAVED_ENV_FILE_RAW%.}"
case "$SAVED_ENV_FILE" in
    ""|/*) ;;
    *) SAVED_ENV_FILE="$(dirname "$CONFIG_FILE")/$SAVED_ENV_FILE" ;;
esac

# Versions up to 1.34.0 DELETED service.json on `service uninstall`, and that uninstall
# is still performed by the OLD binary. The copy is what puts the settings back.
if [ "$CONFIG_EXISTS" = "yes" ]; then
    if config_is_valid "$CONFIG_FILE"; then
        copy_atomic "$CONFIG_FILE" "$CONFIG_BACKUP_FILE"
    else
        # A broken file must NOT be copied over the transient backup: an earlier
        # interrupted run may have left a good one there, and that copy is worth more
        # than the bytes that replaced it. The rescue copy is written ONCE and never
        # refreshed -- it may be the only trace of the original.
        if [ -f "$CONFIG_RESCUE_FILE" ]; then
            echo "WARN: $CONFIG_FILE does not parse - an earlier copy is kept at $CONFIG_RESCUE_FILE"
        else
            copy_atomic "$CONFIG_FILE" "$CONFIG_RESCUE_FILE"
            echo "WARN: $CONFIG_FILE does not parse - copied it as-is to $CONFIG_RESCUE_FILE"
        fi
    fi
fi

# --- Settings for `service install` ---
# The saved values are passed EXPLICITLY rather than left to the server to remember:
# that also survives the case where the freshly installed package turns out to be an
# older one (a lagging PyPI mirror), whose `service install` has no memory at all.
# A host of spaces is not a host: the server strips it and refuses, and it would do so
# only AFTER the service has been unregistered.
if [ -z "$(printf '%s' "$BIND_HOST" | tr -d '[:space:]')" ]; then
    BIND_HOST=""
fi
if [ -z "$(printf '%s' "$SAVED_HOST" | tr -d '[:space:]')" ]; then
    SAVED_HOST=""
fi

EFF_HOST="${BIND_HOST:-$SAVED_HOST}"
EFF_PORT="${PORT:-$SAVED_PORT}"
# A port is only worth passing as an explicit flag if it IS a port: the server rejects an
# explicit nonsense value (rightly -- it would hide a typo), and it would do so after the
# service has already been unregistered.
if [ -n "$EFF_PORT" ] && ! is_valid_port "$EFF_PORT"; then
    if [ -n "$PORT" ]; then
        echo "ERROR: RLM_PORT='$PORT' is not a port number (1-65535)."
        exit 1
    fi
    echo "WARN: saved port '$EFF_PORT' is not a port number - letting the server pick one."
    EFF_PORT=""
fi
if [ -n "$EFF_HOST" ]; then
    INSTALL_ARGS+=(--host "$EFF_HOST")
fi
if [ -n "$EFF_PORT" ]; then
    INSTALL_ARGS+=(--port "$EFF_PORT")
fi

# --- Detect .env ---
# RLM_NO_ENV=1 is the counterpart of `service install --no-env`. It exists because an
# unreadable config leaves the server unable to know the .env path either: it then asks
# for an explicit decision, and without a way to say "none" the script could ask the user
# for something they cannot give.
ENV_DECIDED="no"
ENV_FILE_TO_INSTALL=""
if [ -n "${1:-}" ]; then
    case "$1" in
        /*) ENV_FILE_TO_INSTALL="$1" ;;
        *) ENV_FILE_TO_INSTALL="$PWD/$1" ;;
    esac
    ENV_DECIDED="yes"
    echo "Using .env: $ENV_FILE_TO_INSTALL"
elif [ "${RLM_NO_ENV:-}" = "1" ]; then
    INSTALL_ARGS+=(--no-env)
    ENV_DECIDED="yes"
    echo "RLM_NO_ENV=1 - the service will start without an .env file."
elif [ -n "$SAVED_ENV_FILE" ]; then
    ENV_FILE_TO_INSTALL="$SAVED_ENV_FILE"
    ENV_DECIDED="yes"
    echo "Keeping .env from the service config: $SAVED_ENV_FILE"
elif [ "$CONFIG_EXISTS" = "yes" ] && config_is_readable "$CONFIG_FILE"; then
    # Keep the server's persisted no-env/fallback mode. A legacy env_file:null still
    # allowed user/CWD fallbacks, while a new explicit --no-env is stored separately.
    ENV_DECIDED="yes"
    echo "Existing installation has no explicit .env path - preserving its current mode."
elif [ "$CONFIG_EXISTS" != "yes" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    ENV_FILE_TO_INSTALL="$SCRIPT_DIR/.env"
    ENV_DECIDED="yes"
    echo "Found .env: $SCRIPT_DIR/.env"
elif [ "$CONFIG_EXISTS" != "yes" ]; then
    ENV_DECIDED="yes"
    echo "No .env found next to the installer. No explicit path will be saved;"
    echo "normal user/CWD .env fallbacks remain enabled."
fi

if [ -n "$ENV_FILE_TO_INSTALL" ]; then
    if ! systemd_env_path_is_safe "$ENV_FILE_TO_INSTALL"; then
        echo "ERROR: .env path cannot be represented safely in systemd EnvironmentFile:"
        printf '       %q\n' "$ENV_FILE_TO_INSTALL"
        echo "       Rename the file and retry; the service is left untouched."
        exit 1
    fi
    INSTALL_ARGS+=(--env "$ENV_FILE_TO_INSTALL")
fi

# Nothing destructive has happened yet, and this is the last moment that is true. An
# installation whose settings cannot be read, with nothing given on the command line, has
# exactly one safe outcome: stop and leave the running service alone. Going on would stop
# and unregister it, and only THEN would `service install` refuse -- for exactly the same
# reason, which is why this check asks for precisely what the server will ask for.
if [ "$CONFIG_EXISTS" = "yes" ] && { [ -z "$EFF_HOST" ] || [ -z "$EFF_PORT" ] || [ "$ENV_DECIDED" != "yes" ]; }; then
    echo "ERROR: $CONFIG_FILE exists, but the settings in it could not be read, and they"
    echo "       were not supplied. The service is left untouched."
    echo "       Fix the file, or re-run with all of:"
    echo "         RLM_HOST=<host> RLM_PORT=<port> and either a path to .env as the first"
    echo "         argument or RLM_NO_ENV=1"
    echo "       Or drop the settings altogether: rlm-tools-bsl service uninstall --purge"
    exit 1
fi

# --- Check uv ---
if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found. Install it:"
    echo ""
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    exit 1
fi

# --- Stop & uninstall existing service (best-effort, idempotent for fresh install) ---
# Recheck immediately before the first service command to narrow the unavoidable
# check/use window if permissions changed while prerequisites were prepared.
assert_config_destination_writable

# Prepend uv tool bin to PATH so an existing installation is detected even when
# the shell hasn't been re-sourced since the last `uv tool update-shell`.
UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
if [ -n "$UV_BIN_DIR" ] && [ -d "$UV_BIN_DIR" ]; then
    export PATH="$UV_BIN_DIR:$PATH"
fi

if command -v rlm-tools-bsl &>/dev/null; then
    echo ""
    echo "=== Existing installation detected -- upgrading ==="
    rlm-tools-bsl service stop 2>/dev/null && echo "Service stopped." \
        || echo "Service was not running (OK)."
    rlm-tools-bsl service uninstall 2>/dev/null && echo "Service uninstalled." \
        || echo "Service was not installed (OK)."
fi

# Safety net for orphaned systemd --user units (rlm-tools-bsl binary already
# removed but unit file lingers in ~/.config/systemd/user/).
if command -v systemctl &>/dev/null; then
    systemctl --user disable --now rlm-tools-bsl.service 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

# --- Step 1: Install ---
echo ""
echo "=== Step 1: Install rlm-tools-bsl ==="
UV_EXTRA_ARGS=()
if [ "${UV_NATIVE_TLS:-}" = "true" ]; then
    UV_EXTRA_ARGS+=("--native-tls")
fi

# Force a fresh build (drop any cached wheel from a previous install).
# --upgrade and --refresh keep this path symmetric with simple-install-from-pip.sh:
# without --upgrade an already-installed dependency that still satisfies the
# constraints is left untouched, so a rebuild from source could leave the service
# on an outdated dependency. --refresh also drops uv's cached index metadata and
# the cached local build, which `cache clean` alone does not cover.
uv cache clean rlm-tools-bsl 2>/dev/null || true

if ! uv tool install "${SCRIPT_DIR}[service]" --force --reinstall --upgrade --refresh "${UV_EXTRA_ARGS[@]}"; then
    echo "ERROR: uv tool install failed."
    echo "If you see TLS certificate errors (corporate proxy), retry with:"
    echo "  UV_NATIVE_TLS=true ./simple-install.sh"
    exit 1
fi

# Ensure rlm-tools-bsl is in PATH for this session
if ! command -v rlm-tools-bsl &>/dev/null; then
    echo "Adding uv tool bin directory to PATH..."
    UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
    if [ -n "$UV_BIN_DIR" ] && [ -d "$UV_BIN_DIR" ]; then
        export PATH="$UV_BIN_DIR:$PATH"
    fi
    uv tool update-shell 2>/dev/null || true
fi

# --- Step 2: Register service ---
echo ""
echo "=== Step 2: Register service ==="
if [ -f "$CONFIG_BACKUP_FILE" ] && [ ! -f "$CONFIG_FILE" ]; then
    if restore_config; then
        echo "Restored the service settings that the previous version's uninstall removed."
    fi
fi

rlm-tools-bsl "${INSTALL_ARGS[@]}"

# The transient copy is dropped once the install has put a parseable config in place.
# Any other outcome leaves it for the next run to recover from (`set -e` also aborts
# before this line if the install itself failed). The rescue copy is NOT touched here:
# it outlives the upgrade on purpose.
if config_is_valid "$CONFIG_FILE"; then
    rm -f "$CONFIG_BACKUP_FILE" "$CONFIG_LINK_FILE"
fi
if [ -f "$CONFIG_RESCUE_FILE" ]; then
    echo "NOTE: a copy of the earlier unreadable config is kept at $CONFIG_RESCUE_FILE"
fi


# --- Step 3: Start ---
echo ""
echo "=== Step 3: Start service ==="
rlm-tools-bsl service start

# --- Step 4: Verify ---
echo ""
echo "=== Step 4: Verify ==="
echo "Waiting for server to start (up to ~40s total on slower machines: 4 attempts x 10s)..."

# Read back what `service install` actually saved -- this script deliberately does
# not pass host/port unless asked to, so it cannot assume the defaults.
EFF_HOST="$(read_saved host)"
EFF_PORT="$(read_saved port)"
EFF_HOST="${EFF_HOST:-${BIND_HOST:-127.0.0.1}}"
EFF_PORT="${EFF_PORT:-${PORT:-9000}}"

# A wildcard bind is not a connectable address, and an IPv6 literal has to be bracketed
# or the URL does not parse at all (http://2001:db8::1:3000/health is not a thing).
CHECK_HOST="$EFF_HOST"
CHECK_HOST="${CHECK_HOST#"["}"
CHECK_HOST="${CHECK_HOST%"]"}"
case "$CHECK_HOST" in
    "" | 0.0.0.0 | "*") CHECK_HOST="127.0.0.1" ;;
    "::" | 0:0:0:0:0:0:0:0) CHECK_HOST="[::1]" ;;
    *:*) CHECK_HOST="[$CHECK_HOST]" ;;
esac

# /health is lightweight (no MCP session); /mcp is the real endpoint shown in config.
HEALTH_URL="http://${CHECK_HOST}:${EFF_PORT}/health"
MCP_URL="http://${CHECK_HOST}:${EFF_PORT}/mcp"
echo "Checking $HEALTH_URL ..."

HTTP_CODE="000"
for attempt in 1 2 3 4; do
    sleep 10
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$HEALTH_URL" 2>/dev/null || true)
    if [ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "000" ]; then
        break
    fi
    if [ "$attempt" -lt 4 ]; then
        echo "  Attempt $attempt/4: not ready yet, retrying..."
    fi
done

if [ -n "$HTTP_CODE" ] && [ "$HTTP_CODE" != "000" ]; then
    echo "Server is responding (HTTP $HTTP_CODE). OK."
else
    echo "WARN: Server is not responding at $HEALTH_URL"
    echo "Check status: rlm-tools-bsl service status"
    exit 1
fi

# --- Done ---
echo ""
echo "========================================"
echo " Done! HTTP MCP server is running."
echo "========================================"
echo ""
echo "Version:  $(rlm-tools-bsl --version 2>/dev/null)"
echo "Endpoint: $MCP_URL"
echo "Health:   $HEALTH_URL"
echo ""
echo "Add to .claude.json / mcp.json:"
echo ""
cat <<EOF
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "type": "http",
      "url": "$MCP_URL"
    }
  }
}
EOF
echo ""
echo "To enable autostart without login: loginctl enable-linger \$USER"
echo ""
echo "Service management:"
echo "  rlm-tools-bsl service status"
echo "  rlm-tools-bsl service stop"
echo "  rlm-tools-bsl service start"
echo "  rlm-tools-bsl service uninstall"
