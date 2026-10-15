@echo off
setlocal
:: Apres une coupure de courant, ce PC (sans pile CMOS) revient a une date
:: figee : toutes les sessions Unity s'empilent sur le meme jour dans les logs.
:: On ne cherche pas l'heure exacte (pas d'internet sur site) : on veut
:: seulement que chaque redemarrage tombe sur une date differente et posterieure.
::
:: Pose deux taches planifiees (compte SYSTEM) qui executent AliceClockGuard.ps1 :
::   AliceClockRecord : toutes les minutes, note l'heure courante dans un temoin.
::   AliceClockBoot   : au demarrage, si l'horloge est revenue en arriere,
::                      la regle sur "temoin + 1 jour".
:: Chaque coupure fait donc avancer la date d'un jour, quelle que soit sa duree.
::
:: A poser a cote de AliceClockGuard.ps1 puis double-cliquer (droits admin
:: demandes automatiquement). Relancer pour voir l'etat ou desactiver.
:: Journal : C:\ProgramData\AliceClockGuard\clockguard.log

net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

title Correction automatique de la date
set "DIR=C:\ProgramData\AliceClockGuard"
set "PS1=%DIR%\AliceClockGuard.ps1"
set "SRC=%~dp0AliceClockGuard.ps1"

schtasks /query /tn "AliceClockBoot" >nul 2>&1
if errorlevel 1 (set "STATE=DESACTIVEE") else (set "STATE=ACTIVE")

echo.
echo   Correction automatique de la date apres coupure : %STATE%
echo.
choice /c ON /n /m "  Activer ou mettre a jour la correction ? [O]ui / [N]on = desactiver : "
if errorlevel 2 goto disable

:enable
if not exist "%SRC%" (
    echo   AliceClockGuard.ps1 introuvable a cote de ce fichier : %SRC%
    goto end
)
if not exist "%DIR%" mkdir "%DIR%"
copy /y "%SRC%" "%PS1%" >nul || (echo   Copie impossible vers %DIR% & goto end)
if exist "%DIR%\pause-hours.txt" del "%DIR%\pause-hours.txt"

:: Taches creees par PowerShell et non par "schtasks /create" : schtasks date
:: l'activation de chaque declencheur au moment de la creation. Or sur ce PC
:: l'horloge revient apres chaque coupure a une date FIXE (le 2 octobre 12:46,
:: vue au boot comme anterieure a la creation) : la tache "au demarrage" ne se
:: lancerait jamais. Ici : declencheur au demarrage sans date, et repetition
:: toutes les minutes dont le debut est fixe en l'an 2000, toujours passe.
set "PSARG=-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File %PS1%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '%PSARG% -Mode Record'; $t=New-ScheduledTaskTrigger -Once -At '2000-01-01 00:00' -RepetitionInterval (New-TimeSpan -Minutes 1); $p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; $s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5); Register-ScheduledTask -TaskName 'AliceClockRecord' -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null"
schtasks /query /tn "AliceClockRecord" >nul 2>&1 || (echo   ECHEC de la creation de la tache AliceClockRecord. & goto end)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '%PSARG% -Mode Boot'; $t=New-ScheduledTaskTrigger -AtStartup; $p=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; $s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5); Register-ScheduledTask -TaskName 'AliceClockBoot' -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null"
schtasks /query /tn "AliceClockBoot" >nul 2>&1 || (echo   ECHEC de la creation de la tache AliceClockBoot. & goto end)
schtasks /run /tn "AliceClockRecord" >nul 2>&1

echo.
echo   OK : au prochain demarrage apres une coupure, la date avancera d'un jour
echo   par rapport a la derniere heure connue.

:: Le script ne fabrique pas la date de depart : il fait avancer la derniere
:: date connue. Si la date est deja fausse maintenant, c'est le moment de la
:: remettre une fois a la main.
echo.
echo   Date et heure actuelles du PC : %DATE% %TIME:~0,5%
choice /c ON /n /m "  Sont-elles justes ? [O]ui / [N]on, je les corrige : "
if errorlevel 2 call :settime

:: Le build Unity est lance 20 s apres l'ouverture de session (tache AliceUnity).
:: La correction tourne au demarrage de Windows, avant l'ouverture de session,
:: mais 60 s laissent plus de marge pour que l'en-tete de session soit juste.
schtasks /query /tn "AliceUnity" >nul 2>&1 || goto status
echo.
choice /c ON /n /m "  Retarder le lancement d'ImaginAction a 60 s apres l'ouverture de session (au lieu de 20 s) ? [O]ui / [N]on : "
if errorlevel 2 goto status
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t = Get-ScheduledTask -TaskName 'AliceUnity'; $t.Triggers[0].Delay = 'PT60S'; Set-ScheduledTask -InputObject $t | Out-Null"
if errorlevel 1 (echo   ECHEC : delai inchange.) else (echo   OK : ImaginAction se lancera 60 s apres l'ouverture de session.)
goto status

:disable
schtasks /query /tn "AliceClockBoot" >nul 2>&1
if errorlevel 1 (
    echo   Deja desactivee.
    goto end
)
schtasks /delete /tn "AliceClockRecord" /f >nul 2>&1
schtasks /delete /tn "AliceClockBoot" /f >nul 2>&1
echo   OK : plus de correction automatique. Les fichiers restent dans %DIR%.
goto end

:status
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode Status
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Mode Test

:end
echo.
pause
exit /b

:: -- Saisie de la date et de l'heure reelles (PC sans internet) -----------------
:settime
set "NOW="
set /p "NOW=  Date et heure reelles, format JJ/MM/AAAA HH:MM : "
if not defined NOW exit /b
powershell -NoProfile -Command "Set-Date -Date ([DateTime]::ParseExact('%NOW%', 'dd/MM/yyyy HH:mm', $null)) | Out-Null"
if errorlevel 1 (
    echo   Format non reconnu, heure inchangee.
    exit /b
)
echo   OK : heure reglee sur %NOW%.
:: Le temoin repart tout de suite de cette heure.
schtasks /run /tn "AliceClockRecord" >nul 2>&1
exit /b
