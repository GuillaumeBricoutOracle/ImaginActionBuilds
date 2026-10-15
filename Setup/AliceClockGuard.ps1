# AliceClockGuard.ps1 -- apres une coupure de courant, un PC sans pile CMOS
# revient a une date figee : toutes les sessions Unity s'empilent sur le meme
# jour dans les journaux. Ici on ne cherche pas l'heure exacte (pas d'internet
# sur site) : on veut seulement que chaque redemarrage tombe sur une date
# DIFFERENTE et POSTERIEURE, pour que les fichiers-jours et le dashboard
# separent les sessions.
#
# Principe (deux taches planifiees, voir AliceClockGuard.bat) :
#   -Mode Record  toutes les minutes : note l'heure courante (UTC) dans un
#                 fichier temoin.
#   -Mode Boot    au demarrage : si l'horloge est revenue EN ARRIERE par
#                 rapport au temoin, elle a ete perdue. On la regle sur
#                     temoin + 1 jour
#                 (meme heure que le dernier temoin, le lendemain). Chaque
#                 coupure fait donc avancer la date d'un jour, quelle que soit
#                 sa duree reelle.
#   -Mode Status  affiche l'etat (temoin, journal, taches).
#   -Mode Test    montre ce que ferait Boot, sans rien changer.
#
# La date derive donc avec les coupures : c'est voulu, seul l'ordre des jours
# compte. Journal : C:\ProgramData\AliceClockGuard\clockguard.log

param(
    [ValidateSet('Record', 'Boot', 'Status', 'Test')]
    [string]$Mode = 'Status',
    [string]$Dir = 'C:\ProgramData\AliceClockGuard',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# Toute erreur imprevue finit dans le journal plutot qu'en silence.
trap { try { Write-Log ('ERREUR : ' + $_.Exception.Message) } catch { }; exit 1 }
$Witness = Join-Path $Dir 'last-time.txt'
$LogFile = Join-Path $Dir 'clockguard.log'
$MinJumpMinutes = 2                  # en dessous, ce n'est pas une horloge perdue

function Fmt([DateTime]$utc) { $utc.ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss') }

function Write-Log([string]$msg) {
    if (-not (Test-Path $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
    $line = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' | ' + $Mode + ' | ' + $msg
    Add-Content -Path $LogFile -Value $line -Encoding ASCII
    # Journal borne a 500 lignes
    try {
        $all = Get-Content $LogFile
        if ($all.Count -gt 500) { $all[-500..-1] | Set-Content -Path $LogFile -Encoding ASCII }
    } catch { }
}

function Read-Witness {
    if (-not (Test-Path $Witness)) { return $null }
    try {
        $s = (Get-Content $Witness -First 1).Trim()
        return [DateTime]::Parse($s, $null, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
    } catch { return $null }
}

# Secondes ecoulees depuis le demarrage du PC, independantes de l'horloge.
# [Environment]::TickCount = millisecondes depuis le boot (compteur 32 bits,
# exact pendant 24 jours : largement assez pour distinguer "juste apres le
# boot" de "en cours de session").
function Get-UptimeSeconds {
    $ms = [Environment]::TickCount
    if ($ms -lt 0) { $ms = $ms + 4294967296 }
    return $ms / 1000.0
}

switch ($Mode) {

    'Record' {
        $now = [DateTime]::UtcNow
        $last = Read-Witness
        if ($last -ne $null -and $now -lt $last) {
            # L'horloge est en arriere du temoin. Juste apres un boot, c'est une
            # horloge perdue : on garde le temoin, la tache Boot s'en sert.
            # Plus tard dans la session, c'est quelqu'un (ou NTP) qui vient de
            # remettre l'heure : on adopte la nouvelle heure.
            if ((Get-UptimeSeconds) -lt 600) { exit 0 }
        }
        if (-not (Test-Path $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
        $tmp = $Witness + '.tmp'
        Set-Content -Path $tmp -Value $now.ToString('o') -Encoding ASCII
        Move-Item -Path $tmp -Destination $Witness -Force
        exit 0
    }

    'Boot' {
        $last = Read-Witness
        if ($last -eq $null) { Write-Log 'pas de temoin : rien a corriger'; exit 0 }
        $now = [DateTime]::UtcNow
        $behind = $last - $now
        if ($behind.TotalMinutes -lt $MinJumpMinutes) {
            Write-Log ('horloge OK : ' + (Fmt $now) + ' >= temoin ' + (Fmt $last))
            exit 0
        }
        $target = $last.AddDays(1)
        $msg = 'CORRECTION : horloge ' + (Fmt $now) + ' en retard de ' + [int]$behind.TotalMinutes +
               ' min sur le temoin ' + (Fmt $last) + ' -> reglee sur temoin + 1 jour = ' + (Fmt $target)
        if ($DryRun) { Write-Log ('[essai] ' + $msg); Write-Host ('[essai] ' + $msg); exit 0 }
        try {
            Set-Date -Date $target.ToLocalTime() | Out-Null
        } catch {
            Write-Log ('ECHEC Set-Date (' + $_.Exception.Message + ') : ' + $msg)
            exit 1
        }
        Write-Log $msg
        # Le temoin repart de l'heure corrigee.
        Set-Content -Path $Witness -Value ([DateTime]::UtcNow.ToString('o')) -Encoding ASCII
        exit 0
    }

    'Status' {
        $last = Read-Witness
        $now = [DateTime]::UtcNow
        Write-Host ''
        Write-Host ('  Heure du PC          : ' + (Fmt $now))
        if ($last -eq $null) { Write-Host '  Temoin               : aucun (la tache Record n''a pas encore tourne)' }
        else { Write-Host ('  Temoin               : ' + (Fmt $last) + '  (il y a ' + [int]($now - $last).TotalMinutes + ' min)') }
        Write-Host ('  PC demarre depuis    : ' + [int]((Get-UptimeSeconds) / 60) + ' min')
        foreach ($t in 'AliceClockRecord', 'AliceClockBoot') {
            & cmd.exe /c ('schtasks /query /tn ' + $t + ' >nul 2>&1')
            if ($LASTEXITCODE -eq 0) { Write-Host ('  Tache ' + $t.PadRight(16) + ': en place') }
            else { Write-Host ('  Tache ' + $t.PadRight(16) + ': ABSENTE') }
        }
        if (Test-Path $LogFile) {
            Write-Host ''
            Write-Host '  Dernieres lignes du journal :'
            Get-Content $LogFile -Tail 8 | ForEach-Object { Write-Host ('    ' + $_) }
        }
        Write-Host ''
    }

    'Test' {
        $now = [DateTime]::UtcNow
        Write-Host ''
        Write-Host ('  Si le courant etait coupe maintenant (' + (Fmt $now) + '),')
        Write-Host '  au redemarrage le PC se reveillerait avec son horloge figee (2 octobre 12:46)'
        Write-Host ('  et serait regle sur : ' + (Fmt $now.AddDays(1)) + '  (derniere heure connue + 1 jour)')
        Write-Host ''
    }
}
