@echo off
setlocal
:: Desactive l'ancien service Python (NSSM : AliceForet, AliceBanquet...) qui
:: lancait run_scenario.py au demarrage. A faire sur chaque PC passe a Unity,
:: sinon le script Python et Unity pilotent les memes ESP en meme temps.
:: Utilise sc.exe : marche meme si nssm n'est pas dans le PATH.

:: Droits administrateur obligatoires : relance elevee si besoin.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

title Ancien service Python d'Alice
echo.
set "FOUND="
for /f "tokens=2" %%S in ('sc query state^= all ^| findstr /b /c:"SERVICE_NAME: Alice"') do (
    set "FOUND=1"
    call :handle %%S
)
if defined FOUND goto done
echo   Aucun service "Alice..." sur ce PC : rien a faire.
echo.
set "NAME="
set /p "NAME=  Nom d'un service a traiter quand meme (Entree pour quitter) : "
if defined NAME call :handle "%NAME%"

:done
echo.
pause
exit /b

:: -- Traitement d'un service -------------------------------------------------
:handle
set "SVC=%~1"
sc query "%SVC%" >nul 2>&1
if errorlevel 1 (
    echo   Service "%SVC%" introuvable.
    exit /b
)
set "STATE=?"
set "START=?"
for /f "tokens=4" %%A in ('sc query "%SVC%" ^| findstr /c:"STATE"') do set "STATE=%%A"
for /f "tokens=4" %%A in ('sc qc "%SVC%" ^| findstr /c:"START_TYPE"') do set "START=%%A"

echo   ---------------------------------------------
echo   Service : %SVC%
echo   Etat    : %STATE%   (demarrage : %START%)
echo.
echo     [D] Desactiver  : arrete et ne redemarre plus, mais garde (reactivable)
echo     [S] Supprimer   : arrete et supprime definitivement
echo     [R] Reactiver   : redemarre a chaque allumage, et maintenant
echo     [I] Ignorer     : ne rien changer
echo.
choice /c DSRI /n /m "  Choix [D/S/R/I] : "
if errorlevel 4 exit /b
if errorlevel 3 goto reactivate
if errorlevel 2 goto remove

:disable
sc stop "%SVC%" >nul 2>&1
sc config "%SVC%" start= disabled >nul
if errorlevel 1 ( echo   ECHEC de la desactivation. ) else ( echo   OK : %SVC% arrete et desactive. )
echo.
exit /b

:remove
sc stop "%SVC%" >nul 2>&1
timeout /t 2 /nobreak >nul
sc delete "%SVC%" >nul
if errorlevel 1 ( echo   ECHEC de la suppression. ) else ( echo   OK : %SVC% supprime. )
echo.
exit /b

:reactivate
sc config "%SVC%" start= auto >nul
sc start "%SVC%" >nul 2>&1
if errorlevel 1 ( echo   OK : %SVC% reactive au demarrage, mais il n'a pas pu etre lance maintenant. ) else ( echo   OK : %SVC% reactive et lance. )
echo.
exit /b
