@echo off
setlocal
:: Lancement automatique du build Unity a chaque demarrage du PC.
:: A poser A COTE de l'exe du build (ImaginAction.exe), puis double-cliquer.
:: Pas besoin d'etre administrateur. Relancer ce fichier pour activer/desactiver.

set "TASK=AliceUnity"
if /i "%~1"=="run" goto run

title Lancement automatique d'Alice
set "SELF=%~f0"

:: Exe du build : ImaginAction.exe a cote de ce fichier, sinon le premier .exe trouve.
set "EXE="
if exist "%~dp0ImaginAction.exe" set "EXE=%~dp0ImaginAction.exe"
if not defined EXE for %%F in ("%~dp0*.exe") do if /i not "%%~nxF"=="UnityCrashHandler64.exe" if not defined EXE set "EXE=%%~fF"

schtasks /query /tn "%TASK%" >nul 2>&1
if errorlevel 1 (set "STATE=DESACTIVE") else (set "STATE=ACTIVE")

echo.
echo   Lancement automatique d'ImaginAction au demarrage : %STATE%
echo.
choice /c ON /n /m "  Lancer ImaginAction automatiquement a chaque demarrage ? [O]ui / [N]on : "
if errorlevel 2 goto disable

:enable
if not defined EXE set /p "EXE=  ImaginAction.exe introuvable ici, chemin complet : "
if defined EXE set "EXE=%EXE:"=%"
if not exist "%EXE%" (
    echo   Exe introuvable : "%EXE%"
    goto end
)

:: Tache planifiee "a l'ouverture de session" : Unity tourne dans la session
:: de l'utilisateur (son, carte graphique), 20 s apres le demarrage pour laisser
:: le reseau et la carte son se lancer.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; $arg='/c start '+$q+'AliceUnity'+$q+' /min cmd /c '+$q+$q+$env:SELF+$q+' run '+$q+$env:EXE+$q+$q; $a=New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $arg; $t=New-ScheduledTaskTrigger -AtLogOn -User ($env:USERDOMAIN+'\'+$env:USERNAME); $t.Delay='PT20S'; $s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew; Register-ScheduledTask -TaskName $env:TASK -Action $a -Trigger $t -Settings $s -Force | Out-Null"
if errorlevel 1 (
    echo   ECHEC de la creation de la tache planifiee.
    goto end
)
echo.
echo   OK : ImaginAction se lancera 20 s apres chaque demarrage du PC.
echo   Exe : %EXE%
echo   S'il plante, il est relance automatiquement.
echo.
choice /c ON /n /m "  Le lancer maintenant ? [O]ui / [N]on : "
if errorlevel 2 goto end
schtasks /run /tn "%TASK%" >nul
goto end

:disable
schtasks /query /tn "%TASK%" >nul 2>&1
if errorlevel 1 (
    echo   Deja desactive.
    goto end
)
schtasks /delete /tn "%TASK%" /f >nul
echo   OK : ImaginAction ne se lancera plus au demarrage.
if not defined EXE goto end
for %%F in ("%EXE%") do set "EXENAME=%%~nxF"
tasklist /fi "imagename eq %EXENAME%" | find /i "%EXENAME%" >nul || goto end
echo.
choice /c ON /n /m "  ImaginAction tourne en ce moment. Le fermer ? [O]ui / [N]on : "
if errorlevel 2 goto end
taskkill /im "%EXENAME%" /f >nul
goto end

:end
echo.
pause
exit /b

:: -- Lanceur (appele par la tache planifiee, fenetre reduite) ------------------
:: Relance Unity s'il plante. S'arrete si Unity est quitte normalement (code 0)
:: ou si le lancement auto a ete desactive entre-temps.
:run
title AliceImaginAction
:loop
start "" /wait /d "%~dp2." "%~2"
set "CODE=%errorlevel%"
schtasks /query /tn "%TASK%" >nul 2>&1 || exit
if "%CODE%"=="0" exit
timeout /t 5 /nobreak >nul
goto loop
