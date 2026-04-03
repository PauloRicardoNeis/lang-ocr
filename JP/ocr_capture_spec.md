# Spec: Captura Automatizada + OCR para o Catalogo JP

## Resumo

Este documento define a especificacao e o planejamento de implementacao para automatizar a entrada de screenshots no catalogo JP sem quebrar o principio atual do projeto: a fonte bruta continua sendo a verdade, e a canonizacao dos termos continua assistida.

O objetivo nao e automatizar tudo de uma vez. O objetivo e remover friccao nas etapas de captura, OCR e registro de `sources`, mantendo a revisao humana antes de popular `source_terms`, `term_stats` e a cadeia de flashcards.

## Contexto Atual

Hoje o fluxo principal e:

1. Salvar um screenshot manualmente.
2. Registrar a captura como `source`.
3. Revisar o conteudo.
4. Ligar termos canonicos com `link-term`.
5. Deixar `term_stats` e `kanji_stats` refletirem recorrencia real.

O schema atual ja suporta a maior parte do MVP:

- `sources.source_path` para o arquivo de imagem.
- `sources.raw_text` para o OCR bruto.
- `sources.captured_at` para o timestamp original da captura.
- `source_terms` para as ligacoes revisadas entre fonte e termo.

Isso significa que o MVP pode ser entregue sem migracao de schema, desde que OCR continue sendo tratado como rascunho.

## Problema

O fluxo atual depende de passos manuais demais para um volume maior de capturas:

- tirar screenshot manualmente
- nomear e organizar arquivos
- rodar OCR fora do fluxo
- registrar a fonte no banco
- voltar depois para revisar

Esse atrito reduz a frequencia de captura e dificulta aproveitar janelas curtas de texto em jogos.

## Objetivos

- Capturar screenshots de forma automatizada durante uma sessao de jogo.
- Permitir tambem captura manual por hotkey para eventos rapidos.
- Rodar OCR local logo apos a captura.
- Registrar automaticamente a captura no banco como `source`.
- Preservar a imagem e o texto OCR como verdade bruta e auditavel.
- Reduzir ruido com deduplicacao de imagem e de texto.
- Criar uma fila de revisao para promover OCR em termos canonicos.

## Nao Objetivos

- Nao criar termos canonicos automaticamente no MVP.
- Nao preencher `source_terms` sem revisao humana.
- Nao traduzir para PT como verdade automatica.
- Nao gerar flashcards automaticamente a partir de OCR bruto.
- Nao depender de servicos pagos ou cloud no primeiro corte.

## Principios

- A imagem continua sendo a fonte primaria.
- OCR gera sugestao, nao verdade.
- Repeticao de tela estatica nao deve inflar `distinct_sources`.
- O fluxo automatico deve falhar de forma segura: melhor perder uma sugestao do que poluir o catalogo.
- A automacao deve se apoiar no fluxo atual de `lang_jp.py`, nao substitui-lo de imediato.

## Requisitos Funcionais

### RF1. Captura por intervalo

O sistema deve permitir screenshots automaticos em intervalo configuravel, com valor inicial recomendado de 30 segundos.

### RF2. Captura manual

O sistema deve oferecer uma hotkey para captura imediata fora do intervalo automatico.

### RF3. Persistencia da imagem

Cada captura aceita deve ser salva no filesystem com nome unico, organizado por jogo e sessao.

### RF4. OCR local

Cada captura aceita deve passar por OCR local logo apos o salvamento da imagem.

### RF5. Registro automatico no banco

Cada captura aceita deve gerar um `source` com:

- `source_type = image`
- `source_path`
- `raw_text`
- `captured_at`
- `game`
- `scene` quando disponivel
- `notes` com metadados auxiliares

### RF6. Deteccao de duplicatas

O sistema deve evitar registrar fontes repetidas demais por:

- hash perceptual da imagem
- similaridade do texto OCR
- janela curta de comparacao com capturas recentes

### RF7. Revisao posterior

O sistema deve permitir listar capturas recentes para:

- corrigir OCR
- ignorar ruido
- marcar contexto
- promover termos manualmente

### RF8. Compatibilidade com o fluxo atual

O catalogo existente deve continuar funcionando com:

- `add-source`
- `link-term`
- `list-sources`
- `browse`
- `rebuild-stats`

## Requisitos Nao Funcionais

- Funcionar localmente em Windows.
- Nao exigir conexao com internet.
- Nao bloquear a sessao de jogo com processamento pesado.
- Tolerar sessoes longas sem perder arquivos.
- Ser observavel por logs locais simples.
- Ser reiniciavel sem corromper o banco.

## Fluxo Proposto

1. O usuario inicia uma sessao de captura para um jogo.
2. O capturador observa o timer e a hotkey.
3. Uma captura e gerada da janela alvo ou da tela inteira.
4. O sistema calcula hash da imagem e compara com capturas recentes.
5. Se a captura for considerada duplicada, ela e descartada ou marcada como ruido.
6. Se a captura for aceita, a imagem e salva no disco.
7. O OCR local extrai texto em blocos curtos.
8. O sistema agrega o texto bruto para `raw_text` e salva metadados em `notes`.
9. O sistema cria o `source` no SQLite.
10. Mais tarde, o usuario revisa a fonte e usa o fluxo atual para ligar termos.

## Estrategia de Dados

### MVP sem migracao

O MVP deve reutilizar o schema atual.

- `source_path`: caminho absoluto ou relativo da imagem salva.
- `raw_text`: texto OCR consolidado para busca e leitura rapida.
- `notes`: JSON textual com metadados de OCR e captura.
- `captured_at`: timestamp original da captura.

Exemplo de conteudo planejado para `notes`:

```json
{
  "capture_mode": "interval",
  "capture_interval_seconds": 30,
  "session_id": "2026-03-25-persona5-001",
  "image_hash": "phash:abcd1234",
  "ocr_engine": "tesseract",
  "ocr_blocks": [
    {"text": "kaifuku", "confidence": 0.96},
    {"text": "mikata_tantai", "confidence": 0.88}
  ],
  "dedupe": {
    "compared_with_source_id": 123,
    "image_similarity": 0.98,
    "text_similarity": 0.91
  }
}
```

### Migracao futura opcional

Se o volume crescer e `notes` ficar insuficiente, considerar depois:

- `capture_sessions`
- `source_ocr_blocks`
- `source_review_status`

Essa migracao nao faz parte do primeiro corte.

## Componentes Propostos

### 1. Capturador

Responsavel por:

- timer de captura
- hotkey manual
- escolha de tela inteira ou janela alvo
- criacao do nome do arquivo
- salvamento em pasta por sessao

### 2. Pipeline de OCR

Responsavel por:

- chamar a engine OCR local
- normalizar retorno
- quebrar em blocos curtos
- devolver texto consolidado e confianca por bloco

### 3. Ingestao no Catalogo

Responsavel por:

- aplicar dedupe
- montar metadados
- criar `source` no banco
- registrar logs

### 4. Fila de Revisao

Responsavel por:

- listar fontes recentes
- mostrar preview da imagem e do OCR
- permitir correcoes
- facilitar o fluxo de `link-term`

## Integracao com o Codigo Atual

O desenho recomendado e estender o fluxo existente, nao criar um sistema paralelo.

### Opcoes de implementacao

Opcao A: novos subcomandos em `lang_jp.py`

- `capture-run`
- `ingest-image`
- `review-sources`

Opcao B: modulo separado que usa funcoes internas de `lang_jp.py`

- `capture_jp.py` para a automacao
- `lang_jp.py` continua como CLI principal do catalogo

Recomendacao: comecar pela Opcao B para manter responsabilidades mais claras e evitar inflar `lang_jp.py` logo no inicio.

## Dedupe e Controle de Ruido

Sem dedupe, uma HUD estatica pode gerar dezenas de `sources` praticamente identicas e distorcer prioridade.

### Heuristicas do MVP

- comparar hash perceptual da imagem atual com a ultima aceita
- comparar texto OCR atual com o OCR anterior
- exigir delta minimo de imagem ou texto para aceitar nova captura
- permitir bypass por hotkey manual

### Regras iniciais sugeridas

- descartar quando hash de imagem estiver acima do limiar de similaridade configurado
- descartar quando OCR novo for vazio e a imagem for muito parecida
- aceitar se houver mudanca textual relevante, mesmo com layout parecido
- aceitar sempre capturas manuais, salvo duplicata exata

## OCR

### Recomendacao inicial

Comecar com OCR local baseado em Tesseract com idioma japones.

Motivos:

- simples de instalar
- sem custo recorrente
- suficiente para validar o fluxo
- facil de trocar depois por outro backend

### Estrategia

- extrair texto em blocos, nao so em uma string unica
- guardar confianca quando disponivel
- consolidar em `raw_text` para busca rapida
- manter a possibilidade de correcao manual antes da promocao para termos

## Interface de Revisao

### MVP de revisao

O MVP pode comecar sem nova interface grafica complexa.

Possibilidades:

- enriquecer a UI atual em `lang_jp_web.py`
- ou criar comandos de revisao via CLI para listar e abrir capturas recentes

Recomendacao:

- primeiro garantir ingestao automatica confiavel
- depois adicionar uma tela simples na UI web para revisar `sources` recentes

### Acoes esperadas na revisao

- editar `raw_text`
- ajustar `scene`
- marcar `notes`
- ignorar captura
- chamar fluxo de promocao para termos

## Planejamento de Implementacao

### Fase 1: Captura local

Entregas:

- utilitario local para screenshot por intervalo
- suporte a hotkey manual
- salvamento em estrutura por sessao

Criterios de aceite:

- capturas regulares por pelo menos 1 hora sem falha
- nomes de arquivos unicos e previsiveis
- organizacao por jogo e sessao

### Fase 2: OCR automatizado

Entregas:

- integracao com engine OCR local
- texto OCR retornado logo apos a captura
- consolidacao de blocos curtos

Criterios de aceite:

- cada captura aceita produz `raw_text`
- OCR vazio ou fraco nao derruba o pipeline

### Fase 3: Ingestao no SQLite

Entregas:

- criacao automatica de `sources`
- preenchimento de `source_path`, `raw_text`, `captured_at` e `notes`
- logs locais por captura

Criterios de aceite:

- fonte criada no banco para toda captura aceita
- imagem e texto associados corretamente

### Fase 4: Dedupe e ruido

Entregas:

- comparacao com capturas recentes
- descarte de duplicatas obvias
- bypass para captura manual

Criterios de aceite:

- menus estaticos deixam de inflar o banco
- capturas com texto novo continuam entrando

### Fase 5: Revisao assistida

Entregas:

- lista de fontes recentes
- edicao rapida de OCR e contexto
- ponte rapida para `link-term`

Criterios de aceite:

- uma fonte pode ser revisada e promovida em poucos passos
- o usuario nao precisa sair do fluxo principal para corrigir OCR

### Fase 6: Refinamento

Entregas:

- tags de contexto melhores
- heuristicas por tipo de tela
- filtros para ruido recorrente

Criterios de aceite:

- melhor separacao entre dialogo, menu, combate e sistema
- menor volume de capturas sem valor

## Backlog Tecnico Sugerido

### Infraestrutura

- definir pasta padrao para capturas
- definir formato de nome de sessao
- definir estrategia de logs

### Captura

- implementar captura por timer
- implementar hotkey
- adicionar suporte a janela alvo e tela inteira

### OCR

- integrar Tesseract
- padronizar parser de retorno
- serializar blocos e confianca em `notes`

### Catalogo

- criar funcao interna para inserir `source` sem shell externo
- adicionar comando ou modulo para ingestao automatica
- adicionar listagem filtrada de fontes recentes

### Revisao

- criar status simples de revisao, nem que seja em `notes` no MVP
- permitir correcao rapida de `raw_text`
- facilitar promocao para `link-term`

### Qualidade

- testes de dedupe
- testes de parse de OCR
- teste de escrita concorrente no SQLite
- teste de recuperacao apos falha

## Riscos

- OCR ruim em fonte pequena, blur ou texto estilizado
- excesso de capturas de HUD ou menu parado
- crescimento rapido do volume de imagens
- falsa sensacao de qualidade se OCR alimentar termos cedo demais
- complexidade excessiva na UI antes de validar o pipeline

## Mitigacoes

- revisar antes de ligar termos
- dedupe por imagem e texto
- permitir ignorar capturas com um passo
- manter logs simples para auditoria
- comecar com OCR local trocavel e thresholds configuraveis

## Criterios Gerais de Sucesso

- reduzir drasticamente o trabalho manual de captura
- registrar mais fontes uteis por sessao
- manter a qualidade do catalogo sem inflar ruido
- permitir revisao rapida de OCR antes da canonizacao
- preservar compatibilidade com o banco e os comandos atuais

## Decisoes Recomendadas para o Primeiro Corte

- usar timer de 30 segundos com configuracao simples
- incluir hotkey obrigatoria desde o inicio
- salvar imagem sempre antes do OCR
- reutilizar schema atual sem migracao
- armazenar metadados de OCR em `notes`
- manter termos e flashcards fora da automacao inicial

## Questoes em Aberto

- qual engine de captura usar no Windows para janela alvo
- qual hotkey padrao nao conflita com o jogo
- se `scene` sera manual, inferido ou ambos
- se a UI de revisao entra cedo em `lang_jp_web.py` ou fica para a segunda etapa
- quanto historico de capturas recentes deve entrar na comparacao de dedupe

## Proximo Passo Recomendado

Implementar primeiro um MVP de `captura -> OCR -> add-source`, sem tentar automatizar termos. Esse e o menor corte que valida o ganho de produtividade sem risco alto de poluir `terms`, `source_terms` e `term_stats`.
