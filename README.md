# D-Hub

Eine Oberflaeche fuer alle Spiele-Launcher (Steam, Epic, GOG, Ubisoft, EA, Battle.net).

## Einmal einrichten
1. Oeffentliches GitHub-Repository anlegen (z. B. `D-Hub`) und alle Dateien hochladen,
   auch `.github/workflows/build.yml`, `installer.iss`, `AGB.txt`, `update.json`, `news.json`, `logo.png`, `logo.ico`.
2. Warten (ca. 4 Minuten). GitHub baut selbst und legt unter **Releases** ab:
   - `D-Hub-Setup.exe`: der Installer, **das schickst du an andere**
   - `D-Hub.exe` und `dhub.py`: werden nur fuer die automatischen Updates gebraucht

## Wie alle automatisch Updates bekommen
- Du ersetzt `dhub.py` im Repository durch eine Datei mit hoeherer `VERSION`. GitHub baut ein neues Release.
- Jedes D-Hub prueft beim Start und danach stuendlich auf neue Versionen und installiert sie beim Start von selbst
  (12 Sekunden Vorwarnung mit Abbrechen-Knopf; der Nutzer kann das in den Einstellungen abschalten).
- Die Datei `update.json` ist dein Steuerpult: `min_version` + `force: true` erzwingt das Update bei allen aelteren Versionen,
  `paused: true` ist die Notbremse, `message` ist ein Text an die Nutzer.
- Bequem bedienst du das mit `D-Hub-Admin.html` (gehoert NICHT ins Repository, nur auf deinen PC).

## Neuigkeiten an alle
`news.json` bearbeiten (oder im Admin-Fenster senden). Sie erscheinen im Briefsymbol.

## Hinweise
- Windows warnt bei nicht signierten Programmen: **Weitere Informationen > Trotzdem ausfuehren**.
- Wer in das Repository schreiben darf, kann Code an alle Nutzer ausliefern. Zwei-Faktor-Anmeldung aktivieren,
  und das Admin-Token nur fuer dieses Repository und mit kurzer Laufzeit erstellen.
- Den Text in `AGB.txt` bitte pruefen und anpassen (Name, Kontakt), bevor du D-Hub oeffentlich verteilst.
