# Revisão Crítica: ocr_capture_spec.md — v2

## Contexto

Revisão do plano de implementação em `JP/ocr_capture_spec.md` contra o código real
em `JP/lang_jp.py`, `JP/schema.sql`, `JP/lang_jp_web.py`, `AGENTS.md` e `notes.md`.

Esta é a v2, incorporando meta-revisão externa que identificou erros de leitura do
spec, lacunas não detectadas e exageros de severidade na v1. Os findings removidos
estão listados no final com justificativa.

---

## Findings — Ordenados por Severidade

---

### CRÍTICO 1 — RF7 não tem suporte no código; nenhum caminho de mutação existe

**Seção do spec:** RF7 ("Revisão posterior"), "Interface de Revisão" → "Ações esperadas"

**Arquivo:** `lang_jp.py:1493` (lista completa de subcomandos), `lang_jp_web.py:1093–1118`

O spec promete as seguintes ações de revisão:

- editar `raw_text`
- ajustar `scene`
- marcar `notes`
- ignorar captura
- chamar fluxo de promoção para termos

Nenhuma dessas operações existe no código atual. `lang_jp.py` não tem
`update-source`, `delete-source`, `ignore-source` nem `edit-source`. Os subcomandos
existentes são: `init-db`, `add-source`, `upsert-term`, `link-term`, `rebuild-stats`,
`list-sources`, `list-terms`, `list-kanji`, `generate-flashcards`, `list-flashcards`,
`export-anki-tsv`, `push-anki`, e `browse`. `lang_jp_web.py` só responde a GET —
não processa POST para nenhum endpoint.

Isso significa que o fluxo de revisão descrito na Fase 5 e no backlog de Revisão
depende de infraestrutura de escrita que não existe e não está especificada. O spec
não lista esses comandos nos requisitos funcionais nem no backlog de implementação.

**Ação necessária:** RF7 deve ser traduzido em subcomandos concretos antes da Fase 5
entrar no planejamento. Mínimo para o MVP de revisão:
- `update-source --id N [--raw-text ...] [--scene ...] [--notes ...]`
- `delete-source --id N` com recálculo de stats dos terms afetados

---

### ALTO 2 — Fase 4 (dedupe) sequenciada depois da Fase 3 (ingestão)

**Seção do spec:** "Planejamento de Implementação" → Fase 3 e Fase 4

O spec propõe: primeiro implementar ingestão automática (Fase 3), depois dedupe
(Fase 4). Durante o intervalo entre as duas fases, toda captura automática vai ao
banco sem filtro — menus estáticos a 30 segundos por 1 hora geram 120 fontes
idênticas ou quase idênticas.

O banco estará poluído antes do dedupe existir. E o mecanismo de limpeza
(`delete-source`) também não existe ainda (ver CRÍTICO 1) — a poluição não tem saída.

**Correção:** hash SHA-256 exato da imagem como gate mínimo de dedupe deve ser parte
da Fase 3, não da Fase 4. A Fase 4 adiciona phash e similaridade de texto sobre
essa base.

---

### ALTO 3 — Lifetime de conexão do daemon indefinido; contenção de lock não tratada

**Seção do spec:** backlog "Qualidade" → "teste de escrita concorrente no SQLite"

**Arquivo:** `lang_jp.py:79–84`, `lang_jp_web.py:319–323`

O padrão do repositório é conexão curta por operação (`with db_connect(...) as
connection`). Um daemon de captura não pode seguir esse padrão diretamente: ele
precisa decidir entre manter uma conexão de longa duração ou reconectar a cada
gravação.

Se o daemon mantiver uma conexão aberta entre capturas e o usuário rodar `link-term`
ou `browse` ao mesmo tempo, há contenção real de lock. SQLite no modo journal DELETE
(padrão atual) serializa todos os acessos. Sem `PRAGMA busy_timeout`, uma thread
bloqueada pelo writer falha imediatamente com `database is locked`.

`lang_jp_web.py` usa `ThreadingHTTPServer` e abre conexão curta por request
(`lang_jp_web.py:440, 823, 874, 1146`) — isso mitiga mas não elimina o problema se
o daemon tiver uma conexão aberta.

O spec menciona o teste como item de backlog mas não define:
- se o daemon vai usar conexão persistente ou por-gravação
- se `PRAGMA busy_timeout` deve ser configurado
- se `PRAGMA journal_mode=WAL` deve ser ativado

**Ação necessária:** definir explicitamente no spec o modelo de conexão do daemon e
adicionar `PRAGMA busy_timeout` como requisito não funcional.

---

### ALTO 4 — Stats ficam corrompidas após delete de source; cascade FK não repara

**Arquivo:** `schema.sql:49, 89` (FK ON DELETE CASCADE), `lang_jp.py:281, 456, 626`

`term_stats` e `kanji_stats` são calculados apenas em código explícito
(`recalculate_term_stats`, `recalculate_kanji_stats`, `rebuild_stats`). Quando uma
`source` é deletada, o FK cascade remove silenciosamente as linhas de `source_terms`
e `source_kanji` — mas nenhum trigger recalcula `term_stats` e `kanji_stats`.

O fluxo de revisão proposto inclui "ignorar captura" — que implica deletar ou
desabilitar sources. Se 50 capturas duplicadas de um menu forem deletadas em lote,
`term_stats` mostrará contagens infladas que `list-terms` usará para priorização até
o próximo `rebuild-stats` manual. O problema é silencioso e fácil de ignorar.

**Ação necessária:** qualquer operação de delete ou ignore de source deve chamar
`rebuild_stats` ou recalcular os term_ids afetados antes de deletar. O spec deve
documentar isso explicitamente.

---

### ALTO 5 — Assimetria de migrations: `lang_jp_web.py` não chama `ensure_runtime_schema`

**Arquivo:** `lang_jp.py:128–147`, `lang_jp_web.py:319–323`

O repositório já usa migrations aditivas em runtime via `ensure_runtime_schema`,
chamado em cada `db_connect` do CLI. O web module tem seu próprio `db_connect` que
**não chama** `ensure_runtime_schema`.

Se novos campos (ex.: `review_status`, `image_hash`) forem adicionados via
`ensure_runtime_schema`, eles só existirão no banco após a primeira conexão via CLI.
Se o web for iniciado antes do CLI, ou se o banco for criado via web, colunas
esperadas podem não existir.

O spec diz "MVP sem migração" como princípio, mas `ensure_runtime_schema` já é o
mecanismo de migração do projeto. A afirmação do spec é parcialmente falsa — o
mecanismo existe. O risco concreto é adicionar colunas no CLI sem atualizar o web.

**Ação necessária:** o spec deve reconhecer `ensure_runtime_schema` como mecanismo de
migração vigente. Qualquer coluna nova crítica deve ser adicionada ali, e o web
module deve ser verificado se depende de sua ausência ou presença.

---

### ALTO 6 — AGENTS.md e fluxo automatizado: sem regra de dedupe cross-fluxo

**Arquivo:** `AGENTS.md:6–18`

O AGENTS.md estabelece que imagem enviada ao Claude = populate full DB, incluindo
`link-term`. O spec define um fluxo automatizado onde a captura gera apenas um
`source` (sem `link-term`). Nenhum dos dois documentos menciona o outro.

A v1 propôs `ingest_mode` em `notes` como mitigação. Isso não resolve o problema —
`ingest_mode` apenas labela a proveniência depois da inserção. Não previne que a
mesma imagem gere dois registros por caminhos diferentes.

Se o usuário captura uma tela automaticamente e depois manda a mesma imagem ao
Claude, existirão dois sources para a mesma imagem: um sem termos (daemon) e um com
termos (AGENTS). A UI de revisão vai listar os dois sem distinção clara.

**Ação necessária:** o spec deve definir uma regra de dedupe cross-fluxo baseada em
conteúdo (hash da imagem ou SHA do arquivo) que opere **antes** da inserção,
independentemente do caminho de entrada. AGENTS.md deve ser atualizado para verificar
esse hash antes de criar novo source.

---

### MÉDIO 7 — `add_source` recebe `argparse.Namespace`; Opção B exige refatoração prévia

**Seção do spec:** Opção B em "Integração com o Código Atual", backlog linha 410

**Arquivo:** `lang_jp.py:158`

`add_source` recebe `args: argparse.Namespace` — é um handler de CLI, não função
reutilizável. `capture_jp.py` não pode chamá-la sem montar um Namespace falso.

O spec reconhece isso no backlog ("criar funcao interna para inserir source sem shell
externo", linha 410) mas não o coloca como pré-condição da Fase 3 — está listado
como item de backlog paralelo.

A extração é simples: o SQL de inserção em `lang_jp.py:159–186` é trivial de
encapsular. O problema é de sequenciamento, não de viabilidade.

**Correção:** mover "criar funcao interna" para pré-condição explícita da Fase 3,
não item de backlog opcional.

---

### MÉDIO 8 — Hotkey no Windows com fullscreen exclusivo: decisão de plataforma, não nota

**Seção do spec:** RF2, "Decisões Recomendadas para o Primeiro Corte" (linha 454),
"Questões em Aberto"

O spec lista hotkey como obrigatória no primeiro corte. Jogos em fullscreen exclusivo
ou com antitrapaça ativo (Easy Anti-Cheat, BattlEye) podem bloquear `pynput`,
`keyboard` e outras libs de hook global de teclado no Windows.

Isso não é risco residual — é uma decisão de implementação que precisa ser resolvida
antes de comprometer com a Fase 1. Alternativas concretas:

- AutoHotkey externo registrando hotkey de sistema (contorna hooks de processo)
- `win32con` + `RegisterHotKey` via Win32 API (mais robusto que libs Python puras)
- Aceitar que hotkey só funciona fora de fullscreen exclusivo e documentar a limitação

**Ação necessária:** mover da seção "Questões em Aberto" para pré-condição da Fase 1.
Testar com o jogo alvo antes de comprometer com RF2.

---

### MÉDIO 9 — `notes` como JSON: UI quebrada, queries frágeis, conflito com notas manuais

**Seção do spec:** "Estratégia de Dados" → "MVP sem migração"

**Arquivo:** `lang_jp_web.py` — exibição de `notes` como campo de texto plano

O spec propõe armazenar metadados de OCR, session_id, phash e status de revisão em
`notes` como JSON blob. Problemas concretos:

1. A UI web exibe `notes` como campo de texto plano — JSON vai aparecer como JSON
   cru na tela de revisão.
2. Filtrar "fontes não revisadas" vai exigir `json_extract(notes, '$.review_status')`
   row-by-row. Funciona mas é frágil e não indexável.
3. Se o usuário já tiver conteúdo manual em `notes` (via `add-source --notes`), a
   ingestão automática sobrescreve ou conflita com esse conteúdo.

**Alternativa mais limpa:** colunas dedicadas `ingest_source TEXT DEFAULT 'cli'` e
`review_status TEXT DEFAULT 'pending'` adicionadas via `ensure_runtime_schema`.
Mantém `notes` para comentários humanos. Uma migration de duas colunas tem risco zero.

---

### MÉDIO 10 — Janela de dedupe vaga; busca de phash em `notes` é cara

**Seção do spec:** "Dedupe e Controle de Ruído"

O spec não define quantas capturas recentes entram na janela de comparação, onde fica
o cache de hashes, nem como buscar phash no banco sem full scan. Armazenar hash em
`notes` exige `json_extract(notes, '$.image_hash')` em cada row.

**Recomendação:** definir janela de comparação explícita (ex.: últimas N capturas do
mesmo game/session) e manter o cache em memória no daemon para o MVP. Se persistência
for necessária, coluna dedicada com índice.

---

### MÉDIO 11 — OCR em fonte estilizada: risco listado sem pré-condição de validação

**Seção do spec:** "OCR" → "Recomendação inicial", seção "Riscos"

Tesseract sem pré-processamento costuma retornar string vazia ou lixo em fontes
bitmap estilizadas, texto com bordas/sombras e baixa resolução típicos de jogos. O
spec lista isso como risco mas não tem pré-condição de validação antes da Fase 2.

**Ação necessária:** adicionar como critério de aceite da Fase 1: "validar OCR com
10+ screenshots reais do jogo alvo antes de iniciar a Fase 2". Se inaceitável sem
pré-processamento, a Fase 2 precisa ser replanejada antes de ser iniciada.

---

### BAIXO 12 — `language_code` omitido no spec

`schema.sql:5` tem `language_code TEXT NOT NULL DEFAULT 'ja'`. O default funciona
para o caso japonês, mas o spec não documenta isso — pode causar surpresa em inserts
programáticos que omitem o campo.

---

### BAIXO 13 — Path do banco não resolvido para `capture_jp.py`

`lang_jp.py:17` deriva o path do banco de `__file__`. O spec não documenta como
`capture_jp.py` vai descobrir ou compartilhar esse valor, nem se ambos aceitarão
`--db` como override.

---

## Findings removidos em relação à v1

| Finding v1 | Motivo da remoção |
|---|---|
| CRÍTICO 1 (add_source inviabiliza Opção B) | Rebaixado para MÉDIO 7. O spec já tem o helper no backlog (linha 410). Problema de sequenciamento, não ausência de plano. |
| CRÍTICO 3 (WAL resolve tudo) | Reescrito como ALTO 3. O problema real é ownership da conexão do daemon + busy_timeout. WAL ajuda mas não é a solução principal. |
| ALTO 6 (untitled = ilegíveis) | Removido. CLI e web ainda mostram id, game, scene, timestamp. UX ruim, não blocker. |
| MÉDIO 7 (índice em captured_at) | Removido. Dedupe pode ser in-memory no daemon. Sem evidência de queries de range no banco que exijam índice. |
| MÉDIO 10 (romaji no exemplo JSON) | Removido. O exemplo é documentação, não comportamento do sistema. Ruído. |

---

## Perguntas em Aberto

1. Qual mecanismo de hotkey vai funcionar com os jogos alvo no Windows? (pré-condição
   da Fase 1)
2. O daemon vai usar conexão persistente ou reconectar a cada gravação?
3. `image_hash` em coluna dedicada (via `ensure_runtime_schema`) ou cache em memória
   no daemon?
4. `review_status` como coluna ou campo em `notes`?
5. Como delete/ignore de source vai disparar recálculo de stats?
6. Existe conjunto de screenshots reais para validar OCR antes da Fase 2?
7. A regra de dedupe cross-fluxo (daemon + AGENTS) vai ser hash exato do arquivo ou
   algo mais sofisticado?
8. `scene` será informado pelo usuário ao iniciar sessão, inferido ou omitido?

---

## Mudanças Concretas Recomendadas no Spec

**RF7 — expandir:** listar os subcomandos concretos que implementam revisão
(`update-source`, `delete-source` ou `ignore-source`). Sem isso RF7 é intenção sem
implementação.

**Fase 0 (nova, pré-condição da Fase 3):** extrair `insert_source(connection, *,
source_type, language_code, title, game, scene, source_path, raw_text, notes,
captured_at)` de `add_source`. Handler CLI vira wrapper. Incluir `delete_source(conn,
source_id)` com recálculo de stats dos terms afetados.

**Fase 3 — incluir dedupe mínimo:** gate SHA-256 do arquivo de imagem antes da
inserção. Não pode ficar para Fase 4.

**Fase 5 — redesenhar:** especificar quais mutations entram (`update-source`,
`delete-source`) e se o web module vai receber endpoints POST correspondentes. Hoje
o web é GET-only.

**Seção nova — "Gerenciamento de Stats após Mutação":** qualquer delete/ignore de
source deve recalcular `term_stats` e `kanji_stats` dos terms afetados. Documentar
explicitamente.

**Seção nova — "Interação com o Fluxo Manual Assistido por Claude":** definir regra
de dedupe cross-fluxo baseada em hash de imagem. AGENTS.md deve verificar esse hash
antes de inserir novo source.

**RNF — adicionar:**
- `PRAGMA busy_timeout = 5000` como requisito mínimo para o daemon
- Decisão sobre `journal_mode=WAL` documentada explicitamente
- Modelo de conexão do daemon documentado (persistente vs. por-gravação)

**Fase 1 — adicionar pré-condição:** validar mecanismo de hotkey no Windows com o
jogo alvo antes de comprometer com RF2.

---

## Avaliação Geral

O plano está correto na direção e nos princípios. A decisão de manter OCR como
rascunho e adiar `link-term` automático é a certa. A sequência de fases é
razoável, com exceção do dedupe chegando tarde demais.

Os problemas que vão aparecer no primeiro dia de código:

1. **RF7 sem implementação** — o fluxo de revisão não tem base de mutação no código
2. **Dedupe depois de ingestão** — banco poluído antes do dedupe existir, sem saída
3. **Stats corrompidas após delete** — silencioso, difícil de detectar
4. **Hotkey no Windows** — decisão de plataforma que precisa ser resolvida antes da Fase 1

O plano pode ser salvo sem mudança de estrutura grande. As lacunas são pontuais e têm
correções concretas. A sequência de fases precisa colocar os pré-requisitos de
mutação antes da Fase 5, e dedupe mínimo dentro da Fase 3.

**Riscos residuais:**
- Qualidade de OCR em fontes estilizadas — validar com screenshots reais antes da Fase 2
- Crescimento de volume de imagens sem política de retenção definida
- `lang_jp_web.py` não chama `ensure_runtime_schema` — monitorar se novas colunas
  adicionadas pelo CLI são necessárias no web
