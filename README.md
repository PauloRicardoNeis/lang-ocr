# Lang

Projeto local para capturar, catalogar e estudar vocabulario em qualquer idioma a partir de screenshots, texto e revisao manual. O modulo `JP/` mantem o nome historico, mas o catalogo e poliglota: cada fonte, segmento, termo e card sabe a que idioma pertence, e o mesmo banco guarda japones, espanhol, ingles ou qualquer outro idioma ao mesmo tempo.

## O que o projeto faz

Este repositorio e uma base de estudo de idiomas orientada por fontes reais de uso. O fluxo principal e:

1. Registrar uma fonte bruta, normalmente um screenshot de jogo ou interface, ou um texto.
2. Preservar essa fonte no SQLite como verdade primaria, com imagem, texto bruto e contexto.
3. Detectar o idioma, separar texto misto por idioma e extrair ou revisar segmentos.
4. Ligar termos canonicos (e kanji, quando o idioma tem caracteres) a essa fonte revisada.
5. Calcular recorrencia e prioridade de estudo com base no que reaparece em fontes diferentes.
6. Gerar e sincronizar flashcards com Anki, um deck por idioma.

O plano, os requisitos e a lista de pendencias da versao poliglota estao em `polyglot-plan.md`; os comandos estao em `JP/README.md` (secao "Multiple languages").

O projeto nao e apenas um OCR nem apenas um gerador de flashcards. Ele funciona como um catalogo local de aprendizado, onde a imagem e a fonte bruta, o OCR gera rascunhos, e a canonizacao continua assistida por revisao humana.

## Componentes principais

- `JP/lang_jp.py`: CLI principal para inicializar banco, registrar fontes, ligar termos, revisar analises, recalcular estatisticas e gerar/sincronizar flashcards.
- `JP/languages.py`: registro de idiomas, deteccao offline de idioma, divisao de texto misto e analisadores por idioma (Sudachi para japones, tokenizador generico para idiomas com espacos, jieba para chines quando instalado).
- `JP/lang_jp_web.py`: interface web local para navegar por termos, kanji, fontes e cards, com filtro de idioma em todas as paginas e uma API JSON (`/api/languages`, `/api/terms`, `/api/sources`, `/api/flashcards`).
- `JP/capture_worker.py`: worker Python que consome jobs de captura, roda OCR local e gera rascunhos de analise para revisao.
- `JP/desktop_capture_daemon/`: daemon em Rust para captura passiva de tela, deduplicacao basica e enqueue de jobs.
- `JP/schema.sql`: schema SQLite que sustenta fontes, termos, segmentos, analise OCR, estatisticas e flashcards.
- `JP/data/lang_jp.sqlite3`: banco local do catalogo.

## Fluxo funcional atual

- Entrada manual:
  adicionar uma imagem ou texto como `source`, revisar o conteudo e ligar termos/kanji manualmente.
- Entrada assistida:
  capturar screenshots automaticamente, rodar OCR local, criar `sources` pendentes e aprovar ou ignorar depois.
- Catalogacao:
  manter termos canonicos, formas observadas, segmentos revisados e ligacoes entre fonte e vocabulario.
- Prioridade:
  usar recorrencia por fonte e ocorrencias agregadas para ordenar estudo.
- Saida de estudo:
  gerar cards de vocabulario e kanji e sincronizar com Anki via AnkiConnect ou exportar TSV.

## Arquitetura resumida

- Persistencia local em SQLite.
- Aplicacao principal em Python.
- UI local servida por HTTP no proprio processo Python.
- Captura passiva separada em dois estagios:
  daemon Rust para screenshot/enfileiramento + worker Python para OCR e analise linguistica.
- Integracao externa opcional com AnkiConnect para envio dos cards.
- Integracao externa opcional com `kanjiapi.dev` para enriquecer metadados de kanji.

## Estrutura do repositorio

- `README.md`: visao geral do projeto.
- `AGENTS.md`: convencoes operacionais para agentes.
- `setup_windows.ps1`: bootstrap para recriar os ambientes Python no Windows e rodar smoke tests.
- `open_lang.cmd`: ponto de entrada unico no Windows. Sobe o tray (que supervisiona a interface web e o worker de OCR) e abre o catalogo no navegador. Se o tray ja estiver rodando, so abre o catalogo.
- `JP/README.md`: detalhes do catalogo JP, comandos e fluxo de uso.
- `JP/ocr_capture_spec.md`: especificacao do pipeline de captura automatizada.
- `JP/ocr_capture_review.md`: anotacoes de revisao do fluxo de OCR/captura.
- `notes.md`: notas curtas de modelagem e direcao do projeto.

## Ponto de partida para revisao de codebase

Se outro agente ou auditor for revisar este repositorio, o ponto de entrada correto e:

1. Ler este arquivo para entender o objetivo do sistema.
2. Ler `AGENTS.md` para entender as convencoes do repo.
3. Ler `JP/README.md` para o fluxo operacional do catalogo.
4. Inspecionar `JP/lang_jp.py`, `JP/lang_jp_web.py`, `JP/capture_worker.py` e `JP/schema.sql` como nucleo funcional.

## Rebuild no Windows

Se voce precisou formatar a maquina, o caminho mais seguro hoje e:

1. Instalar Python 3.12 x64.
2. Instalar Git.
3. Instalar Rust/Cargo se for usar a captura passiva.
4. Rodar o bootstrap do repo:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 `
  -PythonExe "C:\Path\To\Python312\python.exe" `
  -CapturePythonExe "C:\Path\To\Python312\python.exe" `
  -InstallCaptureDeps `
  -BuildDaemon `
  -RunSmokeTests
```

Notas:

- O ambiente `.venv` antigo nao deve ser reaproveitado depois de trocar a instalacao base do Python.
- O catalogo principal usa `JP\requirements-main.txt`.
- O OCR/capture usa `JP\requirements-capture.txt` e continua mais estavel em Python 3.12.
- O repo agora tenta resolver caminhos antigos salvos no banco quando a raiz do projeto mudou apos reinstalacao.

## Estado atual

O repositorio parece estar centrado quase totalmente no catalogo japones em `JP/`. Nao ha, neste momento, evidencias de outros modulos equivalentes para outros idiomas dentro da raiz.
