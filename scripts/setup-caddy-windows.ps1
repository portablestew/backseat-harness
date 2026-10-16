<#
.SYNOPSIS
    Interactive, opt-in setup of caddy mode on Windows (README "Caddy mode").

.DESCRIPTION
    Run from an elevated (Administrator) PowerShell. In order it:
      1. refuses to run unless elevated, before any side effect;
      2. installs caddy with winget if it is not already on PATH;
      3. adds an inbound firewall rule for TCP 8443 (idempotent);
      4. prompts for the site host, basic-auth user, web port and password
         (password typed twice, hidden). The site answers as the host you
         give, localhost, 127.0.0.1 and this machine's private IPv4 address,
         which is detected here (re-run the script if it changes);
      5. hashes the password with `caddy hash-password` (plaintext is never
         written to a log or to the caddyfile);
      6. writes ~/.backseat/caddyfile, after an explicit confirmation if one
         already exists (the old file is moved aside, not deleted);
      7. runs `caddy validate` on the result and prints the next steps.

    The daemon never writes a caddyfile; this script is the only writer, and
    only when you run it.
#>

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "== $Text"
}

function Stop-Setup([string]$Message) {
    Write-Host "setup-caddy: $Message"
    exit 1
}

function Read-Default([string]$Prompt, [string]$Default) {
    $answer = Read-Host "$Prompt [$Default]"
    if ($answer) { return $answer }
    return $Default
}

function Find-Caddy {
    $cmd = Get-Command caddy -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Update-SessionPath {
    # winget updates the registry PATH, not this session's PATH.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function ConvertFrom-SecurePlain([Security.SecureString]$Secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Test-PrivateIPv4([string]$Address) {
    $o = $Address.Split('.') | ForEach-Object { [int]$_ }
    return ($o[0] -eq 10) -or ($o[0] -eq 192 -and $o[1] -eq 168) -or ($o[0] -eq 172 -and $o[1] -ge 16 -and $o[1] -le 31)
}

function Find-PrivateIPv4 {
    # The private address on the default-route interface, else any private address.
    $addresses = @()
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
    if ($route) {
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue)
    }
    $private = @($addresses | Where-Object { Test-PrivateIPv4 $_.IPAddress })
    if ($private.Count -eq 0) {
        $private = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { Test-PrivateIPv4 $_.IPAddress })
    }
    if ($private.Count -eq 0) { return $null }
    return $private[0].IPAddress
}

function Wait-ForKey {
    # Keeps the window open when the script is started from Explorer. Scripted
    # runs (redirected input) do not wait.
    if (-not [Console]::IsInputRedirected) {
        Write-Host ''
        Write-Host 'Press any key to close...'
        [void][Console]::ReadKey($true)
    }
}

# 1. Elevation. This runs before anything else touches the machine.
Write-Step '1/7 checking for an elevated session'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'setup-caddy: this script must run from an elevated (Administrator) PowerShell.'
    Write-Host 'setup-caddy: right-click PowerShell, choose "Run as administrator", and run it again.'
    Write-Host 'setup-caddy: nothing was installed, changed, or written.'
    Wait-ForKey
    exit 2
}

# 2. caddy on PATH, installing it with winget if needed.
Write-Step '2/7 finding caddy'
$caddy = Find-Caddy
if (-not $caddy) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Stop-Setup 'caddy is not on PATH and winget is not available. Install caddy, then run this again.'
    }
    Write-Host 'caddy not found on PATH; installing CaddyServer.Caddy with winget...'
    & winget install --id CaddyServer.Caddy -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "setup-caddy: winget exited $LASTEXITCODE; checking whether caddy is now present anyway."
    }
    Update-SessionPath
    $caddy = Find-Caddy
    if (-not $caddy) {
        Stop-Setup 'caddy is still not on PATH in this session. Open a new elevated PowerShell and run this again.'
    }
}
Write-Host "using caddy at $caddy"

# 3. Firewall rule for the HTTPS site port. Re-running does not add a second rule.
Write-Step '3/7 firewall rule for TCP 8443'
$ruleName = 'backseat-harness caddy HTTPS (TCP 8443)'
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Write-Host 'firewall rule already present; leaving it as it is.'
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 8443 -Action Allow | Out-Null
    Write-Host 'firewall rule added.'
}

# 4. Answers. Nothing is written yet.
Write-Step '4/7 settings'
$siteHost = Read-Default 'Site host name (must resolve to this machine from the browser)' 'localhost'
if ($siteHost -notmatch '^[A-Za-z0-9.-]+$') {
    Stop-Setup "site host '$siteHost' may contain only letters, digits, dots and hyphens"
}
$username = Read-Default 'Basic-auth username' 'alice'
if ($username -notmatch '^[A-Za-z0-9._-]+$') {
    Stop-Setup "username '$username' may contain only letters, digits, dots, underscores and hyphens"
}
$webPort = Read-Default '[web] port the web app listens on (must match your config)' '8090'
if ($webPort -notmatch '^[0-9]{1,5}$' -or [int]$webPort -lt 1 -or [int]$webPort -gt 65535) {
    Stop-Setup "web port '$webPort' is not a port number"
}
$lanIp = Find-PrivateIPv4
if ($lanIp) {
    Write-Host "private IPv4 address detected: $lanIp (added to the site addresses)"
} else {
    Write-Host 'no private IPv4 address found; the LAN address is not added.'
}

$secure1 = Read-Host 'Password' -AsSecureString
$secure2 = Read-Host 'Repeat password' -AsSecureString
$plain1 = ConvertFrom-SecurePlain $secure1
$plain2 = ConvertFrom-SecurePlain $secure2
if ($plain1.Length -eq 0) { Stop-Setup 'the password is empty; nothing was written' }
if ($plain1 -cne $plain2) { Stop-Setup 'the two passwords do not match; nothing was written' }
# The password travels as one native argument to caddy. Windows PowerShell 5.1
# does not escape embedded quotes, and a trailing backslash escapes the closing
# quote, so refuse those rather than hash the wrong string.
if ($plain1.Contains('"') -or $plain1.EndsWith('\')) {
    Stop-Setup 'the password may not contain a double quote or end with a backslash; nothing was written'
}
$plain2 = $null

# 5. Hash. Only the hash is kept; the plaintext is cleared after this step.
Write-Step '5/7 hashing the password'
$hashOutput = & $caddy hash-password --plaintext $plain1
if ($LASTEXITCODE -ne 0) { Stop-Setup "caddy hash-password exited $LASTEXITCODE" }
$plain1 = $null
$hash = ($hashOutput | Select-Object -Last 1).Trim()
if ($hash -notmatch '^\$2[abxy]\$') {
    Stop-Setup 'caddy hash-password did not return a bcrypt hash; nothing was written'
}
Write-Host 'password hashed.'

# 6. Write the caddyfile, next to the existing one if there is one.
Write-Step '6/7 writing ~/.backseat/caddyfile'
$dir = Join-Path $env:USERPROFILE '.backseat'
$caddyfile = Join-Path $dir 'caddyfile'
$staged = "$caddyfile.new"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

if (Test-Path $caddyfile) {
    Write-Host "A caddyfile already exists at $caddyfile :"
    Get-Content $caddyfile | ForEach-Object { Write-Host "    $_" }
    Write-Host 'Continuing moves it to caddyfile.bak-<timestamp> and writes a new one.'
    $confirm = Read-Host 'Type REPLACE to continue, anything else to stop'
    if ($confirm -cne 'REPLACE') { Stop-Setup 'not replacing the existing caddyfile; nothing was changed' }
}

# The site answers under each of these names. Duplicates (for example a host of
# localhost) are removed, because caddy rejects the same site address twice.
$names = @($siteHost, 'localhost', '127.0.0.1')
if ($lanIp) { $names += $lanIp }
$names = @($names | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique)
$siteLine = ($names | ForEach-Object { "${_}:8443" }) -join ', '

# skip_install_trust: caddy does not install its root CA into the Windows trust
# store, so the trust dialog does not appear on each daemon restart.
# auto_https disable_redirects: caddy does not open the TCP 80 redirect listener.
$content = @"
{
	skip_install_trust
	auto_https disable_redirects
}
$siteLine {
	tls internal
	basic_auth {
		$username $hash
	}
	reverse_proxy 127.0.0.1:$webPort
}
"@
[IO.File]::WriteAllText($staged, $content + "`n", (New-Object Text.UTF8Encoding($false)))

# 7. Validate the staged file before it replaces anything.
Write-Step '7/7 validating'
& $caddy validate --config $staged --adapter caddyfile
if ($LASTEXITCODE -ne 0) {
    Remove-Item $staged -Force
    Stop-Setup 'caddy validate rejected the generated caddyfile; the existing one (if any) was left alone'
}

if (Test-Path $caddyfile) {
    $backup = "$caddyfile.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Move-Item $caddyfile $backup
    Write-Host "previous caddyfile moved to $backup"
}
Move-Item $staged $caddyfile
Write-Host "caddyfile written: $caddyfile"

Write-Host ''
Write-Host 'Setup finished. Next steps, in this order:'
Write-Host '  1. backseat-harness daemon install     (re-run so the daemon PATH includes caddy)'
Write-Host '  2. backseat-harness daemon restart     (starts caddy with the new caddyfile)'
$urls = ($names | ForEach-Object { "https://${_}:8443" }) -join ' or '
Write-Host "Then open $urls and sign in as $username."
Write-Host 'The browser will show a certificate warning (tls internal) unless you choose to trust caddy''s local CA yourself.'
Write-Host 'The caddyfile sets skip_install_trust (no trust dialog) and auto_https disable_redirects (caddy does not listen on TCP 80).'
