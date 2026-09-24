# TubeKeeper

Lichtgewicht, zelf-gehost alternatief voor Pinchflat. Volgt YouTube-kanalen en -playlists,
controleert periodiek op nieuwe uploads en downloadt ze automatisch met **yt-dlp**, in een
mapstructuur die Plex/Jellyfin/Emby direct oppakken.

## Functies
- Kanalen, playlists of losse video's toevoegen via een web-UI (poort **8945**); naam leeg laten = kanaalnaam
- Per bron: max. kwaliteit (360p–4K/beste), video of alleen audio (m4a/mp3/opus), controle-interval
- Alleen nieuwe uploads, of ook de bestaande backlog (per stuk of in één keer)
- Filter "alleen uploads vanaf datum" en "bewaar alleen de nieuwste N" (oudere bestanden worden opgeruimd)
- Ondertitels embedden, metadata + hoofdstukken + thumbnail embedden, `.jpg` poster naast het bestand
- Abonnementen importeren vanuit je YouTube-account (cookies) of Google Takeout, optioneel automatisch bijhouden
- Meldingen via Pushover, Prowl, ntfy of Discord zodra iets gedownload is (of mislukt)
- Opslaggebruik per bron en in totaal
- Wachtrij met live voortgang, fouten opnieuw proberen, premières/livestreams worden later opgepakt
- yt-dlp wordt bij elke containerstart automatisch bijgewerkt
- Optioneel `cookies.txt` in `/config` voor leeftijdsbeperkte/members-only video's

Bestanden komen standaard in een Plex/Jellyfin-vriendelijke serie-indeling:

```
/downloads/<Bron>/poster.jpg
/downloads/<Bron>/Season 2026/<Bron> - S2026E092401 - Titel [videoId].mp4
```
Seizoen = uploadjaar, aflevering = maand+dag+volgnummer, dus Plex sorteert op uploaddatum.
Per bron kun je ook kiezen voor één map met de datum in de bestandsnaam. Wissel je van indeling
of hernoem je een bron, dan worden bestaande bestanden automatisch verplaatst.

## Plex instellen
1. **Bibliotheek toevoegen → TV-programma's**, map: je `/downloads`-map.
2. **Geavanceerd**: Scanner *Plex Series Scanner*, Agent *Personal Media Shows*.
3. Onder **Instellingen → Agents → Shows → Personal Media Shows**: *Local Media Assets* bovenaan.
Plex toont dan elk kanaal als serie met het kanaallogo als poster, jaren als seizoenen, en
thumbnails per aflevering.

## Installeren via Portainer

Portainer kan geen lokale bouwmap gebruiken in de web editor, dus kies één van deze routes:

### Optie A — Stack vanuit Git-repository (makkelijkst)
1. Zet deze map in een (privé) Git-repo (GitHub, Gitea, …).
2. Portainer → **Stacks → Add stack → Repository**.
3. Repository URL invullen, *Compose path*: `docker-compose.yml`.
4. Pas onder de volumes `/pad/naar/media/youtube` aan naar je mediamap (en eventueel PUID/PGID).
5. **Deploy the stack**. Portainer bouwt het image zelf.

### Optie B — Kant-en-klaar image via GitHub Actions
De workflow in `.github/workflows/docker.yml` bouwt bij elke push een image (amd64 + arm64)
naar `ghcr.io/richrdj/tubekeeper:latest`. Gebruik dan in Portainer → **Web editor**:

```yaml
services:
  tubekeeper:
    image: ghcr.io/richrdj/tubekeeper:latest
    container_name: tubekeeper
    restart: unless-stopped
    ports: ["8945:8945"]
    environment:
      - TZ=Europe/Amsterdam
      - PUID=1000
      - PGID=1000
    volumes:
      - /pad/naar/config/tubekeeper:/config
      - /pad/naar/media/youtube:/downloads
```
(Bij een privé-package: registry `ghcr.io` toevoegen in Portainer met een PAT.)

### Optie C — Zelf bouwen op de Docker-host
```
docker build -t tubekeeper:latest .
```
en daarna in Portainer de stack uit optie B gebruiken met `image: tubekeeper:latest`.

## Versies
Elke release krijgt een eigen image-tag, bv. `ghcr.io/richrdj/tubekeeper:1.0.0`. `:latest` is altijd de nieuwste release; `:edge` volgt `main` (testversie).

## Omgevingsvariabelen
| Variabele | Standaard | Uitleg |
|---|---|---|
| `TZ` | – | Tijdzone voor de UI |
| `PUID` / `PGID` | (root) | Gebruiker/groep die eigenaar wordt van de bestanden |
| `YTDLP_AUTO_UPDATE` | `true` | yt-dlp bijwerken bij start |
| `RECHECK_LIMIT` | `50` | Na de eerste volledige scan: hoeveel nieuwste items per controle |
| `OUTPUT_TEMPLATE` | zie boven | yt-dlp output-template (relatief t.o.v. de bronmap) |
| `LOG_LEVEL` | `INFO` | |

## Tips
- Krijg je "Sign in to confirm you're not a bot"? Exporteer YouTube-cookies (bv. met de extensie
  "Get cookies.txt LOCALLY") naar `/config/cookies.txt` en herstart de container.
- Jellyfin/Plex: voeg `/downloads` toe als bibliotheek van het type *Home videos / Other videos*.
