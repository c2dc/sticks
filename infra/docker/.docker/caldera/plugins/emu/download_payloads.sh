#!/bin/sh
# We are not able to bundle some payloads because their licensing
# prohibits redistribution (notably sysinternals).  This script will
# will download non-redistributable payloads.  If you're deploying
# the plugin without internet access, you can copy this script to
# an internet connected host, run it, and then copy the resulting
# payloads back to the emu/payloads directory

# This script is intentionally resilient: a failed external download must
# NEVER abort the caller (e.g. the Docker image build).  Every download is
# best-effort.  Failures are recorded in payloads/DOWNLOAD_REPORT.txt and the
# script always exits 0.  The Windows emulation payloads fetched here are not
# used by the STICKS Linux curated cases, so a missing payload is not fatal.

# Do not let any single command failure abort the script.
set +e

mkdir -p payloads
REPORT="payloads/DOWNLOAD_REPORT.txt"

DOWNLOADED=""
MISSING=""

# timestamp: emit an ISO-8601-ish UTC timestamp (POSIX date).
timestamp() {
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

# log_line <text>: append a line to the report and echo it to stdout.
log_line() {
    echo "$(timestamp) $1" >> "$REPORT"
}

# fetch <url> <output>: best-effort download.
# Returns 0 on success (file present) and records the item as OK; returns 1
# on failure and records the item as MISSING.  Never aborts the script.
fetch() {
    _url="$1"
    _out="$2"
    if curl -fsSL --connect-timeout 20 --max-time 300 --retry 2 -o "$_out" "$_url" && [ -s "$_out" ]; then
        DOWNLOADED="$DOWNLOADED $_out"
        log_line "OK      $_out  <-  $_url"
        return 0
    else
        MISSING="$MISSING $_out"
        log_line "MISSING $_out  <-  $_url"
        return 1
    fi
}

# run_step <description>: run a follow-up step (already expressed as the
# remaining positional args) and log-but-never-abort on failure.
# Usage: run_step "description" command args...
run_step() {
    _desc="$1"
    shift
    if "$@"; then
        log_line "STEP OK   $_desc"
        return 0
    else
        log_line "STEP FAIL $_desc"
        echo "warning: follow-up step failed (continuing): $_desc"
        return 1
    fi
}

# Start a fresh report for this run.
: > "$REPORT"
log_line "emu payload download report"

# --- AdFind -----------------------------------------------------------------
if fetch "http://www.joeware.net/downloads/files/AdFind.zip" "payloads/AdFind.zip"; then
    _pw=$(unzip -p payloads/AdFind.zip password.txt 2>/dev/null)
    run_step "unzip AdFind.zip" unzip -o -P "$_pw" payloads/AdFind.zip -d payloads/
    if [ -f payloads/AdFind.exe ]; then
        run_step "cp AdFind.exe -> adfind.exe" cp payloads/AdFind.exe payloads/adfind.exe
    fi
fi

# --- dnscat2 ----------------------------------------------------------------
fetch "https://raw.githubusercontent.com/lukebaggett/dnscat2-powershell/master/dnscat2.ps1" "payloads/dnscat2.ps1"

# --- NetSess ----------------------------------------------------------------
if fetch "http://www.joeware.net/downloads/files/NetSess.zip" "payloads/NetSess.zip"; then
    run_step "unzip NetSess.zip" unzip -o payloads/NetSess.zip -d payloads/
    if [ -f payloads/NetSess.exe ]; then
        run_step "cp NetSess.exe -> netsess.exe" cp payloads/NetSess.exe payloads/netsess.exe
    fi
fi

# --- nbtscan ----------------------------------------------------------------
fetch "http://unixwiz.net/tools/nbtscan-1.0.35.exe" "payloads/nbtscan.exe"

# --- psexec (impacket) ------------------------------------------------------
if fetch "https://github.com/ropnop/impacket_static_binaries/releases/download/0.9.22.dev-binaries/psexec_windows.exe" "payloads/psexec.exe"; then
    run_step "cp psexec.exe -> PsExec.exe" cp payloads/psexec.exe payloads/PsExec.exe
fi

# --- putty ------------------------------------------------------------------
fetch "https://the.earth.li/~sgtatham/putty/latest/w64/putty.exe" "payloads/putty.exe"

# --- secretsdump (impacket) -------------------------------------------------
fetch "https://github.com/ropnop/impacket_static_binaries/releases/download/0.9.22.dev-binaries/secretsdump_windows.exe" "payloads/secretsdump.exe"

# --- tcping -----------------------------------------------------------------
fetch "https://download.elifulkerson.com//files/tcping/0.39/tcping.exe" "payloads/tcping.exe"

# --- wce --------------------------------------------------------------------
if fetch "https://www.ampliasecurity.com/research/wce_v1_41beta_universal.zip" "payloads/wce_v1_41beta_universal.zip"; then
    run_step "unzip wce_v1_41beta_universal.zip" unzip -o payloads/wce_v1_41beta_universal.zip -d payloads/
fi

# --- wmiexec ----------------------------------------------------------------
fetch "https://raw.githubusercontent.com/Twi1ight/AD-Pentest-Script/master/wmiexec.vbs" "payloads/wmiexec.vbs"

# --- psexec_sandworm (impacket example) -------------------------------------
fetch "https://raw.githubusercontent.com/SecureAuthCorp/impacket/c328de825265df12ced44d14b36c688cd9973f5c/examples/psexec.py" "payloads/psexec_sandworm.py"

# --- PSTools (sysinternals via web.archive) ---------------------------------
if fetch "https://web.archive.org/web/20221102141531/http://download.sysinternals.com/files/PSTools.zip" "payloads/PSTools.zip"; then
    run_step "unzip PSTools.zip" unzip -o payloads/PSTools.zip -d payloads/PSTools
    if [ -f payloads/PSTools/PsExec64.exe ]; then
        psexec_md5=$(md5sum payloads/PSTools/PsExec64.exe | awk '{ print $1 }')
        if [ "$psexec_md5" = "84858ca42dc54947eea910e8fab5f668" ]; then
            target_dir="data/adversary-emulation-plans/turla/Resources/payloads/snake"
            run_step "cp PsExec64.exe -> Turla snake payloads" sh -c 'mkdir -p "$0" && cp payloads/PSTools/PsExec64.exe "$0/PsExec.exe"' "$target_dir"
            echo "PsExec64.exe v2.4 copied to Turla payloads directory"
        else
            echo "PsExec from PSTools.zip with MD5 '$psexec_md5' does not match v2.4 with MD5 of 84858ca42dc54947eea910e8fab5f668"
        fi
    fi
fi

# --- pscp (copied to Turla carbon payloads) ---------------------------------
if fetch "https://the.earth.li/~sgtatham/putty/latest/w64/pscp.exe" "payloads/pscp.exe"; then
    target_dir="data/adversary-emulation-plans/turla/Resources/payloads/carbon"
    run_step "cp pscp.exe -> Turla carbon payloads" sh -c 'mkdir -p "$0" && cp payloads/pscp.exe "$0/pscp.exe"' "$target_dir"
    echo "Pscp.exe copied to Turla payloads directory"
fi

# --- plink (copied to Turla carbon payloads) --------------------------------
if fetch "https://the.earth.li/~sgtatham/putty/latest/w64/plink.exe" "payloads/plink.exe"; then
    target_dir="data/adversary-emulation-plans/turla/Resources/payloads/carbon"
    run_step "cp plink.exe -> Turla carbon payloads" sh -c 'mkdir -p "$0" && cp payloads/plink.exe "$0/plink.exe"' "$target_dir"
    echo "Plink.exe copied to Turla payloads directory"
fi

# --- mimikatz ---------------------------------------------------------------
if fetch "https://github.com/ParrotSec/mimikatz/blob/master/x64/mimikatz.exe" "payloads/m64.exe"; then
    echo "x64 mimikatz.exe copied to payloads directory as m64.exe"
fi

# --- Summary ----------------------------------------------------------------
ok_count=0
for _f in $DOWNLOADED; do
    ok_count=$((ok_count + 1))
done
miss_count=0
for _f in $MISSING; do
    miss_count=$((miss_count + 1))
done
total_count=$((ok_count + miss_count))

summary="emu payload download summary: $ok_count OK, $miss_count MISSING (of $total_count attempted)"
log_line "$summary"
echo ""
echo "==============================================================="
echo "$summary"
if [ "$miss_count" -gt 0 ]; then
    echo "Missing payloads (best-effort; optional for the contained Linux cases):"
    for _f in $MISSING; do
        echo "  - $_f"
    done
fi
echo "See $REPORT for details."
echo "==============================================================="

# Always succeed - a failed download must never abort the caller (build).
exit 0
