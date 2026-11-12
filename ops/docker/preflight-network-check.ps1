# ============================================================
# preflight-network-check.ps1 — CONTAINMENT SAFEGUARD
# ============================================================
# Checks, BEFORE bringing up the lab, whether any of the SUT subnets is already
# in use on this machine — whether by another Docker network or by a host
# route/interface. An overlapping subnet would make routing send attack packets
# to the WRONG destination (outside the lab), breaking containment. If there is a
# conflict, this script FAILS (exit 1) and the lab must NOT be started.
#
# Usage:
#   pwsh ./preflight-network-check.ps1
#   (exit 0 = free to bring up; exit 1 = conflict, do not bring up)
#
# The lab subnets (class C). Octet 3 = network, octet 4 = machine:
#   caldera-kali 192.168.10.0/24 | kali-nginx 192.168.20.0/24
#   nginx-db     192.168.30.0/24 | mgmt       192.168.40.0/24
# ============================================================

$ErrorActionPreference = "Stop"

$LAB_SUBNETS = @(
  "192.168.10.0/24",  # caldera-kali-network
  "192.168.20.0/24",  # kali-nginx-network
  "192.168.30.0/24",  # nginx-db-network
  "192.168.40.0/24"   # caldera-mgmt-network
)

function Get-Base24([string]$cidr) {
  if ($cidr -match '^(\d+\.\d+\.\d+)\.') { return $matches[1] }
  return $null
}

Write-Host "[preflight] Checking lab subnet conflicts..." -ForegroundColor Cyan

# 1) Subnets already used by existing Docker networks (except the lab's own
#    networks, which may already exist from a previous `up` — those are OK to reuse).
$labNetNames = @(
  "docker_caldera-kali-network","docker_kali-nginx-network",
  "docker_nginx-db-network","docker_caldera-mgmt-network",
  "caldera-kali-network","kali-nginx-network","nginx-db-network","caldera-mgmt-network"
)
$dockerUsed = @{}  # base/24 -> network name
foreach ($name in (docker network ls --format "{{.Name}}")) {
  if ($labNetNames -contains $name) { continue }
  $subs = docker network inspect $name --format "{{range .IPAM.Config}}{{.Subnet}} {{end}}" 2>$null
  foreach ($s in ($subs -split '\s+')) {
    $b = Get-Base24 $s.Trim()
    if ($b) { $dockerUsed[$b] = $name }
  }
}

# 2) Host route/interface prefixes (ignores loopback/link-local/multicast).
$hostUsed = @{}  # base/24 -> prefix:interface
Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {
  $p = $_.DestinationPrefix
  if ($p -match '^(\d+\.\d+\.\d+)\.\d+/(\d+)$') {
    $len = [int]$matches[2]; $b = $matches[1]
    if ($len -ge 8 -and $len -le 24 -and $p -notlike "127.*" -and $p -notlike "169.254.*" -and $p -notlike "224.*") {
      $hostUsed[$b] = "$p ($($_.InterfaceAlias))"
    }
  }
}

$conflicts = @()
foreach ($sub in $LAB_SUBNETS) {
  $base = Get-Base24 $sub
  if ($dockerUsed.ContainsKey($base)) {
    $conflicts += "  $sub  already used by Docker network '$($dockerUsed[$base])'"
  }
  if ($hostUsed.ContainsKey($base)) {
    $conflicts += "  $sub  overlaps host route $($hostUsed[$base])"
  }
}

if ($conflicts.Count -gt 0) {
  Write-Host "[preflight] SUBNET CONFLICT DETECTED — do NOT bring up the lab:" -ForegroundColor Red
  $conflicts | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  Write-Host ""
  Write-Host "Adjust the lab subnets in docker-compose.yml to free ranges" -ForegroundColor Yellow
  Write-Host "(and the matching IPs) before emulating — otherwise attack" -ForegroundColor Yellow
  Write-Host "packets may be routed to destinations outside the lab." -ForegroundColor Yellow
  exit 1
}

Write-Host "[preflight] OK — all 4 lab subnets are free." -ForegroundColor Green
foreach ($sub in $LAB_SUBNETS) { Write-Host "  FREE  $sub" -ForegroundColor Green }
exit 0
