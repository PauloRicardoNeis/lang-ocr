# Auditoria Tecnica — Lang/JP Codebase

Data: 2026-03-29
Auditor: Claude Opus 4.6 (agente automatizado, a pedido do mantenedor)

---

## 0. Resumo do Projeto

**O que faz:** Catalogo local de estudo de japones baseado em fontes reais (screenshots de jogos). Fluxo principal: capturar imagem -> OCR -> registrar como source -> revisar -> ligar termos canonicos -> calcular prioridade -> gerar flashcards -> sincronizar com Anki.

**Stack:** Python 3.12+ (CLI/web/worker), Rust (daemon de captura), SQLite (persistencia), PaddleOCR + SudachiPy (analise linguistica), AnkiConnect (sync externo), `kanjiapi.dev` (metadados de kanji).

**Componentes centrais:**

- `lang_jp.py` (~4900 linhas): CLI monolitico — schema migrations, CRUD, stats, flashcards, Anki sync, argparse
- `lang_jp_web.py` (~2094 linhas): UI web local (HTTP server com ThreadingHTTPServer)
- `capture_worker.py` (~478 linhas): worker Python que consome capture_jobs, roda OCR+analise
- `desktop_capture_daemon/src/main.rs` (~652 linhas): daemon Rust de captura de tela com dedup, tray icon, enqueue de jobs
- `schema.sql`: schema SQLite autoritativo
- `test_capture_draft.py`: unico arquivo de testes

**Arquitetura percebida:** 3 processos independentes (daemon Rust -> worker Python -> CLI/web Python) comunicando via SQLite (tabela `capture_jobs` como fila). Sem IPC direto. O daemon Rust abre conexao rusqlite por operacao; o worker Python usa `db_connect` por transacao.

**Divergencias documentacao x codigo:**

- `ocr_capture_review.md` afirma que o web e GET-only e que nao existem `review-source`, `ignore-source`, `approve-analysis`. **Falso:** essas tres funcionalidades existem no codigo atual (`lang_jp.py:1874-1928`). O review doc esta desatualizado em relacao a implementacao.
- O review doc afirma que `add_source` recebe `argparse.Namespace` e nao e reutilizavel. **Parcialmente superado:** `create_source_record` ja existe como funcao reutilizavel (`lang_jp.py:463-518`).
- O web module **tem** endpoints POST (`/actions/sync-anki`, `/actions/sync-kanji-anki`), contradizendo o review que diz "GET-only".

---

## 1. Resumo Executivo

**Saude geral:** O codigo e funcional, coerente internamente e bem estruturado para um projeto pessoal de uso ativo. O schema e solido e as migrations aditivas sao conservadoras. O fluxo de dados principal (captura -> OCR -> revisao -> termos -> flashcards -> Anki) esta completo end-to-end.

**Principais riscos:**

1. Web module nao executa `ensure_runtime_schema` — pode quebrar em banco criado fora do CLI
2. Path traversal na servico de imagens via web
3. Ausencia quase total de testes (1 arquivo, 1 test case)
4. Blocos massivos de logica duplicada no Anki sync (term vs kanji)
5. Conexoes SQLite nao gerenciadas por context manager em varios pontos do capture_worker
6. Daemon Rust usa alpha dependency (`windows-capture = "2.0.0-alpha.7"`)

**Areas mais frageis:** Anki sync, web image serving, capture_worker error recovery, stats consistency under concurrent access.

**Nivel de confianca da analise:** Alto para Python (lido integralmente), alto para Rust (lido integralmente), alto para schema (lido integralmente).

---

## 2. Mapa do Sistema

```
Daemon Rust (captura de tela, dedup, spool)
    | [SQLite: capture_jobs INSERT + spool PNG]
Worker Python (OCR + analise linguistica)
    | [SQLite: sources INSERT + analysis segments/candidates]
CLI Python (revisao, link-term, stats, flashcards, Anki sync)
    | [HTTP: AnkiConnect]
Web Python (navegacao, busca, sync Anki via subprocess do CLI)
    | [subprocess -> lang_jp.py sync-anki-words / sync-anki-kanji]
```

**Pontos criticos de integracao:**

- SQLite e o unico canal entre daemon, worker, CLI e web
- AnkiConnect e chamado diretamente do CLI e indiretamente do web (via subprocess)
- `kanjiapi.dev` e chamada sincrona durante geracao de kanji flashcards

---

## 3. Achados

### Achado 1: Path Traversal na Servico de Imagens via Web

- **Severidade:** Critica
- **Categoria:** Seguranca
- **Onde:** `lang_jp_web.py:1973-1991` (`respond_source_image`)
- **Evidencia:** O path da imagem vem diretamente do banco (`source_path`), e convertido em `Path()` e lido com `read_bytes()` sem nenhuma validacao. O banco e populado pelo daemon, pelo worker e pelo CLI via `--path`. Qualquer `source_path` que aponte para um arquivo arbitrario no filesystem (ex: `C:\Users\paulorn\.ssh\id_rsa`) sera servido pelo web server.
- **Impacto pratico:** Um source_path malicioso ou acidentalmente errado permite ler qualquer arquivo legivel pelo processo Python via `GET /sources/{id}/image`.
- **Cenario de falha:** Um agent (AGENTS.md workflow) ou um `add-source --path "C:\Windows\System32\config\SAM"` registra um path arbitrario. O web serve o conteudo.
- **Por que acontece:** Nao ha validacao de que o path esta dentro de um diretorio permitido (ex: `data/captures/`).
- **O que validar depois:** Se o server precisa aceitar paths absolutos arbitrarios ou se pode restringir a um prefixo.

### Achado 2: Web Module Nao Executa `ensure_runtime_schema`

- **Severidade:** Alta
- **Categoria:** Inconsistencia entre modulos
- **Onde:** `lang_jp_web.py:525-531` vs `lang_jp.py:279-286`
- **Evidencia:** `lang_jp_web.py:db_connect()` faz `PRAGMA journal_mode = WAL`, `busy_timeout`, `foreign_keys`, mas **nao** chama `ensure_runtime_schema`. O `lang_jp.py:db_connect()` chama. Ambas as funcoes se chamam `db_connect` mas tem comportamento diferente.
- **Impacto pratico:** Se o banco foi criado por `init-db` numa versao antiga e o web e acessado antes do CLI rodar, colunas adicionadas por `ensure_runtime_schema` (ex: `ingest_mode`, `review_status`, `image_sha256`, `capture_job_id`) podem nao existir. O web module faz queries a `sources` sem selecionar essas colunas diretamente na maioria dos casos, mas `source_detail_page` usa `language_code` que e original. Entretanto, o dashboard faz subquery em `source_terms` que e seguro.
- **Cenario de falha:** Em banco recem-criado com schema antigo, o web acessa tabelas/views que dependem de colunas nao migradas, causando `OperationalError`.
- **Por que acontece:** Codigo duplicado de `db_connect` sem manter paridade.
- **O que validar depois:** Se o web alguma vez e executado sem que o CLI tenha rodado antes no mesmo banco.

### Achado 3: Trayicon ICO Memory Leak Permanente no Daemon Rust

- **Severidade:** Media
- **Categoria:** Memory management / codigo confuso
- **Onde:** `main.rs:497` — `Box::leak(bytes.into_boxed_slice())`
- **Evidencia:** `build_tray_icon_ico()` retorna `&'static [u8]` usando `Box::leak`, o que aloca ~1.3KB que nunca sera liberado. E chamado uma vez no startup do daemon.
- **Impacto pratico:** Insignificante em volume (1.3KB), mas e um padrao perigoso. Se alguem copiar o padrao para buffers maiores, sera um leak real.
- **Por que acontece:** A API de `trayicon` requer `&[u8]` com lifetime `'static`. O `Box::leak` e uma solucao valida para isso, mas deveria estar documentado.

### Achado 4: Race Condition na Dedup do Worker — Janela Entre `find_source_by_image_sha256` e `create_source_record`

- **Severidade:** Alta
- **Categoria:** Race condition / concorrencia
- **Onde:** `capture_worker.py:326-377`
- **Evidencia:** O worker verifica duplicata por sha256 numa conexao (`db_connect`), depois fecha essa conexao (sai do `with`), faz OCR, e depois abre **outra** conexao para criar o source. Entre a verificacao e a insercao, outro processo (CLI, agent, outro worker) pode inserir um source com o mesmo `image_sha256`. O schema nao tem UNIQUE constraint em `sources.image_sha256`.
- **Impacto pratico:** Duplicacao de sources quando daemon+worker+CLI operam concorrentemente sobre a mesma imagem.
- **Cenario de falha:** O daemon enfileira uma captura. O usuario manda a mesma imagem ao agent (AGENTS.md workflow). O agent insere o source. O worker nao detecta a duplicata porque verificou antes do agent inserir.
- **Por que acontece:** A verificacao de dedup e a insercao nao estao na mesma transacao. A tabela `sources` nao tem constraint UNIQUE em `image_sha256`.
- **O que validar depois:** Se a adicao de `UNIQUE(image_sha256)` ou check-and-insert atomico resolve.

### Achado 5: `refresh_all_source_kanji` Executado em Toda Geracao de Flashcards — O(N^2) Implicito

- **Severidade:** Alta
- **Categoria:** Performance / escalabilidade local
- **Onde:** `lang_jp.py:2818` (`upsert_flashcards`) e `lang_jp.py:3004` (`upsert_kanji_flashcards`)
- **Evidencia:** Ambos `upsert_flashcards` e `upsert_kanji_flashcards` chamam `refresh_all_source_kanji(connection)` que itera **todos** os source_ids com source_terms ou source_kanji, e para cada um chama `refresh_source_kanji` que por sua vez: deleta todos os source_kanji do source, recalcula, e depois chama `recalculate_kanji_stats` para **cada** kanji anterior e atual. `recalculate_kanji_stats` faz JOIN de `source_kanji -> source_terms -> terms` com `instr()`.
- **Impacto pratico:** Com 100 sources e 50 kanji, isso sao potencialmente milhares de queries com `instr()` scan. `sync-anki-words` e `sync-anki-kanji` executam isso antes de cada sync, mesmo que nada tenha mudado.
- **Cenario de falha:** Com catalogo crescendo, `sync-anki-words` fica cada vez mais lento. O web module chama sync via subprocess (que inclui geracao), potencialmente causando timeout percebido pelo usuario.
- **Por que acontece:** Nao ha dirty-tracking; o refresh e unconditional.
- **O que validar depois:** Se um flag "needs_kanji_refresh" por source reduziria o trabalho.

### Achado 6: `fetch_kanji_metadata` Usa `SystemExit` Para Erros Recuperaveis

- **Severidade:** Media
- **Categoria:** Tratamento de erro
- **Onde:** `lang_jp.py:2412` e `lang_jp.py:2415`
- **Evidencia:** `fetch_kanji_metadata` lanca `SystemExit` quando a API retorna erro. O caller `refresh_kanji_metadata` (`lang_jp.py:2489`) faz `except SystemExit: failed += 1; continue` — ou seja, captura `SystemExit` para continuar. Mas `SystemExit` e um anti-pattern como excecao de fluxo: se outro caller nao capturar, o processo inteiro morre por um kanji nao encontrado na API.
- **Impacto pratico:** Se `fetch_kanji_metadata` for chamado de um contexto que nao espera `SystemExit` (ex: futuro web endpoint), o server morre.
- **Por que acontece:** Pattern de CLI onde `SystemExit` e usado como mecanismo de exit geral.

### Achado 7: Anki Sync — Duplicacao Massiva de Logica entre `sync_anki` e `sync_anki_kanji`

- **Severidade:** Media
- **Categoria:** Debito tecnico / manutenibilidade
- **Onde:** `lang_jp.py:3869-4147` (`sync_anki`) vs `lang_jp.py:4160-4422` (`sync_anki_kanji`) — e `reprioritize_anki_deck` vs `reprioritize_anki_kanji_deck`
- **Evidencia:** Os dois metodos sao praticamente identicos (~280 linhas cada), diferindo apenas em: tabela (`flashcards` vs `kanji_flashcards`), join (`terms`/`term_stats` vs `kanji`/`kanji_stats`), e tratamento de duplicatas (soft skip vs hard `SystemExit`). A mesma logica de create/update note, changeDeck, sync tags, reprioritize esta copiada.
- **Impacto pratico:** Qualquer bug fix ou melhoria no sync de termos precisa ser manualmente replicado no sync de kanji, e vice-versa. Isso ja e visivel: `sync_anki` faz `skipped_duplicates += 1` quando Anki rejeita duplicate, mas `sync_anki_kanji` faz `raise SystemExit` — comportamento assimetrico sem razao aparente.
- **Cenario de falha:** Um kanji flashcard que coincide com um front_text existente mata o processo inteiro, enquanto o mesmo cenario em term flashcard e graciosamente ignorado.

### Achado 8: `capture_worker.process_job_once` Faz Multiplas Conexoes Curtas Sem Transacao Coesa

- **Severidade:** Media
- **Categoria:** Consistencia de dados
- **Onde:** `capture_worker.py:305-430`
- **Evidencia:** O fluxo de processamento de um job abre e fecha **5 conexoes separadas** em sequencia: (1) claim job, (2) check duplicate, (3) [OCR off-db], (4) create source + analysis + update job status, (5) catch: mark failed. Se o processo crashar entre (4) e o commit, o job fica em status `processing` com um source criado mas o job nao marcado como `accepted`. O retry vai reprocessar, criando potencialmente outra source.
- **Impacto pratico:** Crash recovery pode gerar source duplicado sem dedup (porque o source ja existe mas o job esta em `processing`, nao `accepted`).
- **Por que acontece:** Cada `with db_connect(db_path) as connection:` e uma transacao independente.
- **O que validar depois:** Se a insercao do source e a atualizacao do job devem estar na mesma transacao (ja estao em (4), que e bom), mas a verificacao de dedup em (2) nao esta.

### Achado 9: Web Sync Anki Via Subprocess Sem Timeout

- **Severidade:** Media
- **Categoria:** Robustez operacional
- **Onde:** `lang_jp_web.py:2004-2017` (`run_catalog_command`) e `lang_jp_web.py:2030-2065` (`run_sync_anki_action`)
- **Evidencia:** `subprocess.run()` e chamado sem `timeout=`. `sync-anki-words` pode levar minutos (gera flashcards + chama AnkiConnect para cada card + reprioritiza). O `ThreadingHTTPServer` mantem a thread do request bloqueada. Se Anki nao estiver aberto, `urlopen` tem timeout de 10s por request, mas o loop de retry pode fazer N x 10s.
- **Impacto pratico:** O botao "Sincronizar com o Anki" na UI pode travar o browser tab por minutos. O server thread fica preso. Se o usuario clicar de novo, outro subprocess inicia concorrentemente.
- **Cenario de falha:** Anki nao aberto + 200 flashcards pendentes = 200 x 10s timeout = ~33 minutos de processo preso.
- **Por que acontece:** Subprocess sem timeout e sem indicacao de progresso.

### Achado 10: `table_columns` Usa F-String com Nome de Tabela Nao Sanitizado

- **Severidade:** Baixa (uso interno)
- **Categoria:** Seguranca / SQL injection potencial
- **Onde:** `lang_jp.py:333-336`
- **Evidencia:** `f"PRAGMA table_info({table_name})"` — o `table_name` vem de strings hardcoded no codigo (`"flashcards"`, `"sources"`, `"kanji"`, etc.), nunca de input do usuario. Porem, se a funcao for reutilizada com input externo, ha SQL injection.
- **Impacto pratico:** Nenhum no uso atual. Risco de regressao se a funcao for reutilizada.

### Achado 11: `sha256_hex` no Daemon Calcula Hash do Raw Frame Buffer, Nao do Arquivo PNG

- **Severidade:** Media
- **Categoria:** Inconsistencia de dedup entre processos
- **Onde:** `main.rs:292` (hash do buffer) vs `capture_worker.py:313` (usa `job["image_sha256"]` que vem do daemon) vs `lang_jp.py:451-460` (`find_source_by_image_sha256` em sources)
- **Evidencia:** O daemon calcula SHA256 do **raw RGBA pixel buffer** antes de salvar como PNG. O `image_sha256` armazenado em `capture_jobs` e propagado para `sources` e portanto o hash dos pixels, nao do arquivo PNG. Se algum outro caminho de entrada (ex: `add-source --image-sha256`) calcular o hash do **arquivo** PNG em disco, os hashes nao vao coincidir para a mesma imagem. A compressao PNG nao e deterministica entre implementacoes diferentes.
- **Impacto pratico:** Dedup cross-fluxo pode falhar: o hash do daemon (pixels) vs hash de um arquivo PNG enviado via `add-source` (bytes do arquivo) serao diferentes.
- **Por que acontece:** Nao ha documentacao de qual representacao o `image_sha256` deve usar.
- **O que validar depois:** Se algum codigo externo ao daemon calcula `image_sha256` de forma diferente.

### Achado 12: `sql_like` No Web E Vulneravel a Wildcards em Input do Usuario

- **Severidade:** Baixa
- **Categoria:** Caso de borda
- **Onde:** `lang_jp_web.py:547-548`
- **Evidencia:** `sql_like` faz `f"%{value.strip()}%"`. Se o usuario buscar `%` ou `_` (wildcards do SQL LIKE), o resultado sera inesperado. Ex: buscar `_` vai retornar todos os termos cujo lemma contem pelo menos um caractere. Nao e um risco de seguranca (e parameterized), mas e um bug funcional.
- **Impacto pratico:** Busca com caracteres `%` ou `_` retorna resultados incorretos.

### Achado 13: Daemon Rust Nao Auto-Recupera Worker Apos Exit em Estado Running

- **Severidade:** Media
- **Categoria:** Robustez operacional
- **Onde:** `main.rs:193-224` (spawn_worker thread) e `main.rs:406` (WorkerExited handler)
- **Evidencia:** Quando o worker Python morre, a thread de monitoramento envia `UserEvent::WorkerExited`, que seta o estado para `WorkerError`. O usuario precisa clicar "Resume" ou "Start" no tray menu para reiniciar. Nao ha auto-restart nem backoff.
- **Impacto pratico:** Se o worker crashar durante uma sessao de jogo, as capturas continuam sendo enfileiradas mas nao processadas. O usuario pode nao perceber por horas.
- **Por que acontece:** Design deliberado (tray menu manual), mas sem indicacao visual forte de que o worker morreu.

### Achado 14: `approve_source_analysis_candidates` Seta `review_status = 'reviewed'` Mesmo Com Aprovacao Parcial

- **Severidade:** Media
- **Categoria:** Logica de negocio
- **Onde:** `lang_jp.py:1034-1041`
- **Evidencia:** Quando `candidate_ids` e fornecido (aprovacao parcial), a fonte e marcada como `reviewed` mesmo que outros candidatos ainda estejam `pending`. O status `reviewed` implica que a revisao esta completa.
- **Impacto pratico:** Uma fonte parcialmente aprovada desaparece da fila de revisao (`review-sources --status pending`), e os candidatos restantes ficam esquecidos.
- **Cenario de falha:** Usuario aprova 3 de 10 candidatos -> fonte some da fila -> 7 candidatos ficam como `pending` sem visibilidade.

---

## 4. Lacunas de Teste

**Situacao atual:** 1 arquivo de teste (`test_capture_draft.py`) com 1 caso de teste. O teste valida o fluxo de `approve_source_analysis_candidates` em cenario basico.

**Cenarios criticos sem cobertura:**

1. **Dedup cross-fluxo:** Nenhum teste verifica que `find_source_by_image_sha256` previne duplicatas entre daemon e CLI.
2. **Stats apos delete/ignore:** Nenhum teste verifica que `rebuild-stats` corrige `term_stats`/`kanji_stats` apos ignorar sources.
3. **Flashcard generation com conflito de front_text:** A logica de skip por `conflicting_front` nao e testada.
4. **Anki sync:** Zero testes para qualquer parte da integracao com AnkiConnect.
5. **Schema migration:** Nenhum teste verifica que `ensure_runtime_schema` funciona corretamente em bancos antigos.
6. **Concurrent access:** Nenhum teste de contencao SQLite entre daemon, worker e CLI.
7. **Web endpoints:** Zero testes para qualquer rota do web server.
8. **OCR normalization:** Nenhum teste para `normalize_paddle_result` com diferentes versoes do PaddleOCR.
9. **Romanizacao:** Nenhum teste para `romanize_kana`, que tem logica complexa de digraphs, pequeno tsu, n antes de vogal, chouon.
10. **Priority score calculation:** Nenhum teste verifica a formula de `recalculate_term_stats` ou `recalculate_kanji_stats`.

**O teste existente tem uma fragilidade:** usa `db_connect(self.db_path)` sem context manager no `setUp`, e faz `connection.close()` manual em vez de `with`. Se o teste falhar antes do `finally`, a conexao pode ficar aberta em modo WAL.

---

## 5. Riscos Arquiteturais

1. **Monolito `lang_jp.py` a ~4900 linhas:** Mistura concerns de schema migration, CRUD, stats, flashcard generation, Anki sync, CLI parsing, romaji conversion, HTML generation. Qualquer mudanca no schema requer entender o arquivo inteiro. A extracao de modulos (ex: `anki_sync.py`, `stats.py`) reduziria risco de regressao.

2. **Duplicacao `db_connect` entre modulos:** `lang_jp.py` e `lang_jp_web.py` tem funcoes `db_connect` com comportamento diferente. `capture_worker.py` importa do `lang_jp`. Qualquer mudanca no `db_connect` do CLI precisa ser avaliada contra o web. A divergencia e uma fonte provavel de bugs futuros.

3. **Web sync via subprocess:** O web chama `lang_jp.py` via subprocess para sync Anki. Isso significa que o web nao sabe se o sync esta em progresso, nao pode cancelar, e o CLI prints para stdout precisam ser parseados como strings para extrair resultado. Qualquer mudanca no formato de saida do CLI quebra o web.

4. **Dependencia alpha no Rust:** `windows-capture = "2.0.0-alpha.7"` e uma dependencia alpha. Breaking changes sao esperadas. O daemon pode parar de compilar apos `cargo update`.

5. **Sem politica de retencao de imagens:** Screenshots ficam no filesystem indefinidamente. Com captura a cada 15 segundos em sessoes longas, o volume de imagens cresce sem controle. Nao ha mecanismo de cleanup.

6. **`SystemExit` como mecanismo de erro em funcoes reutilizaveis:** Multiplas funcoes internas usam `raise SystemExit(...)` para erros que deveriam ser excecoes normais (`fetch_kanji_metadata`, `create_source_analysis_segment`, `ensure_source_segment`, etc.). Isso funciona no CLI mas e incompativel com uso como library.

---

## 6. Prioridade de Investigacao

| # | Problema | Justificativa |
|---|---------|---------------|
| 1 | **Path traversal no web image serving** (Achado 1) | Unico vetor de seguranca real. Permite leitura arbitraria de arquivos. Fix trivial. |
| 2 | **Web sem `ensure_runtime_schema`** (Achado 2) | Pode causar crash do web em banco nao migrado. Fix trivial (uma linha). |
| 3 | **Race condition dedup worker** (Achado 4) | Causa duplicacao de sources com impacto em stats e flashcards. Schema fix (UNIQUE constraint) + transacao coesa. |
| 4 | **`refresh_all_source_kanji` em todo sync** (Achado 5) | Performance degradante com crescimento do catalogo. Impacta usabilidade direta via web sync button. |
| 5 | **Aprovacao parcial marca fonte como reviewed** (Achado 14) | Causa perda silenciosa de candidatos pendentes. Impacta fluxo de revisao que e central ao projeto. |
| 6 | **Duplicacao massiva Anki sync** (Achado 7) | Ja causou divergencia de comportamento (skip vs SystemExit em duplicatas). Risco de regressao em cada mudanca. |
| 7 | **Web sync sem timeout** (Achado 9) | Thread do server pode ficar presa por dezenas de minutos. Impacta usabilidade. |
| 8 | **SHA256 pixels vs arquivo** (Achado 11) | Dedup cross-fluxo falhara se qualquer path externo usar hash de arquivo. |
| 9 | **Daemon nao auto-recupera worker** (Achado 13) | Captura continua mas processamento para sem aviso. Sessao de jogo inteira pode ser perdida. |
| 10 | **Cobertura de testes** (Lacunas) | 1 teste para ~7500 linhas de codigo Python. Qualquer refatoracao e arriscada sem rede de seguranca. |

**Por que esta ordem:** Os primeiros 3 sao bugs concretos que podem causar perda de dados ou exposicao de arquivos. Os itens 4-5 afetam o fluxo principal de uso. Os itens 6-9 sao debito tecnico com impacto operacional real. O item 10 e o multiplicador de risco de todos os outros — sem testes, qualquer fix pode introduzir regressao.
