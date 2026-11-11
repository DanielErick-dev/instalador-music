# 🎶 Instalador de Músicas

Baixa músicas do YouTube em MP3 (com capa, título e artista) por uma interface web local.

## Como usar

```bash
uv sync                 # instala/atualiza o ambiente (só na 1ª vez ou após atualizar)
uv run instalador       # abre a interface web em http://localhost:8765
uv run musica <links>   # modo terminal (mesmo motor)
```

Requisitos do sistema: `ffmpeg` (`sudo apt install ffmpeg`) e `node` ou `deno` (desafios do YouTube).

## Recursos

- **Fila com barra de progresso** por música e geral, com velocidade, tempo restante, cancelar e tentar de novo.
- **Destino visível**: caminho completo, equivalente no Windows (`D:\...`), espaço livre e nº de músicas. Aceita colar caminho do Windows (`D:\playlist romantica`). Destinos favoritos.
- **Manutenção de links**: tira `si`, `t`, `pp`, `feature`, `utm_*`...; converte youtu.be/shorts/music.youtube; Mix (`list=RD...`) nunca é listado, só o vídeo é baixado; duplicados na lista são ignorados; playlists inteiras só com opção ligada.
- **Sem duplicatas**: antes de baixar compara com a pasta pelo ID do vídeo (gravado nas tags) e pelo título; na aba Biblioteca agrupa repetidas (mesmo vídeo, arquivo idêntico ou mesmo título) e manda para uma lixeira recuperável (`~/.local/share/instalador-music/lixeira`).
- **Nomes limpos**: remove "(Official Video)", "[Lyric Video]", "⧸⧸ Sub Español", caracteres de largura total; nomes compatíveis com pendrive FAT32.
- **Limpeza** de arquivos temporários (`.part`, `.ytdl`, capas soltas).
- **Pendrives no WSL**: detecta pendrives do Windows e mostra o comando para montar (`sudo mount -t drvfs D: /mnt/d`).

## Segurança

- O servidor escuta só em `127.0.0.1` e rejeita outros `Host` (proteção contra DNS rebinding).
- Toda chamada da API exige um token aleatório gerado a cada execução (proteção contra CSRF).
- Só aceita pastas dentro de `~`, `/mnt` e `/media`; remoções só dentro da pasta analisada, e vão para a lixeira.
- Cabeçalhos CSP, `X-Frame-Options` e `nosniff`.

## Atualizações

O YouTube muda com frequência; se os downloads começarem a falhar, atualize o yt-dlp:

```bash
uv lock --upgrade-package yt-dlp && uv sync
```

Atualizar tudo e auditar vulnerabilidades conhecidas:

```bash
uv lock --upgrade && uv sync && uv run pip-audit
```

A aba **Ajustes → Versões e segurança** mostra o que está desatualizado.

## Desenvolvimento

```bash
uv run pytest
uv run ruff check src tests
```
