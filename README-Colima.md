# LibreCrawl Colima & Docker Setup

Dieses Verzeichnis enthält LibreCrawl konfiguriert für **Colima** und **Docker Compose**.

## Starten nach einem System-Neustart (Reboot)

Docker-Container, die mit `restart: unless-stopped` konfiguriert sind, starten automatisch neu, *sobald die Docker-Engine (Colima) läuft*. Auf einem Mac startet Colima jedoch standardmäßig nicht von selbst nach einem Reboot.

Um LibreCrawl nach einem Neustart des Macs wieder hochzufahren, musst du **nur diesen einen Befehl** im Terminal ausführen:

```bash
cd /Users/Shared/LibreCrawl
./start-colima-librecrawl.sh
```

**Was dieses Skript macht:**
1. Es konfiguriert die Umgebungsvariablen (`PATH`), damit `colima` und `docker` gefunden werden.
2. Es prüft, ob Colima läuft, und startet es falls nötig mit angepassten, schlanken Ressourcen (2 CPU, 12GB RAM).
3. Es führt `docker-compose up -d` aus, um sicherzustellen, dass die Container gestartet werden.

Nach Ausführung des Skripts läuft LibreCrawl auf: **http://localhost:5050**

> **Port-Mapping:** Docker mappt Host-Port `5050` auf Container-Port `5000`. Die Anwendung lauscht intern auf Port 5000 (Waitress WSGI), ist von außen aber über Port 5050 erreichbar.

## Logs einsehen

Falls LibreCrawl unerwartetes Verhalten zeigt, kannst du die Live-Logs der Container prüfen:

```bash
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
docker-compose logs -f
```

## Setup stoppen

Möchtest du LibreCrawl temporär anhalten (ohne Datenverlust):
```bash
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
docker-compose stop
```

Oder falls du die gesamte Docker-/Colima-VM herunterfahren willst, um Ressourcen zu sparen:
```bash
colima stop
```
