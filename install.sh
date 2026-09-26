#!/usr/bin/env bash
# ==============================================================================
#  _    _                                _          _     _   _ _____ 
# | |  | |                              | |        | |   | | | |_   _|
# | |__| | ___ _ __ _ __ ___   ___  ___ | |  _  _  | |   | | | | | |  
# |  __  |/ _ \ '__| '_ ` _ \ / _ \/ __|| | | || | | |   | | | | | |  
# | |  | |  __/ |  | | | | | |  __/\__ \| | | || | | |___| |_| |_| |_ 
# |_|  |_|\___|_|  |_| |_| |_|\___||___/|_|  \_,_/ |______\___/|_____|
#
#           ✨ Autonomous AI Agent Web Interface & Workspace ✨
# ==============================================================================

set -eo pipefail

# Style definitions (ANSI Truecolor & Styles)
BOLD='\033[1m'
DIM='\033[2m'
ITALIC='\033[3m'
UNDERLINE='\033[4m'

# Colors
C_RESET='\033[0m'
C_CYAN='\033[38;5;51m'
C_SKY='\033[38;5;45m'
C_BLUE='\033[38;5;39m'
C_PURPLE='\033[38;5;141m'
C_GREEN='\033[38;5;48m'
C_YELLOW='\033[38;5;220m'
C_RED='\033[38;5;196m'
C_GRAY='\033[38;5;244m'
C_WHITE='\033[38;5;255m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Cleanup trap for temporary files
TMP_FILES=()
cleanup_on_exit() {
    for f in "${TMP_FILES[@]:-}"; do
        [ -f "$f" ] && rm -f "$f" 2>/dev/null || true
    done
}
trap cleanup_on_exit EXIT

# Pre-defined Total Steps
TOTAL_STEPS=5
CURRENT_STEP=0

print_banner() {
    [ -t 1 ] && clear 2>/dev/null || true
    echo -e "${C_CYAN}"
    cat << "EOF"
    __  __                                  _       __     __    __  ______
   / / / /___   _____ ____ ___   ___   _____| |     / /___ / /_  / / / /  _/
  / /_/ / _ \ / ___// __ `__ \ / _ \ / ___/| | /| / // _ \ / __ \/ / / // /  
 / __  /  __// /   / / / / / //  __/(__  ) | |/ |/ //  __/ /_/ / /_/ // /   
/_/ /_/\___//_/   /_/ /_/ /_/ \___//____/  |__/|__/ \___/_.___/\____/___/   
EOF
    echo -e "${C_PURPLE}${BOLD}   ──────────  Next-Gen Autonomous Agent Web Interface  ──────────${C_RESET}"
    echo -e "${C_GRAY}               One-Click Zero-Config Universal Installer           ${C_RESET}"
    echo ""
}

step_header() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    echo ""
    echo -e "${C_BLUE}${BOLD}┌──[ ${C_CYAN}Step ${CURRENT_STEP}/${TOTAL_STEPS}${C_BLUE} ]───────────────────────────────────────────────────────┐${C_RESET}"
    echo -e "${C_BLUE}${BOLD}│ ${C_WHITE}⚡ $1${C_RESET}"
    echo -e "${C_BLUE}${BOLD}└──────────────────────────────────────────────────────────────────┘${C_RESET}"
}

sub_info() {
    echo -e "  ${C_SKY}➜${C_RESET} ${C_WHITE}$1${C_RESET}"
}

sub_success() {
    echo -e "  ${C_GREEN}${BOLD}✔${C_RESET} ${C_GREEN}$1${C_RESET}"
}

sub_warn() {
    echo -e "  ${C_YELLOW}${BOLD}▲${C_RESET} ${C_YELLOW}$1${C_RESET}"
}

sub_error() {
    echo -e "  ${C_RED}${BOLD}✖${C_RESET} ${C_RED}$1${C_RESET}"
}

run_with_status() {
    local msg=$1
    shift
    local log_file
    log_file="$(mktemp "${TMPDIR:-/tmp}/hermes_install_log.XXXXXX")"
    TMP_FILES+=("$log_file")
    local status=0

    if [ -t 1 ]; then
        local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        local i=0
        "$@" >"$log_file" 2>&1 &
        local pid=$!
        while kill -0 "$pid" 2>/dev/null; do
            i=$(( (i+1) % 10 ))
            printf "\r  \033[38;5;51m%s\033[0m \033[38;5;244m%s...\033[0m" "${spin:$i:1}" "$msg"
            sleep 0.08
        done
        wait "$pid" || status=$?
        printf "\r\033[K"
    else
        echo -e "  ${C_SKY}➜${C_RESET} ${C_GRAY}${msg}...${C_RESET}"
        "$@" >"$log_file" 2>&1 || status=$?
    fi

    if [ $status -ne 0 ]; then
        if [ -s "$log_file" ]; then
            echo -e "  ${C_RED}${BOLD}✖ Step failed:${C_RESET} ${C_WHITE}$msg${C_RESET}"
            sed 's/^/    /' "$log_file" | tail -n 20 >&2
        fi
        rm -f "$log_file"
        return $status
    fi
    rm -f "$log_file"
    return 0
}

# -----------------------------------------------------------------------------
# Step 1: System Environment Analysis
# -----------------------------------------------------------------------------
print_banner

step_header "Analyzing Operating System & Hardware Platform"

OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
case "$OS_NAME" in
    Darwin*)
        OS_DISPLAY="macOS (Apple Silicon / Intel)"
        ;;
    Linux*)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            OS_DISPLAY="Windows Subsystem for Linux (WSL2)"
        elif [ -f /etc/os-release ]; then
            OS_DISPLAY="$(grep -E '^PRETTY_NAME=' /etc/os-release | cut -d= -f2 | tr -d '"')"
        else
            OS_DISPLAY="Linux Generic"
        fi
        ;;
    *)
        OS_DISPLAY="$OS_NAME ($ARCH_NAME)"
        ;;
esac

sub_info "Platform: ${C_WHITE}${BOLD}${OS_DISPLAY}${C_RESET} [${ARCH_NAME}]"
sub_success "System architecture compatible and verified"

# -----------------------------------------------------------------------------
# Step 2: Essential Package Verification (curl, git)
# -----------------------------------------------------------------------------
step_header "Verifying Essential System Tools (Git & Curl)"

install_system_tool() {
    local tool=$1
    sub_warn "Installing missing tool: ${tool}..."
    if command -v brew >/dev/null 2>&1; then
        run_with_status "Installing $tool via Homebrew" brew install "$tool"
    elif command -v apt-get >/dev/null 2>&1; then
        run_with_status "Installing $tool via APT" sudo apt-get install -y "$tool"
    elif command -v dnf >/dev/null 2>&1; then
        run_with_status "Installing $tool via DNF" sudo dnf install -y "$tool"
    elif command -v pacman >/dev/null 2>&1; then
        run_with_status "Installing $tool via Pacman" sudo pacman -S --noconfirm "$tool"
    fi
}

command -v curl >/dev/null 2>&1 || install_system_tool "curl"
sub_success "Network tool 'curl' is ready"

command -v git >/dev/null 2>&1 || install_system_tool "git"
sub_success "Version control tool 'git' is ready"

# -----------------------------------------------------------------------------
# Step 3: Python Environment & Engine
# -----------------------------------------------------------------------------
step_header "Locating & Configuring Python 3.11+ Runtime"

PYTHON_BIN=""

check_py_candidate() {
    local candidate=$1
    if command -v "$candidate" >/dev/null 2>&1; then
        local major minor
        major="$("$candidate" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)"
        minor="$("$candidate" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
        if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
            echo "$candidate"
            return 0
        fi
    fi
    return 1
}

for c in python3.13 python3.12 python3.11 python3 python; do
    if found="$(check_py_candidate "$c")"; then
        PYTHON_BIN="$found"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    sub_warn "Python 3.11+ not found. Installing latest Python automatically..."
    if command -v brew >/dev/null 2>&1; then
        run_with_status "Installing Python 3.12 via Homebrew" brew install python@3.12
        PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
    elif command -v apt-get >/dev/null 2>&1; then
        run_with_status "Installing Python 3 via APT" sudo apt-get install -y python3 python3-pip python3-venv
        PYTHON_BIN="python3"
    elif command -v dnf >/dev/null 2>&1; then
        run_with_status "Installing Python 3 via DNF" sudo dnf install -y python3 python3-pip
        PYTHON_BIN="python3"
    elif command -v pacman >/dev/null 2>&1; then
        run_with_status "Installing Python via Pacman" sudo pacman -S --noconfirm python python-pip
        PYTHON_BIN="python"
    else
        sub_error "Could not automatically install Python 3.11+. Please install it manually."
        exit 1
    fi
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
sub_success "Python Runtime Active: ${C_WHITE}${BOLD}v${PY_VERSION}${C_RESET} (${PYTHON_BIN})"

# -----------------------------------------------------------------------------
# Step 4: Hermes Agent Core Auto-Discovery & Connection
# -----------------------------------------------------------------------------
step_header "Connecting Hermes Agent Core & Memory Bridge"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME" 2>/dev/null || true

find_installed_agent_dir() {
    local candidates=(
        "${HERMES_WEBUI_AGENT_DIR:-}"
        "$HERMES_HOME/hermes-agent"
        "$REPO_ROOT/../hermes-agent"
        "$HOME/.hermes/hermes-agent"
        "$HOME/hermes-agent"
        "/usr/local/lib/hermes-agent"
    )
    for c in "${candidates[@]}"; do
        if [ -n "$c" ] && [ -f "$c/run_agent.py" ]; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

AGENT_DIR="$(find_installed_agent_dir || echo "")"

if [ -n "$AGENT_DIR" ] && [ -f "$AGENT_DIR/run_agent.py" ]; then
    sub_success "Hermes Agent Connected: ${C_WHITE}${AGENT_DIR}${C_RESET}"
    AGENT_STATUS_STR="${C_GREEN}Active & Linked${C_RESET}"
else
    sub_info "Hermes Agent runtime discovery will be managed by bootstrap engine"
    AGENT_STATUS_STR="${C_SKY}Runtime Discovery${C_RESET}"
fi

# -----------------------------------------------------------------------------
# Step 5: Virtualenv & Package Dependencies
# -----------------------------------------------------------------------------
step_header "Building Virtual Environment & Installing Dependencies"

VENV_DIR="$REPO_ROOT/.venv"
if [ ! -f "$VENV_DIR/bin/python" ] && [ ! -f "$VENV_DIR/Scripts/python.exe" ]; then
    rm -rf "$VENV_DIR" 2>/dev/null || true
    sub_info "Creating isolated virtual environment (.venv)..."
    "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/dev/null || "$PYTHON_BIN" -m venv --symlinks "$VENV_DIR" 2>/dev/null || true
fi

if [ -f "$VENV_DIR/bin/python" ]; then
    VENV_PYTHON="$VENV_DIR/bin/python"
elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
    VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
else
    VENV_PYTHON="$PYTHON_BIN"
fi

# Clean environment execution for pip
run_with_status "Upgrading pip & core build tools" env -u PYTHONPATH "$VENV_PYTHON" -m pip install --quiet --upgrade pip setuptools wheel

if [ -f "$REPO_ROOT/requirements.txt" ]; then
    run_with_status "Installing WebUI dependencies" env -u PYTHONPATH "$VENV_PYTHON" -m pip install --quiet -r "$REPO_ROOT/requirements.txt"
fi

# Optional companion parsers (warning on failure, non-fatal)
run_with_status "Installing optional companion parsers" env -u PYTHONPATH "$VENV_PYTHON" -m pip install --quiet psutil edge-tts python-docx openpyxl python-pptx || true

sub_success "All packages and UI dependencies verified & ready"

# -----------------------------------------------------------------------------
# Launch Summary & Execution
# -----------------------------------------------------------------------------
echo ""
echo -e "${C_GREEN}${BOLD}╔══════════════════════════════════════════════════════════════════╗${C_RESET}"
echo -e "${C_GREEN}${BOLD}║   ✨  Hermes WebUI Installation Finished Successfully!  ✨      ║${C_RESET}"
echo -e "${C_GREEN}${BOLD}╚══════════════════════════════════════════════════════════════════╝${C_RESET}"
echo ""
echo -e "  ${C_SKY}${BOLD}● Web Interface :${C_RESET} ${C_WHITE}http://127.0.0.1:8787${C_RESET}"
echo -e "  ${C_SKY}${BOLD}● Agent Status  :${C_RESET} $AGENT_STATUS_STR"
echo -e "  ${C_SKY}${BOLD}● State Directory:${C_RESET} ${C_GRAY}$HERMES_HOME${C_RESET}"
echo ""
echo -e "${C_PURPLE}${BOLD}Starting server and opening your browser...${C_RESET}"
echo ""

if [ -n "$AGENT_DIR" ]; then
    export HERMES_WEBUI_AGENT_DIR="$AGENT_DIR"
    export PYTHONPATH="$AGENT_DIR:${PYTHONPATH:-}"
fi

exec "$VENV_PYTHON" "$REPO_ROOT/bootstrap.py" "$@"
