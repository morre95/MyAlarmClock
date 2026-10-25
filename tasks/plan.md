# Plan: larmstyrning

Utgår från användarens godkända `SPEC-alarm-control.md`.

En enda synkron styrloop äger larmets timers och ljudkälla. Terminalindata
läses med timeout så att väntan inte blockerar automatisk avstängning.
Spelargränsen är `start()`, `pause()`, `resume()` och `check()`; metoderna
kastar undantag vid fel. `check()` övervakar en ägd lokal process, utan
fortlöpande Spotify-anrop. Spotify-adaptern bekräftar tillstånd på vald enhet.
MP3-adaptern äger endast sin egen mpv-process och låter inte mpv läsa
larmets terminalindata. Inga nya externa beroenden.

Byggordning och verifiering finns i `todo.md`: styrloop → ljudkällor → CLI
och granskning. Skriv fallerande offlinetester före varje steg. Monoton,
simulerad tid verifierar snooze och den oföränderliga stopptiden snabbt.

Risker: nätverksanrop kan försena kommandon tills anropet avslutas;
förfrågningar och statusbekräftelser har begränsade väntetider. Ett
misslyckat Spotify-startförsök kan ha nått spelaren: behåll ägarskapet och
försök pausa även denna spelare när reserven används. Fel får inte dölja
att tystnad inte kunde bekräftas. Processdöd/strömavbrott kan inte hanteras
av en timer i samma process, vilket står i hjälptexten.

Ingen verklig Spotify-uppspelning eller befintlig tokenfil används av testerna.
