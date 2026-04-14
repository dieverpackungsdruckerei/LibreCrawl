#!/bin/bash

echo "🚀 Starte sicheres Colima & Docker Setup für LibreCrawl..."

# Fix PATH for docker and docker-compose
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

# Check if Colima is installed
if ! command -v colima &> /dev/null; then
    echo "❌ Colima ist nicht installiert."
    exit 1
fi

# Check if Colima is running
if ! colima status &> /dev/null; then
    echo "⏳ Colima läuft noch nicht. Starte Colima (x86_64, 2 CPU, 12GB RAM)..."
    colima start --cpu 2 --memory 12 --arch x86_64 --vm-type=vz --mount-type=virtiofs
else
    echo "✅ Colima läuft bereits."
fi

# Start the LibreCrawl container (no rebuild — run docker-compose up -d --build manually after code changes)
echo "🐳 Starte LibreCrawl via Docker Compose..."
/usr/local/bin/docker-compose up -d

echo ""
echo "============================================================"
echo "✅ LibreCrawl läuft stabil im Colima Container!"
echo "🌐 UI erreichbar unter: http://localhost:5050"
echo "🛠 Du kannst die Logs mit folgendem Befehl ansehen:"
echo "   /usr/local/bin/docker-compose logs -f"
echo "============================================================"
