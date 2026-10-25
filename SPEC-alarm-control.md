# Förslag: stopp, snooze och automatisk stopptid

Status: beteendet godkänt av användaren 2026-10-25; implementerat och
verifierat offline med 43 tester. Verklig Spotify-/MP3-uppspelning återstår
att kontrollera manuellt.

## Mål och omfattning

Lägg till ett sammanhängande larmläge i det befintliga terminalskriptet.
Användaren ska kunna stoppa eller snooza ett pågående larm, och larmet ska
stängas av automatiskt. Läget ska styra både Spotify och en eventuell lokal
MP3-reserv. Ingen webbapp, schemaläggare, Home Assistant eller Google Calendar
ingår i denna ändring.

Det befintliga starttestet och `--diagnose` behåller sitt beteende: de lämnar
Spotify igång efter att skriptet avslutats. Det nya läget väljs med `--alarm`
och håller processen igång tills larmet stängs av.

## Användning och kontrakt

Aktivera projektets miljö:

```bash
source .venv/bin/activate
```

Starta ett larm med 9 minuters snooze och automatisk avstängning efter
30 minuter, vilket också är standardvärdena:

```bash
python spotify_test.py --alarm \
  --device 'Web Player (Chrome)' \
  --playlist 'https://open.spotify.com/playlist/DITT_ID' \
  --snooze-minutes 9 --auto-stop-minutes 30
```

`--delay` och `--fallback` kan användas som tidigare. Tidsvärdena för snooze
och automatisk avstängning anges i minuter och måste vara ändliga tal större
än noll. Decimaler tillåts för korta tester. Larmläget kan inte kombineras med
`--diagnose`, `--login`, `--list-devices` eller `--test-fallback`.

Medan larmläget körs skriver användaren ett kommando och trycker på Enter:

| Kommando | Resultat |
| --- | --- |
| `stop` | Stäng av larmet och avbryt eventuell väntande snooze. |
| `snooze` | Tysta larmet och återuppta efter inställd snoozetid. |
| Ctrl+C | Samma avslutning av larmet som `stop`. |

Kommandona fungerar även under väntan före start och under snooze. `stop`
under startfördröjningen avbryter starten utan att pausa annan Spotify-musik.
`snooze` före första starten gör ingenting och ger en förklarande utskrift.
Ogiltiga kommandon ger hjälptext utan att ändra larmet. Stängd standardindata
avslutar larmläget med samma städförsök som `stop`; kommandoläget kräver därför
öppen terminalindata. Utskriften kan fortfarande skickas genom `tee`.

## Tidsregler och uppspelning

- Automatisk avstängning räknas från första bekräftade Spotify-starten, eller
  från starten av MP3-reserven om Spotify-starten misslyckas.
- Stopptiden är en gräns för hela larmtillfället och inkluderar snoozetid.
  Varken snooze eller återupptagen uppspelning flyttar den gränsen.
- Om stopptiden infaller under snooze avslutas larmet utan återstart.
- `snooze` under pågående snooze börjar om snoozeväntan men förlänger inte
  den automatiska stopptiden.
- Spotify pausas på den valda enhetens ID. Snooze återupptar befintlig
  uppspelning där, utan att starta om spellistan från första låten.
- MP3-processen avslutas vid stopp eller snooze. Vid snoozens slut startas
  reservfilen igen från början. Endast processer som larmet självt startat
  får avslutas. Ett oväntat processavslut rapporteras som fel, även om det
  inträffar efter start eller efter snooze.
- Använd monotona tidsgränser så att ändrad systemtid inte flyttar timers.
  Skriptet och datorn måste vara igång; automatisk avstängning kan inte
  garanteras om processen dödas eller datorn stängs av.

## Fel och säkerhetsgränser

- Tokens, autentiseringsheaders och råa API-fel får inte skrivas ut.
- Bekräfta Spotify-paus med statusavläsning inom en begränsad väntetid.
  Om paus inte kan bekräftas ska användaren få ett tydligt fel och uppmanas
  att kontrollera Spotify. Ett API-fel får aldrig beskrivas som lyckat stopp.
- Stopp avbryter alltid väntande återstarter, även om Spotify inte går att
  pausa. Stopprelaterade fel får inte aktivera MP3-reserven.
- En misslyckad snoozepaus får inte schemalägga en återstart. Misslyckad
  återupptagning rapporteras tydligt; inga obegränsade återförsök.
- Ändra inte spelarens volym. Återanvänd befintlig autentisering och scopes.
- Återanvänd Spotipy 2.25.1 och standardbiblioteket; inga nya beroenden behövs.
- Ändra inte tokenfiler eller befintliga diagnostikloggar under utveckling.

Spotify-kontraktet bygger på `pause_playback(device_id=...)`,
`start_playback(device_id=...)` för återupptagning och `current_playback()`
för bekräftelse enligt [Spotipy 2.25.1](https://spotipy.readthedocs.io/en/2.25.1/index).

## Projektstruktur och kodstil

`spotify_test.py` innehåller kommandoradsval och befintlig Spotify-integration.
Larmlivscykeln finns i `alarm_control.py` för att hålla timers och
spelarkontroll begripliga. Tester finns bredvid befintliga tester i
`test_alarm_control.py`. Körkommandona dokumenteras i skriptets hjälptext.
Behåll projektets enkla Python-stil med namngivna funktioner, fyra mellanslag
och svenska användarmeddelanden, exempelvis:

```python
def stop_alarm(player):
    player.pause()
    print("Larmet är avstängt.", flush=True)
```

Exemplet visar stil; felhantering och bekräftelse krävs enligt reglerna ovan.

## Verifiering och acceptanskriterier

Kör hela offlinesviten med:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest -v
git diff --check
```

Skriv först beteendetester som fallerar, implementera i små steg och behåll
alla befintliga diagnostiktester. Simulerad tid gör att inga tester behöver
vänta nio eller trettio minuter. Fejka endast externa gränser som Spotify,
terminalindata och MP3-processen. Tester får inte starta verklig musik.

Ändringen är klar när tester visar att:

1. Stoppa under startfördröjning hindrar första starten.
2. Stoppa under uppspelning pausar vald Spotify-enhet eller avslutar egen MP3.
3. Snooze tystar, väntar rätt tid och återupptar rätt ljudkälla.
4. Stoppa under snooze hindrar alla senare återstarter.
5. Automatisk avstängning fungerar både under uppspelning och under snooze,
   och förlängs inte av upprepade snoozekommandon.
6. Ctrl+C och stängd standardindata avbryter väntan och försöker tysta larmet.
7. Fel vid paus eller återupptagning rapporteras utan tokens och ger ingen
   oavsiktlig reservuppspelning eller återstart.
8. Ogiltiga tider och oförenliga kommandoradsval avvisas före uppspelning.
9. Det gamla starttestet och diagnostikläget fortfarande fungerar som tidigare.

Slutlig manuell kontroll kan göras med exempelvis `--auto-stop-minutes 1`
och `--snooze-minutes 0.1` innan längre larm testas. Rapportera separat vad
som verifierats offline respektive med verklig Spotify-uppspelning.

## Gränser för arbetet

- Alltid: verifiera beteende med tester, uppdatera hjälptext, håll hemligheter
  utanför Git och loggar.
- Förankra först: nya externa tjänster, beroenden eller ändrad omfattning till
  webbapp eller systeminstallation.
- Aldrig: ta bort eller försvaga befintliga tester för att få grönt, stoppa
  godtyckliga ljudprocesser eller påstå att ljudet tystnat när bara ett
  API-anrop har accepterats.

Öppna frågor: inga. Standardvärden och terminalflödet ovan är godkända.
