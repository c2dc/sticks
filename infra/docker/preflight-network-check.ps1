#!/usr/bin/env pwsh
# ============================================================
# preflight-network-check.ps1 — SALVAGUARDA DE CONTENÇÃO
# ============================================================
# Verifica, ANTES de subir o laboratório, se alguma das subnets do SUT já está
# em uso nesta máquina — seja por outra rede Docker, seja por uma rota/interface
# do host. Uma subnet sobreposta faria o roteamento enviar pacotes de ataque
# para o destino ERRADO (fora do laboratório), quebrando a contenção. Se houver
# conflito, este script FALHA (exit 1) e o lab NÃO deve ser iniciado.
#
# Uso:
#   pwsh ./preflight-network-check.ps1
#   (exit 0 = livre para subir; exit 1 = conflito, não suba)
#
# As subnets do laboratório (classe C). Octeto 3 = rede, octeto 4 = máquina:
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

Write-Host "[preflight] Verificando conflitos de subnet do laboratório..." -ForegroundColor Cyan

# 1) Subnets já usadas por redes Docker existentes (exceto as próprias redes do lab,
#    que podem já existir de um `up` anterior — essas são OK para reuso).
$labNetNames = @(
  "docker_caldera-kali-network","docker_kali-nginx-network",
  "docker_nginx-db-network","docker_caldera-mgmt-network",
  "caldera-kali-network","kali-nginx-network","nginx-db-network","caldera-mgmt-network"
)
$dockerUsed = @{}  # base/24 -> nome da rede
foreach ($name in (docker network ls --format "{{.Name}}")) {
  if ($labNetNames -contains $name) { continue }
  $subs = docker network inspect $name --format "{{range .IPAM.Config}}{{.Subnet}} {{end}}" 2>$null
  foreach ($s in ($subs -split '\s+')) {
    $b = Get-Base24 $s.Trim()
    if ($b) { $dockerUsed[$b] = $name }
  }
}

# 2) Prefixos de rota/interface do host (ignora loopback/link-local/multicast).
$hostUsed = @{}  # base/24 -> prefixo:interface
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
    $conflicts += "  $sub  já usada por rede Docker '$($dockerUsed[$base])'"
  }
  if ($hostUsed.ContainsKey($base)) {
    $conflicts += "  $sub  sobrepõe rota do host $($hostUsed[$base])"
  }
}

if ($conflicts.Count -gt 0) {
  Write-Host "[preflight] CONFLITO DE SUBNET DETECTADO — NÃO suba o laboratório:" -ForegroundColor Red
  $conflicts | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  Write-Host ""
  Write-Host "Ajuste as subnets do lab em docker-compose.yml para faixas livres" -ForegroundColor Yellow
  Write-Host "(e os IPs correspondentes) antes de emular — caso contrário pacotes" -ForegroundColor Yellow
  Write-Host "de ataque podem ser roteados para destinos fora do laboratório." -ForegroundColor Yellow
  exit 1
}

Write-Host "[preflight] OK — todas as 4 subnets do laboratório estão livres." -ForegroundColor Green
foreach ($sub in $LAB_SUBNETS) { Write-Host "  LIVRE  $sub" -ForegroundColor Green }
exit 0
