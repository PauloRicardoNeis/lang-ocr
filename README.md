# Lang

Projeto local para capturar, catalogar e estudar vocabulario japones a partir de screenshots, texto e revisao manual, com foco atual no modulo `JP/`.

## O que o projeto faz

Hoje este repositorio e, na pratica, uma base de estudo de japones orientada por fontes reais de uso. O fluxo principal e:

1. Registrar uma fonte bruta, normalmente um screenshot de jogo ou interface.
2. Preservar essa fonte no SQLite como verdade primaria, com imagem, texto bruto e contexto.
3. Extrair ou revisar segmentos de texto japones.
4. Ligar termos canonicos e kanji a essa fonte revisada.
5. Calcular recorrencia e prioridade de estudo com base no que reaparece em fontes diferentes.
6. Gerar e sincronizar flashcards com Anki.

O projeto nao e apenas um OCR nem apenas um gerador de flashcards. Ele funciona como um catalogo local de aprendizado, onde a imagem e a fonte bruta, o OCR gera rascunhos, e a canonizacao continua assistida por revisao humana.

## Componentes principais

- `JP/lang_jp.py`: CLI principal para inicializar banco, registrar fontes, ligar termos, revisar analises, recalcular estatisticas e gerar/sincronizar flashcards.
- `JP/lang_jp_web.py`: interface web local para navegar por termos, kanji, fontes e cards.
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
- `open_jp_catalog.cmd`: atalho Windows para abrir a interface web local.
- `open_jp_capture_tray.cmd`: atalho Windows para abrir o tray de captura passiva.
- `open_jp_capture_logs.cmd`: atalho Windows para acompanhar o log de captura em tempo real.
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

## Estado atual

O repositorio parece estar centrado quase totalmente no catalogo japones em `JP/`. Nao ha, neste momento, evidencias de outros modulos equivalentes para outros idiomas dentro da raiz.
