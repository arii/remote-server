#!/bin/bash

set -eu

# =============================================================================
# Kiro CLI Installation Script
# =============================================================================

# Configuration
BINARY_NAME="kiro-cli"
CHAT_BINARY_NAME="kiro-cli-chat"
ZIP_PACKAGE_NAME="kirocli"
CLI_NAME="Kiro CLI"
COMMAND_NAME="kiro-cli"
BASE_URL="https://prod.download.cli.kiro.dev"
CHANNEL="stable"
# An empty VERSION installs the latest release.
VERSION=""
MACOS_APP_NAME="Kiro CLI.app"
MACOS_DMG_FILENAME="Kiro CLI.dmg"
MACOS_DMG_FILENAME_ESCAPED="Kiro%20CLI.dmg"
MACOS_ZIP_FILENAME="${ZIP_PACKAGE_NAME}-universal-darwin.zip"
# The largest package currently needs 1.81 GiB while its archive and extracted files coexist.
MIN_DOWNLOAD_DIR_SPACE_KIB=$((2 * 1024 * 1024))

# Installation directories
MACOS_APP_DIR="/Applications"
USER_INSTALL_DIR="$HOME/.local/bin"
GLOBAL_INSTALL_DIR="/usr/local/bin"
DOWNLOAD_DIR=""  # Will be set using mktemp in main()

# Global variables
use_musl=false
mounted_dmg=""
SUCCESS=false
no_modify_path=false
force=false
installed_standalone=false

# Set color codes if outputting to a terminal
GREEN='' BOLD='' PURPLE='' ORANGE='' NC=''
if [[ -t 1 ]]; then
    GREEN=$(printf '\033[0;32m')
    BOLD=$(printf '\033[1m')
    PURPLE=$(printf '\033[0;95m')
    ORANGE=$(printf '\033[38;5;214m')
    NC=$(printf '\033[0m')
fi

# =============================================================================
# Utility Functions
# =============================================================================

log_info() {
    printf "%s\n" "$1" >&2
}

log() {
    printf "%s✓%s %s\n" "$GREEN" "$NC" "$1" >&2
}

print_header() {
    echo "Kiro CLI installer:" >&2
    echo >&2
}

success() {
    echo >&2
    printf "🎉 %sInstallation complete!%s Happy coding!\n" "$BOLD" "$NC" >&2
    echo >&2
    echo "Next steps:"
    printf "Use the command \"%s%s%s\" to get started!\n" "$PURPLE" "$COMMAND_NAME" "$NC" >&2
    echo >&2
}

success_without_path() {
    echo >&2
    printf "🎉 %sInstallation complete!%s Happy coding!\n" "$BOLD" "$NC" >&2
    echo >&2
    printf "1. %sImportant!%s Before you can continue, you must update your PATH to include:\n" "$ORANGE" "$NC" >&2
    printf "   %s\n" "$HOME/.local/bin" >&2
    echo >&2
    echo "Add it to your PATH by adding this line to your shell configuration file:" >&2
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
    echo >&2
    printf "2. Use the command \"%s%s%s\" to get started!\n" "$PURPLE" "$COMMAND_NAME" "$NC" >&2
    echo >&2
}

error() {
    echo "❌ $1" >&2
    exit 1
}

warning() {
    echo "⚠️ $1" >&2
}

available_space_kib() {
    local path="$1"
    local df_output
    local available_kib

    if ! df_output=$(LC_ALL=C df -Pk "$path" 2>/dev/null); then
        return 1
    fi

    if ! available_kib=$(printf '%s\n' "$df_output" | awk 'NR == 2 { print $4; exit }'); then
        return 1
    fi

    if [[ ! "$available_kib" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    printf '%s\n' "$available_kib"
}

create_download_dir() {
    local required_kib="$1"
    local available_kib

    if [[ -n "${TMPDIR:-}" ]]; then
        if ! available_kib=$(available_space_kib "$TMPDIR") || (( available_kib < required_kib )); then
            return 1
        fi
        if DOWNLOAD_DIR=$(mktemp -d "$TMPDIR/${BINARY_NAME}-install-XXXXXX"); then
            return
        fi
        DOWNLOAD_DIR=""
        return 1
    fi

    if available_kib=$(available_space_kib /tmp) && (( available_kib >= required_kib )) &&
        DOWNLOAD_DIR=$(mktemp -d "/tmp/${BINARY_NAME}-install-XXXXXX"); then
        return
    fi
    DOWNLOAD_DIR=""

    if available_kib=$(available_space_kib "$HOME") && (( available_kib >= required_kib )) &&
        DOWNLOAD_DIR=$(mktemp -d "$HOME/.${BINARY_NAME}-install-XXXXXX"); then
        return
    fi
    DOWNLOAD_DIR=""
    return 1
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --force|-f)
                force=true
                shift
                ;;
            --no-modify-path)
                no_modify_path=true
                shift
                ;;
            --channel)
                [[ $# -ge 2 && -n "$2" ]] || error "--channel needs a release channel, for example --channel stable"
                CHANNEL="$2"
                shift 2
                ;;
            --version)
                local version="${2:-}"
                version="${version#v}"
                [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
                    error "--version needs a semantic version like 3.0.0, not \"${2:-}\""
                VERSION="$version"
                shift 2
                ;;
            *)
                error "Unknown option: $1"
                ;;
        esac
    done
}

show_help() {
    cat << EOF
$CLI_NAME Installation Script

Usage: $0 [OPTIONS]

Options:
    --help, -h         Show this help message
    --channel CHANNEL  Specify a release channel (default: stable)
    --version VERSION  Install a specific version (default: latest)
    --force, -f        Replace an existing installation without asking
    --no-modify-path   Do not add ~/.local/bin to PATH in shell configuration files

This script will:
1. Detect your platform and architecture
2. Download the appropriate $CLI_NAME package
3. Verify checksums
4. Install $CLI_NAME on your system

For more information, visit: https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-installing.html
EOF
}

# Check for required dependencies
check_dependencies() {

    local missing_deps=()
    
    # Check for downloader
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        missing_deps+=("curl or wget")
    fi
    
    # Check for unzip
    if ! command -v unzip >/dev/null 2>&1; then
        missing_deps+=("unzip")
    fi
    
    # Check for shasum/sha256sum
    if [[ "$os" == "darwin" ]] && ! command -v shasum >/dev/null 2>&1; then
        missing_deps+=("shasum")
    elif [[ "$os" == "linux" ]] && ! command -v sha256sum >/dev/null 2>&1; then
        missing_deps+=("sha256sum")
    fi
    
    if [[ ${#missing_deps[@]} -gt 0 ]]; then
        error "Missing required dependencies: ${missing_deps[*]}"
    fi
}

# Download function that works with both curl and wget
download_file() {
    local url="$1"
    local output="${2:-}"
    
    if command -v curl >/dev/null 2>&1; then
        if [[ -n "$output" ]]; then
            curl -fsSL -o "$output" "$url" || error "Failed to download $url"
        else
            curl -fsSL "$url" || error "Failed to download $url"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if [[ -n "$output" ]]; then
            wget -q -O "$output" "$url" || error "Failed to download $url"
        else
            wget -q -O - "$url" || error "Failed to download $url"
        fi
    else
        error "No downloader available"
    fi
}

# Prints the sha256 of the package whose `download` is the given path or ends in `/<path>`.
#
# For example, `kirocli-x86_64-linux.zip` matches `2.35.2/kirocli-x86_64-linux.zip` but not
# `2.35.2/kirocli-x86_64-linux-musl.zip`, and `1.0.0/x.zip` doesn't match `11.0.0/x.zip`.
get_checksum() {
    local json="$1"
    local path="$2"

    if command -v jq >/dev/null 2>&1; then
        echo "$json" | jq -r --arg path "$path" \
            '(.packages[]?, .versions[]?.packages[]?)
            | select(.download == $path or (.download | endswith("/" + $path))) | .sha256 // empty'
    else
        local pattern package_obj
        pattern=$(printf '%s' "$path" | sed 's/[][\.*^$]/\\&/g')
        package_obj=$(echo "$json" | tr -d '\n\r' |
            grep -o '{[^}]*"download"[[:space:]]*:[[:space:]]*"\([^"]*/\)\{0,1\}'"$pattern"'"[^}]*}' | head -n 1)

        if [[ -n "$package_obj" ]]; then
            if [[ $package_obj =~ \"sha256\"[[:space:]]*:[[:space:]]*\"([a-f0-9]{64})\" ]]; then
                echo "${BASH_REMATCH[1]}"
                return 0
            fi
        fi

        return 1
    fi
}

# =============================================================================
# Platform Detection
# =============================================================================

detect_platform() {
    case "$(uname -s)" in
        Darwin) os="darwin" ;;
        Linux) os="linux" ;;
        *) error "Unsupported operating system: $(uname -s)" ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64" ;;
        arm64|aarch64) arch="aarch64" ;;
        *) error "Unsupported architecture: $(uname -m)" ;;
    esac
}

# Minimum required glibc version. aarch64 gnu builds require a newer glibc than
# x86_64; below the threshold the installer falls back to the musl archive.
GLIBC_MIN_MAJOR=2
GLIBC_X86_64_MIN_MINOR=34
GLIBC_AARCH64_MIN_MINOR=39
GLIBC_MIN_MINOR=$GLIBC_X86_64_MIN_MINOR

# Check if a glibc version meets the minimum requirement
is_glibc_version_sufficient() {
    local version="$1"
    local major minor

    IFS='.' read -r major minor <<EOF
$version
EOF
    if [[ -z "$minor" ]]; then
        minor=0
    fi

    if (( major > GLIBC_MIN_MAJOR || (major == GLIBC_MIN_MAJOR && minor >= GLIBC_MIN_MINOR) )); then
        return 0
    else
        return 1
    fi
}

# Check glibc version for Linux
check_glibc() {
    if [[ "$os" != "linux" ]]; then
        return 0
    fi

    if [[ "$arch" == "aarch64" ]]; then
        GLIBC_MIN_MINOR=$GLIBC_AARCH64_MIN_MINOR
    else
        GLIBC_MIN_MINOR=$GLIBC_X86_64_MIN_MINOR
    fi

    local glibc_version

    # Method 1: Try common libc.so.6 locations
    for LIBC_PATH in /lib64/libc.so.6 /lib/libc.so.6 /usr/lib/x86_64-linux-gnu/libc.so.6 \
        /lib/aarch64-linux-gnu/libc.so.6; do
        if [[ -f "$LIBC_PATH" ]]; then
            glibc_version=$("$LIBC_PATH" | sed -n 's/^GNU C Library (.*) stable release version \([0-9]*\)\.\([0-9]*\).*$/\1.\2/p')
            if [[ -n "$glibc_version" ]]; then
                if is_glibc_version_sufficient "$glibc_version"; then
                    return 0
                else
                    use_musl=true
                    return 0
                fi
            fi
        fi
    done

    # Method 2: Try ldd --version as a more reliable alternative
    if command -v ldd >/dev/null 2>&1; then
        glibc_version=$(ldd --version 2>/dev/null | head -n 1 | grep -o '[0-9]\+\.[0-9]\+' | head -n 1)
        if [[ -n "$glibc_version" ]]; then
            if is_glibc_version_sufficient "$glibc_version"; then
                return 0
            else
                use_musl=true
                return 0
            fi
        fi
    fi

    # Method 3: Try getconf as a fallback
    if command -v getconf >/dev/null 2>&1; then
        glibc_version=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')
        if [[ -n "$glibc_version" ]]; then
            if is_glibc_version_sufficient "$glibc_version"; then
                return 0
            else
                use_musl=true
                return 0
            fi
        fi
    fi

    # Check for musl directly
    if [[ -f /lib/libc.musl-x86_64.so.1 ]] || [[ -f /lib/libc.musl-aarch64.so.1 ]] || \
       ldd /bin/ls 2>&1 | grep -q musl; then
        use_musl=true
        return 0
    fi

    use_musl=true
    return 0
}

# =============================================================================
# Download and Installation Functions
# =============================================================================

# Download and verify file
download_and_verify() {
    local download_url="$1"
    local filename="$2"
    local expected_checksum="$3"

    local file_path="$DOWNLOAD_DIR/$filename"

    download_file "$download_url" "$file_path"

    local actual_checksum
    if [[ "$os" == "darwin" ]]; then
        actual_checksum=$(shasum -a 256 "$file_path" | cut -d' ' -f1)
    else
        actual_checksum=$(sha256sum "$file_path" | cut -d' ' -f1)
    fi

    if [[ "$actual_checksum" != "$expected_checksum" ]]; then
        rm -f "$file_path"
        error "Checksum verification failed. Expected: $expected_checksum, Got: $actual_checksum"
    fi
}


# Install on macOS
install_macos() {
    local dmg_path="$1"
    if [[ ! -f "$dmg_path" ]]; then
        error "DMG file not found: $dmg_path"
    fi

    local mount_path
    # Do not honor build-machine UIDs on the read-only image. Its files need
    # to be readable by the installing user before ditto can copy them.
    mount_path=$(hdiutil attach "$dmg_path" -nobrowse -readonly -owners off | grep Volumes | cut -f 3)
    if [[ -z "$mount_path" ]]; then
        error "Failed to mount DMG"
    fi
    mounted_dmg="$mount_path"

    local app_name="$MACOS_APP_NAME"
    local app_bundle="$mount_path/$app_name"
    if [[ ! -d "$app_bundle" ]]; then
        error "Could not find $app_name in the DMG"
    fi

    # The user already agreed to replace an existing app before the download.
    if [[ -d "$MACOS_APP_DIR/$app_name" ]]; then
        rm -rf "$MACOS_APP_DIR/${app_name:?}"
        log "Existing installation removed"
    fi
    
    # Use ditto instead of cp for better handling of extended attributes and resource forks
    if ! ditto "$app_bundle" "$MACOS_APP_DIR/$app_name"; then
        error "Failed to copy application bundle to $MACOS_APP_DIR"
    fi

    # defaults write can leave the packaged plist owner-readable only (0600).
    # In particular, a root install must leave it readable by the desktop user's
    # LaunchServices process. Otherwise open can report "executable is missing"
    # even when Contents/MacOS contains the executable. Change only this metadata
    # file's mode, not bundle contents, ownership, executable modes or signatures.
    local installed_plist="$MACOS_APP_DIR/$app_name/Contents/Info.plist"
    if [[ ! -f "$installed_plist" || -L "$installed_plist" ]]; then
        error "Application bundle has no regular Contents/Info.plist: $MACOS_APP_DIR/$app_name"
    fi
    if ! chmod 0644 "$installed_plist"; then
        error "Could not make application metadata readable: $installed_plist"
    fi

    if ! open -g -a "$MACOS_APP_DIR/$app_name" --args --no-dashboard; then
        error "Application files were copied, but desktop startup failed. Installation setup may be incomplete."
    fi
    sleep 3
}

# Installs a release zip by running the install.sh inside it
install_zip() {
    local zip_path="$1"
    local extract_dir="$DOWNLOAD_DIR/extract"
    mkdir -p "$extract_dir"

    if ! unzip -q "$zip_path" -d "$extract_dir"; then
        error "Failed to extract $zip_path"
    fi

    # Find and run the install script
    local install_script="$extract_dir/${ZIP_PACKAGE_NAME}/install.sh"

    if [[ ! -f "$install_script" ]]; then
        error "Install script not found in archive"
    fi

    # Checking for kiro-cli-chat since only the headless bundle zip ships it, not the standalone zip.
    if [[ ! -e "$extract_dir/${ZIP_PACKAGE_NAME}/bin/$CHAT_BINARY_NAME" ]]; then
        installed_standalone=true
    fi

    local inner_env=(KIRO_CLI_SKIP_SETUP=1)
    if [[ "$no_modify_path" == "true" ]]; then
        inner_env+=(KIRO_CLI_NO_MODIFY_PATH=1)
    fi
    if [[ -n "${Q_INSTALL_GLOBAL:-}" ]]; then
        inner_env+=(KIRO_CLI_INSTALL_GLOBAL=1)
    fi

    chmod +x "$install_script"
    env "${inner_env[@]}" "$install_script"

    # The standalone ships no kiro-cli-chat, so a bundle's copy would be left behind. This runs only after the
    # inner install succeeds, so a failed install leaves the existing one in place.
    if [[ "$installed_standalone" == "true" ]]; then
        rm -f "$(zip_install_dir)/$CHAT_BINARY_NAME"
    fi
}

# Prints the folder the archive's install.sh installs into.
zip_install_dir() {
    if [[ -n "${Q_INSTALL_GLOBAL:-}" ]]; then
        printf '%s\n' "$GLOBAL_INSTALL_DIR"
    else
        printf '%s\n' "$USER_INSTALL_DIR"
    fi
}

# Asks before replacing the existing install at the given path, and exits the script unless the user agrees.
#
# Returns without asking when --force is set.
confirm_replacing_existing_install() {
    local path="$1"
    warning "Existing $CLI_NAME installation found at $path"
    if [[ "$force" != "true" ]]; then
        # Opening /dev/tty fails with no controlling terminal, as under cron, CI or ssh with a command.
        if ! { : < /dev/tty; } 2>/dev/null; then
            error "No terminal to confirm replacing the existing $CLI_NAME installation. Re-run with --force to replace it."
        fi
        echo "Do you want to replace it? (y/N): "
        local response=""
        read -r response < /dev/tty || true
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            error "Installation cancelled by user"
        fi
    fi
}

# Cleanup function - removes temporary download directory
cleanup() {
    if [ "$SUCCESS" = false ]; then
        echo "❌ Installation failed. Cleaning up..." >&2
    fi

    # Detach mounted DMG if any
    if [[ -n "$mounted_dmg" ]]; then
        hdiutil detach "$mounted_dmg" -quiet 2>/dev/null || true
    fi

    # Remove entire download directory
    if [[ -n "$DOWNLOAD_DIR" ]] && [[ -d "$DOWNLOAD_DIR" ]]; then
        rm -rf "$DOWNLOAD_DIR"
    fi
}

# =============================================================================
# Main Installation Process
# =============================================================================

main() {
    # Parse command line arguments
    parse_args "$@"
    
    print_header

    # Set up cleanup trap
    trap cleanup EXIT

    # Create temporary download directory
    if ! create_download_dir "$MIN_DOWNLOAD_DIR_SPACE_KIB"; then
        error "Could not create a temporary workspace with at least 2 GiB available. Set TMPDIR to a writable directory with enough space."
    fi

    # Platform detection and validation
    detect_platform
    check_dependencies
    check_glibc
    
    # Construct URLs with channel
    local channel_base_url="${BASE_URL}/${CHANNEL}"

    # Reading index.json for a specific version, since the host has a manifest only for the latest release.
    local release_dir="latest"
    local release_json_url="${channel_base_url}/latest/manifest.json"
    local package_prefix=""
    if [[ -n "$VERSION" ]]; then
        log_info "Installing $CLI_NAME $VERSION"
        release_dir="$VERSION"
        release_json_url="${channel_base_url}/index.json"
        package_prefix="$VERSION/"
    fi

    # Reading the manifest or index once, so the package choice and its checksum come from the same file.
    local release_json
    release_json=$(download_file "$release_json_url")
    if [[ -n "$VERSION" ]]; then
        if [[ ! "$release_json" =~ \"versions\"[[:space:]]*:[[:space:]]*\[ ]]; then
            error "Failed to read the version index at $release_json_url"
        fi
        if [[ ! "$release_json" =~ \"version\"[[:space:]]*:[[:space:]]*\""$VERSION"\" ]]; then
            error "Could not find $CLI_NAME $VERSION on the $CHANNEL channel"
        fi
    elif [[ ! "$release_json" =~ \"packages\"[[:space:]]*:[[:space:]]*\[ ]]; then
        error "Failed to read the release manifest at $release_json_url"
    fi

    # Get download information
    local download_url filename package_kind=zip
    if [[ "$os" == "darwin" ]] && [[ -n "$(get_checksum "$release_json" "${package_prefix}${MACOS_DMG_FILENAME}" || true)" ]]; then
        package_kind=dmg
        filename="$MACOS_DMG_FILENAME"
        download_url="${channel_base_url}/${release_dir}/${MACOS_DMG_FILENAME_ESCAPED}"
    elif [[ "$os" == "darwin" ]]; then
        filename="$MACOS_ZIP_FILENAME"
        download_url="${channel_base_url}/${release_dir}/$filename"
    else
        # Linux
        if [[ "$use_musl" == "true" ]]; then
            filename="${ZIP_PACKAGE_NAME}-${arch}-linux-musl.zip"
        else
            filename="${ZIP_PACKAGE_NAME}-${arch}-linux.zip"
        fi
        download_url="${channel_base_url}/${release_dir}/$filename"
    fi

    local expected_checksum
    expected_checksum=$(get_checksum "$release_json" "${package_prefix}${filename}" || true)
    if [[ ! "$expected_checksum" =~ ^[a-f0-9]{64}$ ]]; then
        if [[ -n "$VERSION" ]]; then
            local missing="$filename"
            if [[ "$os" == "darwin" ]]; then
                missing="$MACOS_DMG_FILENAME or $MACOS_ZIP_FILENAME"
            fi
            error "There is no $missing package for $CLI_NAME $VERSION on the $CHANNEL channel. Choose a version that has a package for this platform."
        fi
        error "Could not find valid checksum for $filename"
    fi

    if [[ "$package_kind" == "dmg" && -d "$MACOS_APP_DIR/$MACOS_APP_NAME" ]]; then
        confirm_replacing_existing_install "$MACOS_APP_DIR/$MACOS_APP_NAME"
    elif [[ "$package_kind" == "zip" && -f "$(zip_install_dir)/$BINARY_NAME" ]]; then
        confirm_replacing_existing_install "$(zip_install_dir)/$BINARY_NAME"
    fi
    
    # Download and verify
    log_info "Downloading package..."
    download_and_verify "$download_url" "$filename" "$expected_checksum"
    local downloaded_file="$DOWNLOAD_DIR/$filename"
    log "Downloaded and extracted"


    # Install based on package type
    if [[ "$package_kind" == "dmg" ]]; then
        install_macos "$downloaded_file"
    else
        install_zip "$downloaded_file"
    fi
    
    log "Package installed successfully"

    SUCCESS=true


    # Skipping the PATH note after a standalone zip since its install.sh prints its own PATH guidance.
    if [[ "$installed_standalone" == "true" ]] || [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
        success
    else
        success_without_path
    fi
}

# Run main function when executed directly or read from stdin
if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]:-}" == "$0" ]]; then
    main "$@"
fi
